#!/usr/bin/env bash
# Boot the vitals page. Run ON bigmem3 -- InfluxDB is bound to that host.
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
  echo "[1/3] loading .env"
  set -a; . ./.env; set +a
else
  echo "[1/3] no .env found -- falling back to Cheu's stack env"
  if [ -f "$HOME/srtm-TIG/.env" ]; then
    set -a; . "$HOME/srtm-TIG/.env"; set +a
    export INFLUX_URL="${INFLUX_URL:-http://127.0.0.1:8096}"
  else
    echo "      FAIL: no .env anywhere. Copy .env.example to .env." >&2
    exit 1
  fi
fi

echo "[2/3] checking influxdb at ${INFLUX_URL:-http://127.0.0.1:8096}"
curl -sf "${INFLUX_URL:-http://127.0.0.1:8096}/ping" >/dev/null \
  && echo "      OK   influxdb answering" \
  || { echo "      FAIL influxdb not answering -- is the TIG stack up?" >&2; exit 1; }

echo "[3/3] starting flask on 127.0.0.1:5055"
exec python3 app.py
