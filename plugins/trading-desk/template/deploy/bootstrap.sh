#!/usr/bin/env bash
# Run this ONCE on a fresh Oracle Cloud Always Free instance (Ubuntu 22.04/24.04,
# Ampere A1 / ARM64), as the default `ubuntu` user.
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/bootstrap.sh | bash
#
# or clone first and run ./deploy/bootstrap.sh
set -euo pipefail

REPO_URL="${REPO_URL:-}"
APP_DIR="${APP_DIR:-$HOME/trading-strategies}"

echo "==> architecture: $(uname -m)   (expect aarch64 on Ampere A1)"

echo "==> installing docker"
if ! command -v docker >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl gnupg git
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER"
  echo "    added $USER to the docker group; you may need to log out and back in"
fi

# Oracle's Ubuntu images ship iptables rules that DROP almost everything inbound,
# separately from the cloud-side security list. Most people open the security list,
# see nothing work, and lose an hour here. Opening 8080 for the dashboard API.
echo "==> opening port 8080 on the instance firewall"
sudo iptables -I INPUT 5 -p tcp --dport 8080 -j ACCEPT || true
if command -v netfilter-persistent >/dev/null; then
  sudo netfilter-persistent save || true
else
  sudo apt-get install -y iptables-persistent || true
fi
echo "    NOTE: you must ALSO add an ingress rule for 8080/tcp in the OCI console"
echo "    (Networking > VCN > Subnet > Security List). The instance firewall alone is not enough."

echo "==> swap (12 GB of RAM is plenty, but builds are happier with swap)"
if [ ! -f /swapfile ]; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if [ -n "$REPO_URL" ] && [ ! -d "$APP_DIR" ]; then
  echo "==> cloning $REPO_URL"
  git clone "$REPO_URL" "$APP_DIR"
fi

cat <<'NEXT'

==> bootstrap done. Remaining steps:

  1. cd ~/trading-strategies/deploy
  2. cp .env.example .env   and fill in the three Alpaca key pairs, TYPESAFE_API_KEY
     and DATABENTO_API_KEY
  3. docker compose --env-file .env up -d --build
  4. docker compose ps          # six services should be "running"
  5. docker compose logs -f orb

  If you ran deploy/oracle_deploy.sh from the laptop, all of that is already done.

  The keepalive service must stay running. Oracle stops Always Free instances
  that average under 5% CPU over 24 hours, and bots that sleep between bars
  will trip it.

NEXT
