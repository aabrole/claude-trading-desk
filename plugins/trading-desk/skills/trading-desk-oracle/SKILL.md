---
name: trading-desk-oracle
description: Host the bots and dashboard 24/7 for nothing on Oracle Cloud Always Free - scripted VM provisioning with capacity retry, Docker deploy, and the two firewalls that catch everyone. Use when deploying or debugging the deployment.
version: 1.0.0
tags: [trading, oracle-cloud, docker, deployment, free-tier]
allowed-tools: Read, Grep, Glob, Bash, Write, Edit
---

# Free 24/7 hosting

A laptop is not a host. It sleeps, the lid closes, and the bot misses the open,
which leaves a gap in the record you cannot explain. Oracle Cloud Always Free
gives an ARM VM with no expiry, which is far more than a few Python bots need.

As of 2026 the free ARM allowance is **2 Ampere cores and 12 GB** (it was halved
from 4 and 24 in June 2026, including on existing instances, so older tutorials
promise more than you will get).

## The scripted path

```bash
./deploy/oracle_configure.sh     # record who your API key belongs to
./deploy/oracle_up.sh            # waits for the key, then provisions and deploys
```

`oracle_up.sh` can be started **before** you upload the API key. It polls until
the credentials work, so nothing is created until authentication succeeds and
starting early costs nothing.

Individually: `oracle_provision.sh` then `oracle_deploy.sh`, with
`oracle_logs.sh <service>` to follow a bot from your laptop.

## What you have to do in a browser

Only this, because it cannot be automated:

1. Sign up at cloud.oracle.com. A card is required for identity and is not
   charged. Your home region is fixed at signup, so pick one you can live with.
2. Profile > My profile > API keys > Add API key > **Paste a public key**, and
   paste `~/.oci/oci_api_key_public.pem`.
3. Copy the User OCID, Tenancy OCID and region into `oracle_configure.sh`.

Note the account opens as a 30-day trial that downgrades to Always Free, so mark
resources Always Free eligible or you will build something that expires.

## Out of host capacity is normal

Free ARM capacity is genuinely scarce in popular regions. This error is not a
mistake on your part. `oracle_provision.sh` walks every availability domain on a
loop for `RETRY_MINUTES`, so leave it running rather than clicking Create by hand.

Do not escape to an x86 shape: the free x86 allowance is two 1 GB VMs and will
struggle with pandas.

## There are two firewalls and you must open both

This is the single most common way a working deployment looks broken.

1. **The instance**, whose Ubuntu image ships iptables rules that drop almost all
   inbound traffic. `bootstrap.sh` handles this.
2. **The cloud-side Security List**, in the VCN. `oracle_provision.sh` handles
   this for a VCN it creates.

If the container is healthy and the page will not load from outside, check in this
order: `curl localhost:8080/healthz` on the VM, then the Security List, then
`sudo iptables -L INPUT -n`.

## Ship the committed tree, not the working tree

`oracle_deploy.sh` sends `git archive HEAD`, about 1 MB. A research repo
accumulates gigabytes of data caches that nothing live needs, because each bot
fetches its own data at runtime. It warns you when uncommitted work is therefore
not going out.

Secrets go by `scp` at mode 600, never baked into an image.

## Verify, because "Started" is not "running"

A container that starts and dies immediately still reports `Started`, so the stack
looks healthy for several seconds. `oracle_deploy.sh` waits, then checks restart
counts and fails with the logs of anything looping.

Two real crash-loops this caught on a first deployment, both invisible in
`docker compose ps` for the first few seconds:

- a bot passed a flag its script did not define, so argparse exited 2
- a bot imported a package nobody had added to `requirements.txt`

Always check restart counts before believing a deploy succeeded.

## Keep the instance alive

Oracle reclaims Always Free instances averaging under 5% CPU over 24 hours, and
bots that sleep between bars will trip that. The `keepalive` service exists for
this and must stay running. Confirm the instance is still up after a day rather
than assuming.

## Cost

The VM is free. The data and decision APIs for a few bots run to a couple of
dollars a month. There is no paid tier anywhere in this stack.
