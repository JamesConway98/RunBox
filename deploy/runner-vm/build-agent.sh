#!/usr/bin/env bash
#
# Build the sandbox image on the runner box itself.
#
#   ssh root@your-box 'bash /opt/runbox/src/deploy/runner-vm/build-agent.sh'
#
# There is no registry in this path, and that is deliberate for a single-runner
# deployment. The only consumer of this image is the Docker daemon on this
# machine, so pushing it to GHCR and pulling it straight back would add an
# account, a credential and a network round trip to move a file to itself.
#
# What that costs is the ability to pin by registry digest. The build still
# produces an immutable image ID, which this prints, and that answers the same
# question — "which code actually ran" — for a host that builds its own images.
#
# Add a registry when there is a second runner. Not before.

set -euo pipefail

SRC_DIR=${RUNBOX_SRC:-/opt/runbox/src}
REPO=${RUNBOX_REPO:-https://github.com/JamesConway98/RunBox.git}

if [ ! -d "$SRC_DIR/.git" ]; then
  echo "→ cloning $REPO"
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone --depth 50 "$REPO" "$SRC_DIR"
else
  echo "→ updating source"
  git -C "$SRC_DIR" fetch --quiet origin
  git -C "$SRC_DIR" reset --hard --quiet origin/main
fi

SHA=$(git -C "$SRC_DIR" rev-parse --short HEAD)
TAG="runbox/agent:$SHA"

echo "→ building $TAG"
docker build --quiet -t "$TAG" -t runbox/agent:latest "$SRC_DIR/agent" >/dev/null
echo "  built"

# The image ID, not a RepoDigest. A locally built image has no registry digest
# until it is pushed, but the ID is just as immutable and just as specific.
IMAGE_ID=$(docker image inspect --format='{{.Id}}' "$TAG")

echo
echo "Built $TAG"
echo
echo "Set this in /etc/runbox/runner.env:"
echo "  RUNBOX_AGENT_IMAGE=$IMAGE_ID"
echo
echo "Then: systemctl restart runbox-runner"

# Old images accumulate one per deploy and each is a few hundred MB. On a 40GB
# disk that is a slow-motion outage, so prune anything untagged.
docker image prune -f >/dev/null 2>&1 || true
