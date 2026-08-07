#!/usr/bin/env python3
"""
srtm-quickboot-vitals :: tools/collect_cadence.py

STEP 1 OF 2. Measures how often every channel actually changes and writes
data/cadence_stats.csv. It does NOT decide what "fast" or "slow" means --
that comes after looking at the numbers.

WHY NOT JUST period / transitions
---------------------------------
That is a mean, and a mean hides burstiness. A channel that flips 500 times
in one minute and then sits dead for a day has the same mean interval as one
ticking steadily every three minutes. They are not the same channel and they
must not land in the same colour bin.

So we measure the distribution of GAPS BETWEEN CONSECUTIVE VALUE CHANGES:

    difference()                 value deltas
    filter(_value != 0)          keep only real transitions
    elapsed(unit: 1s)            adds an 'elapsed' column (seconds)
    map(_value = elapsed)        MUST promote it -- elapsed() does NOT
                                 replace _value, it appends. Aggregating
                                 without this silently averages the VALUE
                                 deltas instead of the time gaps, which
                                 shows up as negative "seconds".
    mean/median/quantiles/max    computed server-side, per field

median is the number to trust for a channel's normal rhythm. The spread
between p10 and p90 tells you whether it has a rhythm at all. A channel with
median 5s and p90 4000s is bursty, not slow, and needs different treatment
from one that is steadily slow.

TWO PASSES
----------
    pass 1   --days 1        every numeric field
    pass 2   --deep-days 14  only fields with too few transitions to measure

A day is plenty for a 5-second channel (about 17,000 samples). Only genuinely
slow channels need the long window, and there are few of them -- which is why
a 14-day sweep over all 165 fields times out and this does not.

USAGE
-----
    set -a; . ~/srtm-TIG/.env; set +a
    export INFLUX_URL=http://127.0.0.1:8096
    .venv/bin/python tools/collect_cadence.py

    # from another terminal:
    tail -f logs/collect_cadence.log

Then read data/cadence_stats.csv and the decile table this prints, and pick
the bin edges from what is actually there.

READ-ONLY. Queries Prof. Cheu's bucket, writes nothing to it.
"""

import argparse
import csv
import datetime as dt
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "cadence_stats.csv")
CLASS_CSV = os.path.join(ROOT, "data", "node_classification.csv")
LOG = os.path.join(ROOT, "logs", "collect_cadence.log")

INFLUX_URL = os.environ.get("INFLUX_URL", "http://127.0.0.1:8096")
INFLUX_ORG = os.environ.get("INFLUXDB_ORG", "SRTM")
INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "SRTM-bucket")
INFLUX_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
MEASUREMENT = os.environ.get("SRTM_MEASUREMENT", "srtm")

# yield name -> csv column
AGGS = [
    ("n_gaps",   "count()"),
    ("gap_mean", "mean()"),
    ("gap_med",  "median()"),
    ("gap_p10",  "quantile(q: 0.10)"),
    ("gap_p90",  "quantile(q: 0.90)"),
    ("gap_min",  "min()"),
    ("gap_max",  "max()"),
]

_logf = None


def log(msg=""):
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    line = f"{stamp}  {msg}" if msg else ""
    print(line, flush=True)
    if _logf:
        _logf.write(line + "\n")
        _logf.flush()


