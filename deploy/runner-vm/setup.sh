#!/usr/bin/env bash
#
# Provision a fresh Debian 12 / Ubuntu 24.04 box to run the Runbox runner.
#
#   scp -r deploy/runner-vm root@your-box:/tmp/
#   ssh root@your-box 'bash /tmp/runner-vm/setup.sh'
#
# Idempotent: safe to re-run after a change. It installs Docker, creates an
# unprivileged service account, and lays down the systemd unit — it does not
# install the runner binary or the secrets, because those come from a build and
# from you respectively. `deploy.sh` handles the binary.

set -euo pipefail

RUNBOX_USER=runbox
INSTALL_DIR=/opt/runbox
CONFIG_DIR=/etc/runbox
SOCKET_DIR=/var/run/runbox-sockets
SRC_DIR=/opt/runbox/src
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root." >&2
  exit 1
fi

echo "→ installing git"
if ! command -v git >/dev/null; then
  apt-get update -qq && apt-get install -y -qq git
fi

echo "→ installing docker"
if ! command -v docker >/dev/null; then
  # The convenience script rather than the distro package: Debian's docker.io
  # lags far enough behind that the API version negotiation in the runner has
  # occasionally had to fall back.
  curl -fsSL https://get.docker.com | sh
else
  echo "  already installed: $(docker --version)"
fi
systemctl enable --now docker

echo "→ creating service account"
if ! id "$RUNBOX_USER" >/dev/null 2>&1; then
  # No login shell and no home. This account exists to own one process.
  useradd --system --no-create-home --shell /usr/sbin/nologin "$RUNBOX_USER"
fi
# Membership of the docker group is effectively root on this host. That is
# unavoidable — creating containers is the runner's entire job — and it is why
# this box should run nothing else.
usermod -aG docker "$RUNBOX_USER"

echo "→ creating directories"
install -d -o "$RUNBOX_USER" -g "$RUNBOX_USER" -m 0755 "$INSTALL_DIR"
install -d -o root -g "$RUNBOX_USER" -m 0750 "$CONFIG_DIR"
# 0700 on the socket parent: the per-run directories inside hold sockets that
# are world-writable by necessity, and this is what keeps anything else on the
# host from reaching them.
install -d -o "$RUNBOX_USER" -g "$RUNBOX_USER" -m 0700 "$SOCKET_DIR"

# The socket dir lives under /var/run, which is a tmpfs on most distros and is
# therefore empty after a reboot. Recreate it on boot so the first run after a
# restart does not fail.
cat >/etc/tmpfiles.d/runbox.conf <<EOF
d $SOCKET_DIR 0700 $RUNBOX_USER $RUNBOX_USER -
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/runbox.conf

echo "→ fetching source for the agent image build"
# The agent image is built here rather than pulled from a registry. The only
# consumer is this machine's Docker daemon, so a registry would move a file to
# itself via the internet.
bash "$SCRIPT_DIR/build-agent.sh" || echo "  (build-agent.sh failed — run it manually later)"

echo "→ installing systemd unit"
cp "$SCRIPT_DIR/runbox-runner.service" /etc/systemd/system/
systemctl daemon-reload

if [ ! -f "$CONFIG_DIR/runner.env" ]; then
  cp "$SCRIPT_DIR/runner.env.example" "$CONFIG_DIR/runner.env"
  chown root:"$RUNBOX_USER" "$CONFIG_DIR/runner.env"
  chmod 0640 "$CONFIG_DIR/runner.env"
  echo
  echo "  Wrote $CONFIG_DIR/runner.env from the example. Fill it in before starting:"
  echo "    DATABASE_URL, REDIS_URL, ANTHROPIC_API_KEY, RUNBOX_AGENT_IMAGE"
fi

echo
echo "Done. Next:"
echo "  1. edit $CONFIG_DIR/runner.env — the RUNBOX_AGENT_IMAGE id is printed above"
echo "  2. ./deploy.sh root@this-box   (from your laptop — builds and ships the binary)"
echo "  3. systemctl start runbox-runner"
