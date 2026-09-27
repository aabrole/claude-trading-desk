#!/usr/bin/env bash
# oracle_provision.sh
# ===================
# Creates the Oracle Cloud Always Free instance the bots run on, from the CLI,
# so the only thing done in a browser is signing up and uploading one API key.
#
# Idempotent: every object is looked up by name first and reused if it exists,
# so re-running after a failure continues rather than duplicating. Safe to run
# again after "Out of host capacity", which is the normal ARM experience and the
# reason this script exists: it walks every availability domain on a loop
# instead of making you click Create in the console until one lands.
#
#   ./deploy/oracle_provision.sh
#   RETRY_MINUTES=120 ./deploy/oracle_provision.sh     # keep trying for 2 hours
#
# Creates: a VCN, a public subnet, an internet gateway, a route table, a
# security list opening 22 and 8080, and one VM.Standard.A1.Flex instance with
# 2 OCPUs and 12 GB, which is the whole Always Free ARM allowance as of 2026.
set -euo pipefail

NAME="${NAME:-trading-bots}"
SHAPE="${SHAPE:-VM.Standard.A1.Flex}"
OCPUS="${OCPUS:-2}"
MEM_GB="${MEM_GB:-12}"
BOOT_GB="${BOOT_GB:-50}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/oracle_bots.pub}"
RETRY_MINUTES="${RETRY_MINUTES:-45}"
DASH_PORT="${DASH_PORT:-8080}"
STATE_FILE="${STATE_FILE:-$HOME/.oci/${NAME}-provision.env}"

say(){ printf "\n\033[1m==> %s\033[0m\n" "$*"; }
info(){ printf "    %s\n" "$*"; }
die(){ printf "\n\033[31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }

command -v oci >/dev/null || die "the oci CLI is not installed (brew install oci-cli)"
[ -f "$HOME/.oci/config" ] || die "no ~/.oci/config. Run ./deploy/oracle_configure.sh first."
[ -f "$SSH_KEY" ] || die "no ssh public key at $SSH_KEY"

# --------------------------------------------------------------- identity
say "checking credentials"
TENANCY="$(oci iam compartment list --access-level ANY --include-root \
  --query "data[?\"compartment-id\"==null].id | [0]" --raw-output 2>/dev/null || true)"
[ -n "$TENANCY" ] && [ "$TENANCY" != "null" ] \
  || die "could not call OCI. The API key may not be uploaded yet, or the fingerprint in ~/.oci/config is wrong."
COMPARTMENT="${COMPARTMENT:-$TENANCY}"
REGION="$(oci iam region-subscription list --query 'data[0]."region-name"' --raw-output)"
info "region      $REGION"
info "compartment ${COMPARTMENT:0:22}..."

# Read into an array without mapfile: macOS still ships bash 3.2, and this
# script runs on the laptop, not the VM.
ADS=()
while IFS= read -r line; do
  [ -n "$line" ] && ADS+=("$line")
done < <(oci iam availability-domain list --compartment-id "$COMPARTMENT" \
  --query 'data[].name' --raw-output | tr -d '[]", ' | grep -v '^$')
[ "${#ADS[@]}" -gt 0 ] || die "no availability domains returned"
info "availability domains: ${#ADS[@]}"

# --------------------------------------------------------------- network
say "network"
VCN_ID="$(oci network vcn list --compartment-id "$COMPARTMENT" --display-name "$NAME-vcn" \
  --lifecycle-state AVAILABLE --query 'data[0].id' --raw-output 2>/dev/null || true)"
if [ -z "$VCN_ID" ] || [ "$VCN_ID" = "null" ]; then
  VCN_ID="$(oci network vcn create --compartment-id "$COMPARTMENT" --display-name "$NAME-vcn" \
    --cidr-blocks '["10.0.0.0/16"]' --dns-label "${NAME//-/}" --wait-for-state AVAILABLE \
    --query 'data.id' --raw-output)"
  info "created VCN"
else
  info "reusing VCN"
fi

IG_ID="$(oci network internet-gateway list --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" \
  --query 'data[0].id' --raw-output 2>/dev/null || true)"
if [ -z "$IG_ID" ] || [ "$IG_ID" = "null" ]; then
  IG_ID="$(oci network internet-gateway create --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" \
    --is-enabled true --display-name "$NAME-ig" --wait-for-state AVAILABLE \
    --query 'data.id' --raw-output)"
  info "created internet gateway"
else
  info "reusing internet gateway"
fi

RT_ID="$(oci network vcn get --vcn-id "$VCN_ID" --query 'data."default-route-table-id"' --raw-output)"
oci network route-table update --rt-id "$RT_ID" --force \
  --route-rules "[{\"cidrBlock\":\"0.0.0.0/0\",\"networkEntityId\":\"$IG_ID\"}]" >/dev/null
info "default route points at the internet gateway"

