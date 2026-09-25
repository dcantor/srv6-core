#!/usr/bin/env bash
# The container's front door: the lab's own tools, with the ones that need libvirt refused rather than half-run.
#   srv6 <command> [args...]        (the image's entrypoint; tools/docker.sh passes everything through)
set -euo pipefail
cd /lab 2>/dev/null || { echo "error: mount the srv6-core checkout at /lab (tools/docker.sh does)" >&2; exit 2; }

PY="${SRV6_PYTHON:-python3}"
ON_HOST="up down bootstrap rebuild clean console log"     # these need libvirt, which is the host's job

usage() {
  cat <<U
srv6 tooling container — the repository is mounted at /lab

  inventory                 the lab as JSON (from lab.conf)
  render                    render nodes/<n>/vyos_config.txt, nodes/lg/{frr.conf,lgd.json} from lab.conf
  nautobot <sub> [args]     seed | render [--check|--live|--write] | token
  configure [node...]       push the rendered configuration over SSH
  test [robot args]         the Robot Framework suites (results in results/<timestamp>/)
  verify                    IS-IS / SRv6 / VPNv4 / VRF routes / host ping matrix
  steer <add|del|show> ...  explicit-path SRv6 steering
  lg <sub>                  the BGP looking glass: deploy | status | logs | frr | url
  backup [-m msg]           configurations and routing tables to Gitea
  portal                    the tenant portal on :8091 (publish the port when you run the container)
  python|robot|bash ...     run something directly with the image's interpreter
  version                   the checkout this image was built from, and the versions inside it
  help                      this

  VM lifecycle ($ON_HOST) needs libvirt and stays on the host: run ./lab.sh there.
U
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  help|-h|--help) usage ;;
  version|--version)
    echo "srv6-core tooling image — built from ${SRV6_REVISION:-unknown} at ${SRV6_BUILT:-unknown}"
    echo "  $("$PY" --version), $("${SRV6_ROBOT:-robot}" --version 2>&1 | head -1)"
    "$PY" - <<'P'
import importlib.metadata as m
print("  " + ", ".join(f"{n} {m.version(n)}" for n in ("netmiko", "paramiko", "pynautobot", "requests", "fastapi", "uvicorn")))
P
    echo "  mounted checkout: $(git -C /lab describe --always --dirty 2>/dev/null || echo 'not a git checkout')" ;;
  inventory|verify|configure|steer|backup|nautobot|lg|status) exec ./lab.sh "$cmd" "$@" ;;
  render)   exec "$PY" tools/gen_configs.py "$@" ;;
  test)     exec ./lab.sh test "$@" ;;
  portal)   cd webapp && exec "${SRV6_PYTHON:-python3}" -m uvicorn app:app --host 0.0.0.0 --port "${WEBAPP_PORT:-8091}" ;;
  python)   exec "$PY" "$@" ;;
  robot)    exec "${SRV6_ROBOT:-robot}" "$@" ;;
  bash|sh)  exec bash "$@" ;;
  *)
    for x in $ON_HOST; do
      [[ "$cmd" == "$x" ]] && { echo "error: '$cmd' drives libvirt — run ./lab.sh $cmd on the lab host" >&2; exit 2; }
    done
    echo "error: unknown command '$cmd'" >&2; usage >&2; exit 2 ;;
esac
