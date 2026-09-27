#!/usr/bin/env bash
# oracle_configure.sh
# ===================
# Writes ~/.oci/config from the four values Oracle shows you after you upload
# the API public key. The keypair itself already exists; this only records who
# it belongs to.
#
# Pass them as environment variables, or run with no arguments to be prompted:
#
#   OCI_USER=ocid1.user...  OCI_TENANCY=ocid1.tenancy...  \
#   OCI_REGION=us-ashburn-1 OCI_FINGERPRINT=aa:bb:...     ./deploy/oracle_configure.sh
#
# The fingerprint is shown in the console next to the key you uploaded, and is
# also printed by this script from the local key so you can check they match.
set -euo pipefail

KEY="$HOME/.oci/oci_api_key.pem"
CONFIG="$HOME/.oci/config"
[ -f "$KEY" ] || { echo "ERROR: no API key at $KEY" >&2; exit 1; }

LOCAL_FP="$(openssl rsa -pubout -outform DER -in "$KEY" 2>/dev/null | openssl md5 -c | awk '{print $NF}')"

ask(){ # ask <varname> <prompt>
  local cur="${!1:-}"
  if [ -z "$cur" ]; then
    printf "%s" "$2" >&2
    read -r cur
  fi
  printf "%s" "$cur"
}

OCI_USER="$(ask OCI_USER        'User OCID     (Profile > My profile > OCID): ')"
OCI_TENANCY="$(ask OCI_TENANCY  'Tenancy OCID  (Profile > Tenancy > OCID):    ')"
OCI_REGION="$(ask OCI_REGION    'Region        (e.g. us-ashburn-1):           ')"
OCI_FINGERPRINT="${OCI_FINGERPRINT:-$LOCAL_FP}"

for v in OCI_USER OCI_TENANCY OCI_REGION; do
  [ -n "${!v}" ] || { echo "ERROR: $v is required" >&2; exit 1; }
done
case "$OCI_USER" in ocid1.user.*) ;; *) echo "ERROR: user OCID should start with ocid1.user." >&2; exit 1 ;; esac
case "$OCI_TENANCY" in ocid1.tenancy.*) ;; *) echo "ERROR: tenancy OCID should start with ocid1.tenancy." >&2; exit 1 ;; esac

if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$CONFIG.bak.$(date +%s)"
  echo "    existing config backed up"
fi

umask 077
cat > "$CONFIG" <<EOF
[DEFAULT]
user=$OCI_USER
fingerprint=$OCI_FINGERPRINT
tenancy=$OCI_TENANCY
region=$OCI_REGION
key_file=$KEY
EOF
chmod 600 "$CONFIG"

echo "    wrote $CONFIG"
echo "    fingerprint recorded: $OCI_FINGERPRINT"
[ "$OCI_FINGERPRINT" = "$LOCAL_FP" ] || echo "    WARNING: that does not match the local key ($LOCAL_FP)"

echo
echo "==> testing the credentials"
if oci iam region-subscription list --query 'data[0]."region-name"' --raw-output 2>/dev/null; then
  echo "    credentials work."
  echo "    next: ./deploy/oracle_provision.sh"
else
  echo "    the call failed. Usual causes, in order of likelihood:"
  echo "      1. the public key has not finished registering (wait a minute, retry)"
  echo "      2. the fingerprint does not match the uploaded key"
  echo "      3. the region is not one your tenancy is subscribed to"
  exit 1
fi
