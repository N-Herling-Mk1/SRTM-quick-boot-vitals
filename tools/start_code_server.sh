#!/usr/bin/env bash
# code-server on bigmem3 -- browser VS Code over the SSH tunnel.
#
#   local:  ssh -L 8080:localhost:8080 -L 5055:localhost:5055 \
#                naherlin@eepp-bigmem3.physics.arizona.edu
#   then:   http://localhost:8080
#
# Installed by tarball, NOT on PATH -- a bare `code-server` fails.
# No service unit: this does not survive a reboot. Re-run it after one.
# Password prompt at localhost:8080 -- read it with:
#   grep -E '^(password|hashed-password|auth):' ~/.config/code-server/config.yaml
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:$PATH
BIN="$HOME/code-server/bin/code-server"
[ -x "$BIN" ] || { echo "FAIL not found: $BIN" >&2; exit 1; }
if ss -ltn | grep -q ':8080'; then echo "already listening on 8080"; exit 0; fi
mkdir -p ~/logs
nohup "$BIN" --bind-addr 127.0.0.1:8080 > ~/logs/code-server.out 2>&1 &
disown
sleep 4
ss -ltn | grep -q ':8080' && echo "OK  http://localhost:8080 via tunnel" \
  || { echo "FAIL -- see ~/logs/code-server.out" >&2; exit 1; }
