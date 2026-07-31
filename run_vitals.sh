#!/usr/bin/env bash
# srtm-quickboot-vitals -- boot the page. Run ON bigmem3; InfluxDB is bound
# to that host and 127.0.0.x only exists there.
#
# PYTHON ON BIGMEM3: the login shell sources the PetaLinux/Vitis SDK, which
# puts an SDK python3.10 ahead of the system python and can drop coreutils
# off PATH entirely. Packages pip installs there are invisible to the
# interpreter that actually runs, so `pip3 install` succeeds and `import
# requests` still fails. This script never trusts a bare `python3`.
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/4] selecting interpreter"
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"; echo "      OK   venv  $PY"
elif [ -x "/usr/bin/python3" ]; then
  PY="/usr/bin/python3"
  echo "      WARN no .venv -- using $PY"
  echo "           recommended:  /usr/bin/python3 -m venv .venv"
  echo "                         .venv/bin/pip install -r requirements.txt"
else
  echo "      FAIL no usable python found" >&2; exit 1
fi

case "$($PY -c 'import sys; print(sys.executable)')" in
  *petalinux*|*sysroots*)
    echo "      FAIL interpreter is inside the PetaLinux SDK sysroot." >&2
    echo "           /usr/bin/python3 -m venv .venv" >&2
    echo "           .venv/bin/pip install -r requirements.txt" >&2
    exit 1 ;;
esac

if ! $PY -c "import flask, requests" 2>/dev/null; then
  echo "      FAIL flask/requests not importable for $PY" >&2
  echo "           .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
echo "      OK   flask + requests importable"

echo "[2/4] environment"
if [ -f .env ]; then
  set -a; . ./.env; set +a
  echo "      OK   .env"
elif [ -f "$HOME/srtm-TIG/.env" ]; then
  set -a; . "$HOME/srtm-TIG/.env"; set +a
  export INFLUX_URL="${INFLUX_URL:-http://127.0.0.1:8096}"
  echo "      WARN no .env here -- borrowing ~/srtm-TIG/.env"
  echo "           that token is admin-scoped; make a read-only one"
else
  echo "      FAIL no .env. Run: cp .env.example .env  and fill in the token" >&2
  exit 1
fi

URL="${INFLUX_URL:-http://127.0.0.1:8096}"
echo "[3/4] influxdb at $URL"
if curl -sf "$URL/ping" >/dev/null; then
  echo "      OK   answering"
else
  echo "      FAIL not answering -- is the TIG stack up?  docker ps" >&2
  exit 1
fi

echo "[4/4] serving on 127.0.0.1:5055"
exec "$PY" app.py
