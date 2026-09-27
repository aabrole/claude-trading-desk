#!/usr/bin/env bash
# oracle_up.sh
# ============
# Waits for the API key to be registered, then provisions and deploys in one go.
#
# Meant to be started before uploading the key and left alone: it polls until the
# credentials work, then runs oracle_provision.sh (which itself retries for ARM
# capacity) and oracle_deploy.sh. Nothing is created until authentication
# succeeds, so starting this early costs nothing.
#
#   ./deploy/oracle_up.sh
#   WAIT_MINUTES=90 ./deploy/oracle_up.sh
set -euo pipefail
cd "$(dirname "$0")/.."

WAIT_MINUTES="${WAIT_MINUTES:-60}"
export SUPPRESS_LABEL_WARNING=True
export PATH="/opt/homebrew/bin:$PATH"

say(){ printf "\n\033[1m==> %s\033[0m\n" "$*"; }

say "waiting for the API key to be registered (up to $WAIT_MINUTES minutes)"
DEADLINE=$(( $(date +%s) + WAIT_MINUTES * 60 ))
OK=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if REGION="$(oci iam region-subscription list --query 'data[0]."region-name"' --raw-output 2>/dev/null)"; then
    OK=1
    printf "\n    credentials work. region: %s\n" "$REGION"
    break
  fi
  printf "."
  sleep 20
done

if [ -z "$OK" ]; then
  printf "\n\033[31mThe API key was never registered within %s minutes.\033[0m\n" "$WAIT_MINUTES"
  echo "Paste ~/.oci/oci_api_key_public.pem into:"
  echo "  OCI console > Profile > My profile > API keys > Add API key > Paste a public key"
  exit 1
fi

say "provisioning"
./deploy/oracle_provision.sh

say "deploying"
./deploy/oracle_deploy.sh
