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
# the host runs the suites out of tests/.venv; SRV6_ROBOT / SRV6_PYTHON let the container use its own installation
ROBOT="${SRV6_ROBOT:-$PWD/.venv/bin/robot}"; PY="${SRV6_PYTHON:-$PWD/.venv/bin/python}"
[[ -x "$ROBOT" ]] || { echo "error: run tests/setup.sh first (or set SRV6_ROBOT)" >&2; exit 1; }
# one run at a time on this host (manual, portal, CI): the suites cut links and shut sessions, two runs would fail each other
exec 9>/tmp/srv6-core-test.lock
flock -n 9 || { echo "==> another test run holds /tmp/srv6-core-test.lock — waiting for it"; flock 9; }

ts="$(date +%Y-%m-%d_%H-%M-%S)"
out="$(cd .. && pwd)/results/$ts"
mkdir -p "$out/configs"
echo "==> results: $out"

echo "==> capturing VyOS configurations and routing tables (pre-run)"
"$PY" capture_configs.py "$out/configs/pre-run" || echo "warning: config capture failed" >&2

echo "==> running Robot Framework suites"
# explicit suite files on the command line replace the default "every suite"
args=("$@"); explicit=0; for x in "$@"; do [[ "$x" == *.robot ]] && explicit=1; done; [[ $explicit -eq 1 ]] || args+=(suites/)
"$ROBOT" --outputdir "$out" --name "srv6 core lab" --loglevel INFO "${args[@]}"
rc=$?

echo "==> capturing VyOS configurations and routing tables (post-run backup)"
"$PY" capture_configs.py "$out/configs/post-run" || echo "warning: config capture failed" >&2
# diff ignoring the capture-timestamp header line
diff -ru -x routes -I '^# .* captured ' "$out/configs/pre-run" "$out/configs/post-run" > "$out/configs/pre-vs-post.diff" \
  && echo "    no configuration changes during the run" \
  || echo "    configuration changed during the run, see configs/pre-vs-post.diff"

ln -sfn "$(basename "$out")" ../results/latest
echo "==> report: $out/report.html  (rc=$rc)"
exit $rc
