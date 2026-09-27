# Deploying the bots on Oracle Cloud Always Free

What you get for nothing: **2 ARM Ampere cores, 12 GB RAM, 200 GB storage,
10 TB egress a month**, with no expiry. That is far more than four Python bots
need. Oracle halved this tier in June 2026 (it was 4 cores and 24 GB), and the
halving was applied to existing instances, so do not be surprised if older
tutorials promise more.

Four things about Oracle specifically will cost you an hour each if nobody warns
you. All four are handled below.

---

## The short version: three scripts

Everything except signing up and uploading one key is scripted. From the repo on
your laptop:

```bash
./deploy/oracle_configure.sh     # records who your API key belongs to
./deploy/oracle_up.sh            # waits for the key, then provisions and deploys
```

`oracle_up.sh` is the hands-off version: start it before you upload the API key
and leave it. It polls until the credentials work, then runs the two steps below
in order. Nothing is created until authentication succeeds, so starting it early
costs nothing. The steps individually, if you would rather drive:

```bash
./deploy/oracle_provision.sh     # builds the network and the VM, retries for capacity
./deploy/oracle_deploy.sh        # ships HEAD, installs docker, starts everything
```

`oracle_provision.sh` creates the VCN, a public subnet, an internet gateway, the
route, **both** ingress rules (22 and 8080), and the instance itself. It walks
every availability domain on a loop, so "Out of host capacity" is retried rather
than something you sit and click through.

`oracle_deploy.sh` sends `git archive HEAD`, about 1 MB, not the working tree:
the caches here are over a gigabyte and nothing live needs them. Re-run it any
time to redeploy, and it will tell you if you have uncommitted changes that are
therefore not going out.

What you still have to do by hand, because it needs a browser:

1. Sign up at cloud.oracle.com (card for identity check, not charged).
2. Profile > My profile > API keys > Add API key > **Paste a public key**, and
   paste `~/.oci/oci_api_key_public.pem`.
3. Copy the User OCID, Tenancy OCID and region into `oracle_configure.sh`.

Then `./deploy/oracle_logs.sh orb` follows a bot from your laptop.

The rest of this file is the manual route, and explains what the scripts are
doing and every place Oracle bites.

---

## 1. Create the instance (manual route)

1. Sign up at cloud.oracle.com. It asks for a card for identity verification and
   does not charge it, but you must choose **Always Free** resources explicitly:
   the account starts as a 30-day trial and downgrades to Always Free after,
   keeping anything marked Always Free eligible.
2. Compute > Instances > Create Instance.
3. Image: **Ubuntu 24.04** (or 22.04). Shape: **VM.Standard.A1.Flex**, then set
   **2 OCPU and 12 GB** so the whole free allowance sits in one machine.
4. Add your SSH public key. If you do not have one:
   `ssh-keygen -t ed25519 -C "oracle"` and paste `~/.ssh/id_ed25519.pub`.
5. Create.

**Gotcha one: "Out of host capacity."** ARM capacity is genuinely scarce in
popular regions and this error is normal, not a mistake on your part. Options, in
order of effort: try a different availability domain in the same region, try
again a few hours later, or pick a quieter home region (your home region is fixed
at signup, so choose one you are willing to live in). `deploy/oracle_provision.sh` does exactly
that retry loop for you. Do not switch to an x86 shape to escape it: the free
x86 allowance is two tiny 1 GB VMs and will struggle with pandas.

---

## 2. Bootstrap it

```bash
ssh ubuntu@<your-instance-public-ip>
git clone <your repo> ~/trading-strategies
cd ~/trading-strategies && ./deploy/bootstrap.sh
```

That installs Docker for ARM64, adds swap, and opens port 8080 on the instance
firewall.

**Gotcha two: there are two firewalls.** Oracle's Ubuntu images ship iptables
rules that drop almost all inbound traffic, and that is separate from the
cloud-side Security List. Opening one and not the other is the single most common
Oracle support question. `bootstrap.sh` handles the instance side; you must still
add the ingress rule in the console:

> Networking > Virtual Cloud Networks > your VCN > Subnets > your subnet >
> Security Lists > Default Security List > Add Ingress Rule
> Source `0.0.0.0/0`, protocol TCP, destination port `8080`.

---

## 3. Configure and start

```bash
cd ~/trading-strategies/deploy
cp .env.example .env
nano .env          # three Alpaca key pairs, plus TYPESAFE_API_KEY
docker compose --env-file .env up -d --build
docker compose ps
docker compose logs -f orb
```

**Three Alpaca accounts.** One paper account per Alpaca login, so a real
three-way leaderboard needs three signups with three email addresses. Each gets
its own $10,000 and its own equity curve. The alternative, one account with
orders tagged per strategy, makes the leaderboard meaningless because the curves
are not independent.

First build takes about five minutes on two ARM cores. Everything installs from
arm64 wheels, so there is no compiler step.

---

## 4. Keep it alive

**Gotcha three: Oracle stops idle Always Free instances.** Under 5% average CPU
across 24 hours and the instance is reclaimed, usually overnight. Three bots that
wake once every five minutes and sleep otherwise will absolutely trip this.

The `keepalive` service in the compose file holds roughly 8% of one core and does
nothing else. Leave it running. Verify with:

```bash
docker stats --no-stream
uptime
```

If you would rather not burn CPU, the alternative is to disable idle detection on
the tenancy, which Oracle allows for paid accounts but not reliably for free
ones. Burning 8% of a free core is the cheaper trade.

---

## 5. Operating it

```bash
docker compose logs -f --tail=100          # all bots
docker compose logs -f insider              # one bot
docker compose restart pead               # restart one
docker compose down                        # stop everything
docker compose up -d --build               # after a git pull

cat /var/lib/docker/volumes/deploy_state/_data/orb.json | python3 -m json.tool
```

Every bot writes `state/<bot>.json` (a snapshot) and `state/<bot>.events.jsonl`
(an append-only feed). Those two files are the entire dashboard contract.

**Gotcha four: the clock.** The containers are pinned to `America/New_York` via
`TZ`, because every strategy in this repo reasons in Eastern time. Do not change
the host timezone and assume the bots follow; they read `TZ` from compose.

---

## 6. Kill switches

Each bot honours a kill-switch file. To stop new entries without killing the
process or touching open positions:

```bash
docker compose exec orb touch /app/state/STOP_orb
docker compose exec orb rm /app/state/STOP_orb      # resume
```

To flatten everything for one bot immediately:

```bash
docker compose exec orb python3 /app/orb_breakout/live.py --flatten
```

---

## What this costs

| item | cost |
|---|---|
| Oracle Always Free instance | $0 |
| Three Alpaca paper accounts | $0 |
| Jev decisions, all three bots | roughly $1.50 a month |
| Public dashboard on Cloudflare | $0 within the free tier |

Under two dollars a month to run three strategies around the clock with a public
leaderboard. The expensive part remains your attention, not the infrastructure.

Paper results are hypothetical. Nothing here is financial advice.
