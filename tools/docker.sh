#!/usr/bin/env bash
# Run the srv6-core tooling from the container image (podman or docker, whichever is installed).
#   tools/docker.sh build              build / rebuild the image
#   tools/docker.sh <command> [args]   run a tooling command (see `tools/docker.sh help`)
#
# The checkout is mounted at /lab and the container joins the host's network namespace, because every one of these
# tools talks to the lab over the OOB networks (10.3.0.0/24 for this lab, 10.0.0.10 for Nautobot / Gitea / Grafana).
# ~/.ssh is mounted read-only so `nautobot` can fetch its token the way lab.sh does on the host; set NAUTOBOT_TOKEN
# instead if you would rather not share the key.
set -euo pipefail
LAB="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
IMAGE="${SRV6_IMAGE:-srv6-tools}"
ENGINE="${SRV6_CONTAINER_ENGINE:-$(command -v podman || command -v docker)}"
[[ -n "$ENGINE" ]] || { echo "error: neither podman nor docker is installed" >&2; exit 1; }

if [[ "${1:-}" == "build" ]]; then
  shift
  rev="$(git -C "$LAB" describe --always --dirty 2>/dev/null || echo unknown)"
  "$ENGINE" build -t "$IMAGE:latest" -t "$IMAGE:$rev" \
    --build-arg "SRV6_REVISION=$rev" --build-arg "SRV6_BUILT=$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$@" "$LAB"
  echo "built $IMAGE:latest and $IMAGE:$rev"
  exit 0
fi

args=(--rm -i --network host -v "$LAB:/lab:z" -w /lab)
[[ -t 0 && -t 1 ]] && args+=(-t)
[[ -d "$HOME/.ssh" ]] && args+=(-v "$HOME/.ssh:/root/.ssh:ro,z")
for v in NAUTOBOT_URL NAUTOBOT_TOKEN VYOS_USERNAME VYOS_PASSWORD HOST_USERNAME HOST_PASSWORD WEBAPP_PORT; do
  [[ -n "${!v:-}" ]] && args+=(-e "$v=${!v}")
done
exec "$ENGINE" run "${args[@]}" "$IMAGE" "$@"
