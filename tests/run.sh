#!/usr/bin/env bash
# Run the Robot Framework test suite against the lab.
#   tests/run.sh [robot options...]      e.g.  tests/run.sh --exclude internet
# Every run gets its own folder named by date and time: results/YYYY-MM-DD_HH-MM-SS/
#   configs/pre-run/    running configuration of every VyOS node before the tests (+ routes/: RIBs per VRF, kernel SRv6
#                       routes, BGP VPNv4, IS-IS SRv6, BFD — for the record)
#   configs/post-run/   the same, captured again after the tests (the backup of record)
#   configs/pre-vs-post.diff   what the run changed in the configurations (empty = nothing; the routing tables are not diffed)
#   log.html, report.html, output.xml   Robot Framework results
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ -x .venv/bin/robot ]] || { echo "error: run tests/setup.sh first" >&2; exit 1; }

ts="$(date +%Y-%m-%d_%H-%M-%S)"
out="$(cd .. && pwd)/results/$ts"
mkdir -p "$out/configs"
echo "==> results: $out"

echo "==> capturing VyOS configurations and routing tables (pre-run)"
.venv/bin/python capture_configs.py "$out/configs/pre-run" || echo "warning: config capture failed" >&2

echo "==> running Robot Framework suites"
.venv/bin/robot --outputdir "$out" --name "srv6 core lab" --loglevel INFO "$@" suites/
rc=$?

echo "==> capturing VyOS configurations and routing tables (post-run backup)"
.venv/bin/python capture_configs.py "$out/configs/post-run" || echo "warning: config capture failed" >&2
# diff ignoring the capture-timestamp header line
diff -ru -x routes -I '^# .* captured ' "$out/configs/pre-run" "$out/configs/post-run" > "$out/configs/pre-vs-post.diff" \
  && echo "    no configuration changes during the run" \
  || echo "    configuration changed during the run, see configs/pre-vs-post.diff"

ln -sfn "$(basename "$out")" ../results/latest
echo "==> report: $out/report.html  (rc=$rc)"
exit $rc
