#!/usr/bin/env bash
#
# Build the runner for linux/amd64 and ship it to the VM.
#
#   ./deploy.sh root@your-box
#
# Cross-compiled locally rather than built on the box, so the VM never needs a
# Go toolchain and the thing that ships is the thing that was tested.

set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage: ./deploy.sh user@host" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "→ building runner (linux/amd64)"
cd "$REPO_ROOT/runner"
# CGO off for a static binary: the VM's glibc version stops being something we
# have to care about.
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
  -trimpath \
  -ldflags="-s -w -X main.version=$(git rev-parse --short HEAD)" \
  -o "$BUILD_DIR/runner" ./cmd/runner

echo "  $(du -h "$BUILD_DIR/runner" | cut -f1)"

echo "→ uploading"
# To a temp path first, then move into place. Writing directly over a running
# binary gives "text file busy"; the move is atomic.
scp -q "$BUILD_DIR/runner" "$TARGET:/tmp/runner.new"

echo "→ installing and restarting"
ssh "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
install -o runbox -g runbox -m 0755 /tmp/runner.new /opt/runbox/runner
rm -f /tmp/runner.new
systemctl restart runbox-runner
sleep 2
systemctl is-active --quiet runbox-runner \
  && echo "  runbox-runner is active" \
  || { echo "  failed to start:"; journalctl -u runbox-runner -n 30 --no-pager; exit 1; }
REMOTE

echo "Done. Logs: ssh $TARGET journalctl -u runbox-runner -f"
