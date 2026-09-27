#!/usr/bin/env bash
# oracle_deploy.sh
# ================
# Ships the committed tree to the Oracle instance and starts everything. Run it
# again any time to redeploy: it is a fresh copy of HEAD each time, so there is
# no drift between what is committed here and what is running there.
#
#   ./deploy/oracle_deploy.sh
#   IP=1.2.3.4 ./deploy/oracle_deploy.sh        # override the saved IP
#
# Sends `git archive HEAD`, about 1 MB, rather than the working tree: the caches
# in this repo are over a gigabyte and none of them are needed live. Every bot
# fetches its own data at runtime.
set -euo pipefail

NAME="${NAME:-trading-bots}"
STATE_FILE="${STATE_FILE:-$HOME/.oci/${NAME}-provision.env}"
APP_DIR="trading-strategies"
DASH_PORT="${DASH_PORT:-8080}"

say(){ printf "\n\033[1m==> %s\033[0m\n" "$*"; }
info(){ printf "    %s\n" "$*"; }
die(){ printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

if [ -f "$STATE_FILE" ]; then . "$STATE_FILE"; fi
IP="${IP:-${ORACLE_PUBLIC_IP:-}}"
KEY="${KEY:-${ORACLE_SSH_KEY:-$HOME/.ssh/oracle_bots}}"
[ -n "$IP" ] || die "no IP. Run ./deploy/oracle_provision.sh first, or pass IP=<address>."
[ -f "$KEY" ] || die "no ssh private key at $KEY"

SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ubuntu@$IP"

say "waiting for ssh on $IP"
# A fresh instance takes a minute or two to finish first boot.
for i in $(seq 1 60); do
  if $SSH true 2>/dev/null; then info "ssh is up"; break; fi
  [ "$i" = 60 ] && die "ssh never came up. The instance may still be booting, or the security list is not open on 22."
  printf "."; sleep 5
done

say "checking what is already there"
ARCH="$($SSH 'uname -m')"
info "architecture $ARCH"
[ "$ARCH" = "aarch64" ] || info "WARNING: expected aarch64 on Ampere A1"

say "shipping HEAD ($(git rev-parse --short HEAD))"
git diff --quiet || info "NOTE: you have uncommitted changes; they will NOT be deployed"
$SSH "mkdir -p ~/$APP_DIR"
git archive HEAD | $SSH "tar x -C ~/$APP_DIR"
info "$(git ls-files | wc -l | tr -d ' ') files sent"

say "installing docker and opening the instance firewall"
$SSH "cd ~/$APP_DIR && chmod +x deploy/*.sh && ./deploy/bootstrap.sh" 2>&1 | sed 's/^/    /'

say "sending the secrets"
[ -f deploy/.env ] || die "deploy/.env does not exist. Copy deploy/.env.example and fill it in."
# scp rather than baking them into the image, and 600 on the far side.
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new deploy/.env "ubuntu@$IP:~/$APP_DIR/deploy/.env"
$SSH "chmod 600 ~/$APP_DIR/deploy/.env"
info "deploy/.env copied, mode 600"

say "building and starting the stack"
# sg docker, because the usermod from bootstrap.sh has not taken effect in this
# session yet and would otherwise need a logout.
$SSH "cd ~/$APP_DIR/deploy && sg docker -c 'docker compose --env-file .env up -d --build'" 2>&1 | tail -25 | sed 's/^/    /'

say "what is running"
$SSH "cd ~/$APP_DIR/deploy && sg docker -c 'docker compose ps'" | sed 's/^/    /'

# A container that starts and immediately dies still reports "Started", so the
# stack looks healthy for a few seconds. Give them time to fall over, then check
# restart counts: a bot with a bad argument or a missing import shows up here and
# nowhere else. Without this the deploy reports success over a crash-loop.
say "checking nothing is crash-looping"
sleep 25
BAD="$($SSH "sg docker -c 'docker inspect --format=\"{{.Name}} {{.RestartCount}}\" orb insider pead lvl dashboard keepalive'" \
  | awk '$2 > 1 {print $1" restarted "$2" times"}')"
if [ -n "$BAD" ]; then
  printf "\n\033[31m    these are restarting in a loop:\033[0m\n"
  echo "$BAD" | sed 's/^/      /'
  echo
  for SVC in $(echo "$BAD" | sed 's#^/##' | awk '{print $1}'); do
    printf "    --- %s ---\n" "$SVC"
    $SSH "cd ~/$APP_DIR/deploy && sg docker -c 'docker compose logs --tail=12 $SVC'" 2>&1 \
      | tail -12 | sed 's/^/      /'
  done
  die "fix those before relying on this deployment"
fi
info "no crash-loops"

say "checking the dashboard answers from the internet"
OK=""
for i in $(seq 1 20); do
  if curl -fsS --max-time 5 "http://$IP:$DASH_PORT/healthz" >/dev/null 2>&1; then OK=1; break; fi
  printf "."; sleep 3
done
echo
if [ -n "$OK" ]; then
  curl -s --max-time 5 "http://$IP:$DASH_PORT/healthz" | sed 's/^/    /'
  printf "\n\033[1m    the desk is live: http://%s:%s\033[0m\n\n" "$IP" "$DASH_PORT"
else
  info "the dashboard did not answer from out here."
  info "It is probably running fine and blocked by a firewall. Check, in this order:"
  info "  1. on the VM:  curl -s localhost:$DASH_PORT/healthz"
  info "     if that works, the container is fine and the problem is a firewall"
  info "  2. the OCI Security List needs TCP $DASH_PORT open to 0.0.0.0/0"
  info "     (oracle_provision.sh does this, but a pre-existing VCN may differ)"
  info "  3. on the VM:  sudo iptables -L INPUT -n --line-numbers | head"
  exit 1
fi

cat <<NEXT
    useful from here:

      ./deploy/oracle_logs.sh orb          follow one bot
      ./deploy/oracle_deploy.sh            redeploy after committing
      ssh -i $KEY ubuntu@$IP

NEXT
