# The srv6-core tooling in a container: everything the lab is driven *with*, none of what it runs *on*.
#
# The VMs need libvirt and stay on the host (`lab.sh up|down|bootstrap|rebuild|clean`); everything else is Python and
# SSH and can just as well run here — rendering the configurations, seeding Nautobot and checking it still renders the
# same, the Robot Framework suites, the tenant portal, the looking glass's deploy. That makes the toolchain the same
# on a laptop, on the lab host and in CI, and it is the quickest way to run the suites against the lab from somewhere
# that has no Python set up at all.
#
#   podman build -t srv6-tools .            # or: docker build -t srv6-tools .
#   tools/docker.sh inventory               # the wrapper mounts the repo and puts the container on the host network
#
# The repository is mounted at /lab rather than copied in, so the image survives every change to the lab and one build
# serves every checkout.
FROM python:3.13-slim

# ssh: the routers and hosts are reached over it (netmiko / paramiko use their own client, `lab.sh ssh` and the
# Nautobot token fetch use this one). git: the labportal dependency is installed from GitHub and `lab.sh backup`
# commits to Gitea. The rest is for looking around inside the container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git openssh-client ca-certificates curl iputils-ping iproute2 jq less \
    && rm -rf /var/lib/apt/lists/*

# The dependency lists of the repository itself, so the image matches what tests/setup.sh installs on the host.
COPY tests/requirements.txt /tmp/tests-requirements.txt
COPY webapp/requirements.txt /tmp/webapp-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/tests-requirements.txt -r /tmp/webapp-requirements.txt \
    && rm -f /tmp/tests-requirements.txt /tmp/webapp-requirements.txt

# lab.sh and tests/run.sh prefer the host's virtualenv when it is there; inside the container the interpreter is the
# image's own (the mounted tests/.venv belongs to the host and its Python does not exist here).
ENV SRV6_PYTHON=/usr/local/bin/python3 \
    SRV6_ROBOT=/usr/local/bin/robot \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY tools/docker-entrypoint.sh /usr/local/bin/srv6
RUN chmod +x /usr/local/bin/srv6

WORKDIR /lab
ENTRYPOINT ["/usr/local/bin/srv6"]
CMD ["help"]
