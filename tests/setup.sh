#!/usr/bin/env bash
# Create the Python virtualenv used by run.sh (Robot Framework + device libraries).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
[[ -d "$(dirname "$0")/../../lab-portal" ]] && .venv/bin/pip install -q -e "$(dirname "$0")/../../lab-portal"   # labportal: Grafana annotations from the suites
.venv/bin/robot --version || true