def hms(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def human(sec):
    """Seconds -> compact human string, for the log tables only."""
    if sec is None:
        return "-"
    if sec < 1:
        return f"{sec:.2f}s"
    if sec < 90:
        return f"{sec:.1f}s"
    if sec < 5400:
        return f"{sec / 60:.1f}m"
    if sec < 172800:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"


def flux(query, timeout):
    """Annotated-CSV aware. Keeps the 'result' column so multiple yields in
    one query can be told apart."""
    r = requests.post(
        f"{INFLUX_URL}/api/v2/query",
        params={"org": INFLUX_ORG},
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        data=query.encode("utf-8"),
        timeout=timeout,
    )
    r.raise_for_status()
    rows, header = [], None
    for raw in r.text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            header = None
            continue
        if line.lstrip().startswith("#"):
            continue
        try:
            parts = next(csv.reader([line]))
        except StopIteration:
            continue
        if header is None:
            header = parts
            continue
        if parts == header:
            continue
        rows.append(dict(zip(header, parts)))
    return rows


def load_classes():
    t = {}
    if os.path.exists(CLASS_CSV):
        with open(CLASS_CSV, newline="") as f:
            for row in csv.DictReader(f):
                t[row["field_name"]] = row["class"]
    return t


def discover(days, timeout):
    q = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> last()
  |> keep(columns: ["_field", "_value"])
'''
    numeric, strings = [], []
    for r in flux(q, timeout):
        f = r.get("_field")
        if not f:
            continue
        try:
            float(r.get("_value"))
            numeric.append(f)
        except (TypeError, ValueError):
            strings.append(f)
    return sorted(set(numeric)), sorted(set(strings))


def stats_batch(fields, days, timeout):
    """One query, several yields. 'base' is the gap series in seconds."""
    lst = ", ".join(f'"{f}"' for f in fields)
    pipes = "\n".join(
        f'base |> {expr} |> keep(columns: ["_field", "_value"]) '
        f'|> yield(name: "{name}")'
        for name, expr in AGGS)
    q = f'''
base = from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> filter(fn: (r) => contains(value: r._field, set: [{lst}]))
  |> difference(nonNegative: false)
  |> filter(fn: (r) => r._value != 0.0)
  |> elapsed(unit: 1s)
  |> map(fn: (r) => ({{ r with _value: float(v: r.elapsed) }}))

{pipes}
'''
    out = {f: {} for f in fields}
    for r in flux(q, timeout):
        f, res = r.get("_field"), r.get("result")
        if not f or f not in out or not res:
            continue
        try:
            out[f][res] = float(r.get("_value"))
        except (TypeError, ValueError):
            pass
    return out


def stats_adaptive(fields, days, timeout):
    """Timeout -> halve the batch and retry, down to single fields."""
    try:
        return stats_batch(fields, days, timeout)
    except requests.exceptions.Timeout:
        if len(fields) == 1:
            log(f"        FAILED  {fields[0]}  (alone, {timeout}s)")
            return {fields[0]: None}
        mid = len(fields) // 2
        log(f"        timeout on {len(fields)} -> split {mid}/{len(fields)-mid}")
        a = stats_adaptive(fields[:mid], days, timeout)
        a.update(stats_adaptive(fields[mid:], days, timeout))
        return a


def run_pass(label, fields, days, chunk, timeout):
    nb = (len(fields) + chunk - 1) // chunk
    log(f"      {label}: {len(fields)} fields, {nb} batches of {chunk}, {days}d")
    got, times = {}, []
    for i in range(nb):
        b = fields[i * chunk:(i + 1) * chunk]
        t0 = time.time()
        got.update(stats_adaptive(b, days, timeout))
        el = time.time() - t0
        times.append(el)
        eta = (sum(times) / len(times)) * (nb - i - 1)
        log(f"      batch {i+1}/{nb}  {len(b):>3} fields  {el:6.1f}s  "
            f"eta ~{hms(eta)}")
    return got


def decile_table(vals, label):
    """The point of the whole exercise: what does the population look like."""
    if not vals:
        return
    v = sorted(vals)
    log("")
    log(f"  --- {label}: distribution across {len(v)} channels ---")
    for p in (0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
        idx = min(len(v) - 1, int(round((p / 100) * (len(v) - 1))))
        log(f"      p{p:<3}  {human(v[idx]):>10}   ({v[idx]:.3f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    # Default OFF. Enabling it over every low-count field pulls in all the
    # DISCRETE flags and retired SPI channels -- 100+ fields, ~3 hours, to
    # measure things that rarely change by design. Point it at a short
    # explicit list instead when a specific slow channel matters.
    ap.add_argument("--deep-days", type=int, default=0)
    ap.add_argument("--min-gaps", type=int, default=2,
                    help="fewer gaps than this in the fast pass -> deep pass")
    ap.add_argument("--chunk", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--log", default=LOG)
    a = ap.parse_args()

    if not INFLUX_TOKEN:
        sys.exit("FATAL: INFLUXDB_TOKEN not set. "
                 "Run: set -a; . ~/srtm-TIG/.env; set +a")

    global _logf
    os.makedirs(os.path.dirname(a.log), exist_ok=True)
    _logf = open(a.log, "w")
    t0 = time.time()

    log("=" * 68)
    log(" collect_cadence -- how often does each channel actually change")
    log("=" * 68)
    log(f"  bucket {INFLUX_BUCKET}   fast {a.days}d   deep {a.deep_days}d"
        f"   chunk {a.chunk}   timeout {a.timeout}s")
    log(f"  log    {a.log}")
    log("")
    log("  This measures. It does not classify. Bin edges come after you")
    log("  read data/cadence_stats.csv and the decile table below.")
    log("")

    classes = load_classes()
    log(f"[1/5] classification: {len(classes)} fields known")

    log(f"[2/5] discovering fields in the last {a.days}d ...")
    numeric, strings = discover(a.days, a.timeout)
    log(f"      {len(numeric)} numeric, {len(strings)} string")
    unk = [f for f in numeric + strings if f not in classes]
    if unk:
        log(f"      NOTE {len(unk)} unclassified: {', '.join(unk)}")

    log("[3/5] fast pass ...")
    res = run_pass("fast", numeric, a.days, a.chunk, a.timeout)
    used_days = {f: a.days for f in numeric}

    sparse = [f for f, s in res.items()
              if s is not None and s.get("n_gaps", 0) < a.min_gaps]
    if a.deep_days and sparse:
        log(f"[4/5] deep pass: {len(sparse)} sparse field(s) over {a.deep_days}d")
        log(f"      {', '.join(sparse[:14])}" + (" ..." if len(sparse) > 14 else ""))
        deep = run_pass("deep", sparse, a.deep_days,
                        max(4, a.chunk // 2), a.timeout)
        for f, s in deep.items():
            if s and s.get("n_gaps", 0) > (res.get(f) or {}).get("n_gaps", 0):
                res[f] = s
                used_days[f] = a.deep_days
    else:
        log("[4/5] deep pass skipped")

    log(f"[5/5] writing {a.out} ...")
    cols = ["field_name", "class", "window_days", "n_gaps",
            "gap_min_s", "gap_p10_s", "gap_med_s", "gap_mean_s",
            "gap_p90_s", "gap_max_s", "burstiness", "notes"]
    rows, failed, flat = [], 0, 0
    for f in numeric:
        s = res.get(f)
        r = {"field_name": f, "class": classes.get(f, "UNKNOWN"),
             "window_days": used_days.get(f, a.days)}
        if s is None:
            r.update({c: "" for c in cols[3:]})
            r["notes"] = "query failed -- rerun with smaller --chunk"
            failed += 1
        elif not s or s.get("n_gaps", 0) < 1:
            r.update({c: "" for c in cols[3:]})
            r["n_gaps"] = 0
            r["notes"] = "no value changes in window"
            flat += 1
        else:
            med = s.get("gap_med")
            p90 = s.get("gap_p90")
            r.update({
                "n_gaps":     int(s.get("n_gaps", 0)),
                "gap_min_s":  round(s.get("gap_min", 0), 3),
                "gap_p10_s":  round(s.get("gap_p10", 0), 3),
                "gap_med_s":  round(med, 3) if med else "",
                "gap_mean_s": round(s.get("gap_mean", 0), 3),
                "gap_p90_s":  round(p90, 3) if p90 else "",
                "gap_max_s":  round(s.get("gap_max", 0), 3),
                # p90/median: 1 = metronome, large = bursty
                "burstiness": round(p90 / med, 2) if med and p90 else "",
                "notes": "",
            })
        rows.append(r)
    for f in strings:
        r = {c: "" for c in cols}
        r.update({"field_name": f, "class": classes.get(f, "UNKNOWN"),
                  "window_days": a.days,
                  "notes": "non-numeric -- no change statistics"})
        rows.append(r)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: x["field_name"]))

    meds = [float(r["gap_med_s"]) for r in rows if r.get("gap_med_s") not in ("", None)]
    log("")
    log("--- summary " + "-" * 56)
    log(f"  rows            {len(rows)}")
    log(f"  measured        {len(meds)}")
    log(f"  never changed   {flat}")
    log(f"  query failures  {failed}")
    log(f"  non-numeric     {len(strings)}")
    decile_table(meds, "median gap between value changes")

    bursty = [(r["field_name"], float(r["burstiness"])) for r in rows
              if r.get("burstiness") not in ("", None)]
    bursty.sort(key=lambda x: -x[1])
    if bursty:
        log("")
        log("  --- most bursty (p90 / median -- 1.0 = metronome) ---")
        for name, b in bursty[:10]:
            log(f"      {name:<24} {b:>8.1f}x")

    log("")
    log(f"  elapsed  {hms(time.time() - t0)}")
    log("")
    log(f"  Next: read {a.out}, then choose bin edges from the deciles.")
    _logf.close()


if __name__ == "__main__":
    main()
