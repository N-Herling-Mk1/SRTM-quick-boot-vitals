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
import io
import time
import statistics
from collections import defaultdict

import requests
from flask import Flask, jsonify, render_template

# ----------------------------------------------------------------- config
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
T_CHANGE_WARN, T_CHANGE_ALARM = 30, 120

HERE = os.path.dirname(os.path.abspath(__file__))
CLASS_CSV = os.path.join(HERE, "data", "node_classification.csv")

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


CLASSES = load_classification()


# ------------------------------------------------------------------- influx
def flux(query):
    """POST a Flux query, return parsed CSV rows. Read-only by construction."""
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
    return list(csv.DictReader(io.StringIO(r.text)))


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
        times  = [iso_to_epoch(t) for t, _ in points]
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

        channels.append({
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

    canaries = [c for c in channels if c["canary"]]
    if not canaries:
        source_state = "unknown"
    elif any(c["verdict"] == "stale" for c in canaries):
        source_state = "frozen"
    elif any(c["verdict"] == "watch" for c in canaries):
        source_state = "watch"
    elif all(c["verdict"] == "warming" for c in canaries):
        source_state = "warming"
    else:
        source_state = "live"

    return {
        "generated":  now,
        "window_min": WINDOW_MIN,
        "min_samples": MIN_SAMPLES,
        "poll_interval": POLL_INTERVAL,
        "clocks": {
            "t_poll":       t_poll,
            "t_poll_state": poll_state,
            "source_state": source_state,
            "t_wincc":      None,
            "t_wincc_state": "not_implemented",
        },
        "counts":   {"total": len(channels)},
        "channels": channels,
    }


# ------------------------------------------------------------------- routes
@app.route("/")
def index():
    return render_template("index.html", poll_interval=POLL_INTERVAL)


@app.route("/api/vitals")
def api_vitals():
    if not INFLUX_TOKEN:
        return jsonify({"error": "INFLUXDB_TOKEN is not set. "
                                 "Source the .env before starting."}), 500
    try:
        return jsonify(build_vitals())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"InfluxDB unreachable at {INFLUX_URL}: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/health")
def api_health():
    return jsonify({
        "influx_url": INFLUX_URL,
        "bucket": INFLUX_BUCKET,
        "org": INFLUX_ORG,
        "token_present": bool(INFLUX_TOKEN),
        "classified_fields": len(CLASSES),
    })


if __name__ == "__main__":
    print("=" * 62)
    print(" srtm-quickboot-vitals")
    print("=" * 62)
    print(f"  influx     : {INFLUX_URL}")
    print(f"  bucket/org : {INFLUX_BUCKET} / {INFLUX_ORG}")
    print(f"  token      : {'present' if INFLUX_TOKEN else 'MISSING -- source .env'}")
    print(f"  classified : {len(CLASSES)} fields")
    print(f"  window     : {WINDOW_MIN} min, min {MIN_SAMPLES} samples")
    print(f"  serving    : http://127.0.0.1:5055")
    print("=" * 62, flush=True)
    app.run(host="127.0.0.1", port=5055, debug=False)
