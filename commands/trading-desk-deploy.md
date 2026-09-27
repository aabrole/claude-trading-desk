---
description: Deploy the bots and dashboard to a free Oracle Cloud VM, or redeploy after changes
---

Deploy this workspace to Oracle Cloud Always Free.

1. Load the `trading-desk-oracle` skill.
2. Check state before doing anything: is `oci` installed, is `~/.oci/config`
   present, does `oci iam region-subscription list` authenticate, and is there
   already an instance recorded in `~/.oci/trading-bots-provision.env`?
3. If credentials are missing, generate the keypairs, then stop and give the user
   the exact browser steps plus the public key to paste. You cannot create their
   Oracle account for them.
4. If credentials work and no instance exists, run `./deploy/oracle_provision.sh`.
   Expect "Out of host capacity" and let the retry loop handle it.
5. Commit first, because the deploy ships `git archive HEAD` and uncommitted work
   will not go out.
6. Run `./deploy/oracle_deploy.sh`.
7. **Verify rather than trusting the summary.** Check restart counts, not just
   `docker compose ps`, and read each bot's last log lines to confirm it is
   idling on the schedule you expect. Report the public URL only once the
   dashboard answers from outside the VM.

Never print the contents of `.env` or any key. Never commit `.env`.
