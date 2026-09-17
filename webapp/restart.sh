#!/usr/bin/env bash
# Restart the portal's systemd user unit — refused while a run is in progress (a restart would interrupt it).
set -euo pipefail
if curl -sf http://127.0.0.1:8091/api/runs 2>/dev/null | python3 -c 'import sys,json; sys.exit(0 if any(r["status"] in ("running","queued") for r in json.load(sys.stdin)) else 1)'; then
  echo "a run is in progress — not restarting" >&2; exit 1
fi
systemctl --user restart srv6-webapp && sleep 2 && systemctl --user is-active srv6-webapp