# Both firewalls have to agree. This is the cloud-side one; bootstrap.sh opens
# the instance's own iptables. Opening one and not the other is the single most
# common way this ends up looking broken.
SL_ID="$(oci network vcn get --vcn-id "$VCN_ID" --query 'data."default-security-list-id"' --raw-output)"
oci network security-list update --security-list-id "$SL_ID" --force \
  --egress-security-rules '[{"destination":"0.0.0.0/0","protocol":"all","isStateless":false}]' \
  --ingress-security-rules "[
    {\"source\":\"0.0.0.0/0\",\"protocol\":\"6\",\"isStateless\":false,
     \"tcpOptions\":{\"destinationPortRange\":{\"min\":22,\"max\":22}}},
    {\"source\":\"0.0.0.0/0\",\"protocol\":\"6\",\"isStateless\":false,
     \"tcpOptions\":{\"destinationPortRange\":{\"min\":$DASH_PORT,\"max\":$DASH_PORT}}},
    {\"source\":\"0.0.0.0/0\",\"protocol\":\"1\",\"isStateless\":false,
     \"icmpOptions\":{\"type\":3,\"code\":4}}
  ]" >/dev/null
info "ingress open on 22 and $DASH_PORT"

SUBNET_ID="$(oci network subnet list --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" \
  --display-name "$NAME-subnet" --query 'data[0].id' --raw-output 2>/dev/null || true)"
if [ -z "$SUBNET_ID" ] || [ "$SUBNET_ID" = "null" ]; then
  SUBNET_ID="$(oci network subnet create --compartment-id "$COMPARTMENT" --vcn-id "$VCN_ID" \
    --display-name "$NAME-subnet" --cidr-block "10.0.1.0/24" --dns-label "bots" \
    --prohibit-public-ip-on-vnic false --route-table-id "$RT_ID" --security-list-ids "[\"$SL_ID\"]" \
    --wait-for-state AVAILABLE --query 'data.id' --raw-output)"
  info "created public subnet"
else
  info "reusing subnet"
fi

# --------------------------------------------------------------- image
say "finding the Ubuntu 24.04 ARM image"
IMAGE_ID="$(oci compute image list --compartment-id "$COMPARTMENT" \
  --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
  --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0].id' --raw-output)"
[ -n "$IMAGE_ID" ] && [ "$IMAGE_ID" != "null" ] || die "no Ubuntu 24.04 image found for $SHAPE"
info "image ${IMAGE_ID:0:28}..."

# --------------------------------------------------------------- instance
EXISTING="$(oci compute instance list --compartment-id "$COMPARTMENT" --display-name "$NAME" \
  --lifecycle-state RUNNING --query 'data[0].id' --raw-output 2>/dev/null || true)"
if [ -n "$EXISTING" ] && [ "$EXISTING" != "null" ]; then
  say "an instance named $NAME is already running"
  INSTANCE_ID="$EXISTING"
else
  say "launching $SHAPE with $OCPUS OCPUs and ${MEM_GB}GB"
  info "ARM capacity is scarce on the free tier. Out of host capacity is normal;"
  info "this walks every availability domain and keeps trying for $RETRY_MINUTES minutes."
  DEADLINE=$(( $(date +%s) + RETRY_MINUTES * 60 ))
  INSTANCE_ID=""
  ROUND=0
  while [ -z "$INSTANCE_ID" ] && [ "$(date +%s)" -lt "$DEADLINE" ]; do
    ROUND=$((ROUND+1))
    for AD in "${ADS[@]}"; do
      printf "    round %d, %s ... " "$ROUND" "${AD##*:}"
      set +e
      OUT="$(oci compute instance launch \
        --compartment-id "$COMPARTMENT" --availability-domain "$AD" \
        --display-name "$NAME" --shape "$SHAPE" \
        --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}" \
        --image-id "$IMAGE_ID" --subnet-id "$SUBNET_ID" \
        --assign-public-ip true --boot-volume-size-in-gbs "$BOOT_GB" \
        --ssh-authorized-keys-file "$SSH_KEY" \
        --query 'data.id' --raw-output 2>&1)"
      RC=$?
      set -e
      if [ $RC -eq 0 ]; then
        INSTANCE_ID="$OUT"; printf "launched\n"; break
      fi
      case "$OUT" in
        *"Out of host capacity"*|*"OutOfHostCapacity"*) printf "no capacity\n" ;;
        *LimitExceeded*|*"limit"*) printf "limit reached\n"
          die "Oracle says you are at your service limit. Free tier allows 4 ARM OCPUs total; an older instance may already be using them. Check Compute > Instances." ;;
        *) printf "failed\n"; echo "$OUT" | head -4; ;;
      esac
    done
    [ -z "$INSTANCE_ID" ] && sleep 45 || true
  done
  [ -n "$INSTANCE_ID" ] || die "no ARM capacity after $RETRY_MINUTES minutes. Re-run later, or set RETRY_MINUTES higher and leave it going."
fi

say "waiting for the instance to come up"
# Polled rather than --wait-for-state: that flag belongs to the action and create
# commands, and `instance get` rejects it as a usage error.
for i in $(seq 1 60); do
  STATE="$(oci compute instance get --instance-id "$INSTANCE_ID" \
    --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo PENDING)"
  case "$STATE" in
    RUNNING) info "state RUNNING"; break ;;
    TERMINATED|TERMINATING) die "the instance went to $STATE unexpectedly" ;;
  esac
  [ "$i" = 60 ] && die "instance never reached RUNNING (last state: $STATE)"
  printf "."
  sleep 5
done
VNIC_ID="$(oci compute instance list-vnics --instance-id "$INSTANCE_ID" --query 'data[0].id' --raw-output)"
PUBLIC_IP="$(oci network vnic get --vnic-id "$VNIC_ID" --query 'data."public-ip"' --raw-output)"
[ -n "$PUBLIC_IP" ] && [ "$PUBLIC_IP" != "null" ] || die "instance is running but has no public IP"

cat > "$STATE_FILE" <<EOF
# written by oracle_provision.sh on $(date)
ORACLE_INSTANCE_ID=$INSTANCE_ID
ORACLE_PUBLIC_IP=$PUBLIC_IP
ORACLE_REGION=$REGION
ORACLE_COMPARTMENT=$COMPARTMENT
ORACLE_SSH_KEY=${SSH_KEY%.pub}
EOF

say "done"
info "public IP   $PUBLIC_IP"
info "ssh         ssh -i ${SSH_KEY%.pub} ubuntu@$PUBLIC_IP"
info "saved       $STATE_FILE"
printf "\n    next: ./deploy/oracle_deploy.sh\n\n"
