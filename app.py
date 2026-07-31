#!/usr/bin/env python3
"""
srtm-quickboot-vitals :: app.py

Read-only front door onto the SRTM board data already flowing into
InfluxDB via Prof. Cheu's TIG stack.

WHAT THIS DOES NOT DO
---------------------
It does not write to InfluxDB. It does not touch Grafana. It does not
modify, restart, or reconfigure any container. Every query is a read.
The stack it reads from is production monitoring owned by someone else.

WHY A BACKEND EXISTS AT ALL
---------------------------
A browser cannot query InfluxDB directly: that would ship the API token
into client-side JavaScript and require a CORS exception on a container
we do not own. This process holds the token and serves plain JSON.

THE THREE CLOCKS
----------------
  t_poll    seconds since the collector last wrote anything    (global)
  t_change  seconds since a channel's value last differed      (per channel)
  t_wincc   deferred to mk_1 -- needs WinCC CTRL or RDB access
"""

import csv
import os
import time
import statistics
from collections import defaultdict

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

# ----------------------------------------------------------------- config
def _load_env_file():
    """Read .env from the repo root into os.environ if present.

    Without this, `python app.py` starts with no token and every /api call
    500s -- the shell has to have sourced .env first, which is easy to forget
    and gives a page that looks broken rather than unconfigured. Values
    already in the environment win, so `set -a; . .env` still overrides.
    No dependency: this is twelve lines, not python-dotenv.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_env_file()

INFLUX_URL    = os.environ.get("INFLUX_URL", "http://127.0.0.1:8096")
INFLUX_ORG    = os.environ.get("INFLUXDB_ORG", "SRTM")
INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "SRTM-bucket")
INFLUX_TOKEN  = os.environ.get("INFLUXDB_TOKEN", "")
MEASUREMENT   = os.environ.get("SRTM_MEASUREMENT", "srtm")

WINDOW_MIN    = int(os.environ.get("VITALS_WINDOW_MIN", "30"))
MIN_SAMPLES   = int(os.environ.get("VITALS_MIN_SAMPLES", "20"))
POLL_INTERVAL = int(os.environ.get("VITALS_POLL_SEC", "5"))

# thresholds (seconds) -- provisional, see three_clock_spec.txt sec 3
T_POLL_WARN,   T_POLL_ALARM   = 15, 60

# Cadence bins -- seconds of MEDIAN GAP between value changes.
# These are a taxonomy, not a health scale: green = changes often,
# red = changes rarely. Edges were cut from the measured distribution
# (tools/collect_cadence.py), both landing in empty regions:
#     52 ch @ 5s | 10 @ 10s | 6 @ 15-25s | (void) | 4 @ 35-85s |
#     (void) | 3 @ 7200s | ~90 with no change in 24h
CAD_GREEN  = float(os.environ.get("VITALS_CAD_GREEN",  "30"))       # 30s
CAD_YELLOW = float(os.environ.get("VITALS_CAD_YELLOW", "300"))      # 5m
CAD_ORANGE = float(os.environ.get("VITALS_CAD_ORANGE", "86400"))    # 24h
T_CHANGE_WARN, T_CHANGE_ALARM = 30, 120

HERE = os.path.dirname(os.path.abspath(__file__))
CLASS_CSV = os.path.join(HERE, "data", "node_classification.csv")
CAD_CSV   = os.path.join(HERE, "data", "cadence_stats.csv")

app = Flask(__name__)


# ------------------------------------------------------------ classification
def load_classification():
    """field_name -> {class, canary, notes}. Drives what we alarm on."""
    table = {}
    with open(CLASS_CSV, newline="") as f:
        for row in csv.DictReader(f):
            table[row["field_name"]] = {
                "class":  row["class"],
                "canary": row["canary"] == "Y",
                "notes":  row["notes"],
            }
    return table


def load_cadence():
    """field -> measured change-cadence stats from tools/collect_cadence.py.

    Keys: med, mean, p10, p90, min, max, n_gaps, burstiness, window_days.
    A row with a blank median means the channel never changed during the
    measurement window -- kept, because "never changed in 24h" is itself
    the slowest cadence, not missing data.
    """
    table = {}
    if not os.path.exists(CAD_CSV):
        return table

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    with open(CAD_CSV, newline="") as f:
        for row in csv.DictReader(f):
            table[row["field_name"]] = {
                "med":    num(row.get("gap_med_s")),
                "mean":   num(row.get("gap_mean_s")),
                "p10":    num(row.get("gap_p10_s")),
                "p90":    num(row.get("gap_p90_s")),
                "min":    num(row.get("gap_min_s")),
                "max":    num(row.get("gap_max_s")),
                "n_gaps": num(row.get("n_gaps")),
                "burst":  num(row.get("burstiness")),
                "days":   num(row.get("window_days")),
                "notes":  row.get("notes", ""),
            }
    return table


CLASSES = load_classification()
CADENCE = load_cadence()


def cadence_of(field):
    """Colour bin for one channel, by how often it changes. Taxonomy only:
    this makes no claim about health. Returns (bin, median_gap_seconds).

        green   <= 30s        fast movers, at or near the 5s polling floor
        yellow  30s - 5m
        orange  5m  - 24h     e.g. the 2-hour FireFly uptime counters
        red     > 24h         rare, including "never changed in the window"
        grey    not measured  no row in cadence_stats.csv
    """
    row = CADENCE.get(field)
    if row is None:
        return "grey", None
    med = row.get("med")
    if med is None:
        # Present in the sweep but never changed -> slowest bin, by definition.
        return "red", None
    if med <= CAD_GREEN:
        return "green", med
    if med <= CAD_YELLOW:
        return "yellow", med
    if med <= CAD_ORANGE:
        return "orange", med
    return "red", med


def gap_stats(pts):
    """Mean / stdev / median of the interval between consecutive value
    CHANGES, computed over whatever window the caller fetched.

    Deliberately separate from the mean/sigma of the VALUE. This measures
    rhythm; that measures magnitude. They answer different questions and
    conflating them is how 'sigma' ends up meaning two things.
    """
    changes, prev = [], None
    for t, v in pts:
        if prev is None:
            prev = v
            continue
        if v != prev:
            changes.append(t)
            prev = v
    gaps = [changes[i] - changes[i - 1] for i in range(1, len(changes))]
    if not gaps:
        return {"n_changes": len(changes), "n_gaps": 0, "gap_mean": None,
                "gap_sd": None, "gap_med": None, "gap_min": None,
                "gap_max": None}
    return {
        "n_changes": len(changes),
        "n_gaps":    len(gaps),
        "gap_mean":  statistics.fmean(gaps),
        "gap_sd":    statistics.pstdev(gaps) if len(gaps) > 1 else 0.0,
        "gap_med":   statistics.median(gaps),
        "gap_min":   min(gaps),
        "gap_max":   max(gaps),
    }


# ------------------------------------------------------------------- influx
def flux(query):
    """POST a Flux query, return parsed rows. Read-only by construction.

    InfluxDB returns ANNOTATED CSV, not plain CSV:
      - '#group' / '#datatype' / '#default' comment lines before each table
      - a leading empty annotation column on every row
      - a FRESH header row for every result table
      - blank lines separating tables

    csv.DictReader on the raw body treats the first line as the schema and
    every later header row as data, which is how the literal string '_time'
    ends up being handed to the date parser. Parse defensively instead:
    reset the header at each table boundary and drop repeated headers.
    The empty annotation column simply maps to the key '' and is ignored.
    """
    r = requests.post(
        f"{INFLUX_URL}/api/v2/query",
        params={"org": INFLUX_ORG},
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        data=query.encode("utf-8"),
        timeout=30,
    )
    r.raise_for_status()

    rows, header = [], None
    for raw in r.text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            header = None          # blank line ends the current table
            continue
        if line.lstrip().startswith("#"):
            continue               # annotation line
        try:
            parts = next(csv.reader([line]))
        except StopIteration:
            continue
        if header is None:
            header = parts         # first non-comment line of a table
            continue
        if parts == header:
            continue               # repeated header
        rows.append(dict(zip(header, parts)))
    return rows


def safe_epoch(s):
    """None instead of an exception. A single malformed row must never take
    down the whole endpoint."""
    try:
        return iso_to_epoch(s)
    except (ValueError, TypeError, AttributeError):
        return None


def fetch_window():
    """One query pulls the whole window; every metric is derived from it."""
    q = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{WINDOW_MIN}m)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> keep(columns: ["_time", "_field", "_value"])
'''
    series = defaultdict(list)
    for row in flux(q):
        field = row.get("_field")
        t     = row.get("_time")
        v     = row.get("_value")
        if not field or not t:
            continue
        if safe_epoch(t) is None:
            continue
        series[field].append((t, v))
    for f in series:
        series[f].sort(key=lambda p: p[0])
    return series


def iso_to_epoch(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest[:6], rest[len(rest) - 6:]
        s = f"{head}.{frac}{tz}"
    from datetime import datetime
    return datetime.fromisoformat(s).timestamp()


# ------------------------------------------------------------------ metrics
def verdict(cls, canary, t_change, sigma, n):
    """
    Staleness verdict. Only COUNTER canaries and ANALOG channels are
    alarmable -- DISCRETE is legitimately flat, STATIC never changes.
    Sigma alarming is deliberately NOT wired: it needs a per-channel
    baseline from history first (spec sec 8.3). Displayed, not judged.
    """
    if cls in ("STATIC", "DISCRETE"):
        return "info"
    if n < MIN_SAMPLES:
        return "warming"
    if t_change is None:
        return "stale" if cls == "COUNTER" else "flat"
    if canary:
        if t_change > T_CHANGE_ALARM:
            return "stale"
        if t_change > T_CHANGE_WARN:
            return "watch"
        return "live"
    if sigma is not None and sigma == 0.0:
        return "flat"
    return "live"


def build_vitals():
    now = time.time()
    series = fetch_window()

    channels, last_write = [], None
    for field, points in series.items():
        meta = CLASSES.get(field, {"class": "UNKNOWN", "canary": False,
                                   "notes": "not in classification csv"})
        times  = [safe_epoch(t) for t, _ in points]
        raws   = [v for _, v in points]
        newest = times[-1] if times else None
        if newest and (last_write is None or newest > last_write):
            last_write = newest

        # numeric coercion -- string fields (Hwid, serials) have no stats
        nums, numeric = [], True
        for v in raws:
            try:
                nums.append(float(v))
            except (TypeError, ValueError):
                numeric = False
                break

        # t_change: walk back to the last value that differed from current
        t_change = None
        if raws:
            cur = raws[-1]
            for i in range(len(raws) - 2, -1, -1):
                if raws[i] != cur:
                    t_change = now - times[i + 1]
                    break

        mean = sigma = None
        if numeric and len(nums) >= 2:
            mean  = statistics.fmean(nums)
            sigma = statistics.pstdev(nums)

        band, med = cadence_of(field)
        channels.append({
            "heat":      band,
            "cad_med":   med,
            "field":     field,
            "cls":       meta["class"],
            "canary":    meta["canary"],
            "value":     raws[-1] if raws else None,
            "numeric":   numeric,
            "age":       round(now - newest, 1) if newest else None,
            "t_change":  round(t_change, 1) if t_change is not None else None,
            "mean":      mean,
            "sigma":     sigma,
            "n":         len(points),
            "verdict":   verdict(meta["class"], meta["canary"],
                                 t_change, sigma, len(points)),
        })

    order = {"COUNTER": 0, "ANALOG": 1, "DISCRETE": 2, "STATIC": 3, "UNKNOWN": 4}
    channels.sort(key=lambda c: (order.get(c["cls"], 9), c["field"]))

    t_poll = round(now - last_write, 1) if last_write else None
    if t_poll is None:
        poll_state = "dead"
    elif t_poll > T_POLL_ALARM:
        poll_state = "dead"
    elif t_poll > T_POLL_WARN:
        poll_state = "watch"
    else:
        poll_state = "live"

    heat_counts = {}
    for c in channels:
        heat_counts[c["heat"]] = heat_counts.get(c["heat"], 0) + 1

    # FAST canaries only. Measurement showed FF11/12/13_uptime tick every
    # 7200s -- they cannot say anything about liveness for up to two hours,
    # so including them produced a "frozen" verdict on a healthy board.
    # A canary is usable here only if its measured cadence is inside the
    # green bin.
    canaries = [c for c in channels if c["canary"]]
    fast = [c for c in canaries
            if c["cad_med"] is not None and c["cad_med"] <= CAD_GREEN]
    slow = [c["field"] for c in canaries if c not in fast]

    return {
        "generated":  now,
        "window_min": WINDOW_MIN,
        "min_samples": MIN_SAMPLES,
        "poll_interval": POLL_INTERVAL,
        "clocks": {
            "t_poll":       t_poll,
            "t_poll_state": poll_state,
            "fast_canaries": [c["field"] for c in fast],
            "slow_canaries": slow,
            "t_wincc":      None,
            "t_wincc_state": "not_implemented",
        },
        "counts":   {"total": len(channels)},
        "heat":     heat_counts,
        "cad_cuts": {"green": CAD_GREEN, "yellow": CAD_YELLOW,
                     "orange": CAD_ORANGE},
        "cadence_fields": len(CADENCE),
        "channels": channels,
    }


# ------------------------------------------------------------------- routes
@app.route("/")
def index():
    return render_template("index.html", poll_interval=POLL_INTERVAL)


@app.route("/api/vitals")
def api_vitals():
    if not INFLUX_TOKEN:
        return jsonify({"error": "INFLUXDB_TOKEN is not set. Copy "
                                 ".env.example to .env and fill it in."}), 500
    try:
        return jsonify(build_vitals())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"InfluxDB unreachable at {INFLUX_URL}: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/channel/<field>")
def api_channel(field):
    """Full series for one channel, for the detail view's time plot.

    Separate from /api/vitals deliberately: the grid needs 161 current
    values, the detail view needs one channel's history. Pulling both in
    one query would make every 5s poll drag the whole history across.
    """
    if not INFLUX_TOKEN:
        return jsonify({"error": "INFLUXDB_TOKEN is not set."}), 500
    try:
        minutes = max(5, min(int(request.args.get("minutes", WINDOW_MIN)), 10080))
    except (TypeError, ValueError):
        minutes = WINDOW_MIN

    safe = field.replace('"', '')
    q = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{minutes}m)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "{safe}")
  |> keep(columns: ["_time", "_value"])
'''
    try:
        rows = flux(q)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"InfluxDB unreachable: {e}"}), 502

    pts = []
    for r in rows:
        t, v = r.get("_time"), r.get("_value")
        e = safe_epoch(t) if t else None
        if e is None:
            continue
        pts.append((e, v))
    pts.sort(key=lambda p: p[0])

    nums, numeric = [], True
    for _, v in pts:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            numeric = False
            break

    meta = CLASSES.get(field, {"class": "UNKNOWN", "canary": False, "notes": ""})
    out = {
        "field": field,
        "cls": meta["class"],
        "canary": meta["canary"],
        "notes": meta["notes"],
        "minutes": minutes,
        "numeric": numeric,
        "n": len(pts),
        "series": [[p[0] * 1000, (nums[i] if numeric else None)]
                   for i, p in enumerate(pts)],
        "raw_last": pts[-1][1] if pts else None,
    }
    if numeric and len(nums) >= 2:
        out.update({
            "mean":  statistics.fmean(nums),
            "sigma": statistics.pstdev(nums),
            "min":   min(nums),
            "max":   max(nums),
        })
    else:
        out.update({"mean": None, "sigma": None, "min": None, "max": None})

    t_change = None
    if pts:
        cur = pts[-1][1]
        for i in range(len(pts) - 2, -1, -1):
            if pts[i][1] != cur:
                t_change = time.time() - pts[i + 1][0]
                break
    out["t_change"] = round(t_change, 1) if t_change is not None else None

    # rhythm of this channel over the window actually being displayed
    g = gap_stats(pts)
    out["gaps"] = {k: (round(v, 3) if isinstance(v, float) else v)
                   for k, v in g.items()}

    # the 24h reference measurement, for comparison against the above
    band, med = cadence_of(field)
    ref = CADENCE.get(field) or {}
    out["cadence"] = {
        "bin": band, "median": med,
        "ref_mean": ref.get("mean"), "ref_p10": ref.get("p10"),
        "ref_p90": ref.get("p90"), "ref_min": ref.get("min"),
        "ref_max": ref.get("max"), "ref_n_gaps": ref.get("n_gaps"),
        "burstiness": ref.get("burst"), "ref_days": ref.get("days"),
        "ref_notes": ref.get("notes", ""),
    }
    return jsonify(out)


@app.route("/favicon.ico")
def favicon():
    """Browsers request /favicon.ico from the site root regardless of the
    <link> tags, so serve it there too rather than logging a 404 every load."""
    return send_from_directory(os.path.join(HERE, "static"), "favicon.ico",
                               mimetype="image/vnd.microsoft.icon")


@app.route("/api/health")
def api_health():
    return jsonify({
        "influx_url": INFLUX_URL,
        "bucket": INFLUX_BUCKET,
        "org": INFLUX_ORG,
        "token_present": bool(INFLUX_TOKEN),
        "classified_fields": len(CLASSES),
        "cadence_fields": len(CADENCE),
    })


if __name__ == "__main__":
    print("=" * 62)
    print(" srtm-quickboot-vitals")
    print("=" * 62)
    print(f"  influx     : {INFLUX_URL}")
    print(f"  bucket/org : {INFLUX_BUCKET} / {INFLUX_ORG}")
    print(f"  token      : "
          f"{'present' if INFLUX_TOKEN else 'MISSING -- cp .env.example .env'}")
    print(f"  classified : {len(CLASSES)} fields")
    print(f"  cadence    : {len(CADENCE)} fields"
          f"{'  <-- run tools/collect_cadence.py' if not CADENCE else ''}")
    print(f"  window     : {WINDOW_MIN} min, min {MIN_SAMPLES} samples")
    print(f"  serving    : http://127.0.0.1:5055")
    print("=" * 62, flush=True)
    app.run(host="127.0.0.1", port=5055, debug=False)
