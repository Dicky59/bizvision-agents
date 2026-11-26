# Runbook — M7 deploy & go-live

Run the steps in order, one at a time, and verify before moving on. Every command that touches the
server is yours to run; nothing here runs automatically. Decisions and reasons: ADR-008.

Server `5.222.8.228` / `2a01:4f9:c015:68b9::1` (Hetzner CX23, Helsinki). Database: Neon project
`lucky-haze-24832414`, branch `production`. Public name: `agents.bizvision.fi`.

> Secrets are never typed into chat, committed, or put on a command line. Use `read -rs VAR` or
> edit the file on the server.

---

## Step 0 — Production keys (before touching the server)

Create **new** keys. Dev keys never go on the server.

| Service | What to create |
|---|---|
| Anthropic | Workspace **`bizvision-agents-prod`** with its **own monthly spend limit** (suggest EUR 50 to start: well above the tenant budget of EUR 30, so the app's cap triggers first), and an API key inside that workspace |
| OpenAI | A separate project/key for production (embeddings only); set a project budget |
| Resend | A separate API key restricted to sending from `notify.bizvision.fi` |
| Admin API | `openssl rand -base64 48` (keep it in your password manager) |
| Neon | Connection string of the **`production`** branch, **direct** (not "pooled": host without `-pooler`). Prefix it `postgresql+asyncpg://` instead of `postgresql://` |

**Verify:** four values in your password manager, none of them reused from `.env` on your laptop.

## Step 1 — Server hardening

**1a. Bootstrap** (from this repo on your laptop, Git Bash):

```bash
scp scripts/server-bootstrap.sh root@5.222.8.228:/root/
ssh root@5.222.8.228 'bash /root/server-bootstrap.sh "$(cat)"' < ~/.ssh/id_ed25519.pub
```

It upgrades packages, creates `deploy` with your key, enables unattended security upgrades
(reboot window 04:00 UTC), installs Docker (Docker's apt repo if it has a suite for Ubuntu 26.04,
otherwise Ubuntu's `docker.io` + `docker-compose-v2`; it tells you which), caps container logs, and
clones the repo to `/srv/bizvision-agents`. It does not touch sshd.

**1b. Verify in a SECOND terminal** (keep the root session open):

```bash
ssh deploy@5.222.8.228 'sudo -n true && docker run --rm hello-world | head -n 3 && docker compose version'
```

**1c. Lock down SSH** (only after 1b works):

```bash
scp scripts/server-lockdown-ssh.sh root@5.222.8.228:/root/
ssh root@5.222.8.228 'bash /root/server-lockdown-ssh.sh'
```

**Verify** (new terminals): `ssh deploy@5.222.8.228` works; `ssh root@5.222.8.228` →
`Permission denied`; `ssh -o PubkeyAuthentication=no deploy@5.222.8.228` → `Permission denied`.

**1d. Hetzner Cloud Firewall** (console → Firewalls → create → apply to the server). Inbound:

| Protocol | Port | Source |
|---|---|---|
| TCP | 22 | Any IPv4 + Any IPv6 |
| TCP | 80 | Any IPv4 + Any IPv6 |
| TCP | 443 | Any IPv4 + Any IPv6 |
| UDP | 443 | Any IPv4 + Any IPv6 (HTTP/3) |
| ICMP | – | Any IPv4 + Any IPv6 |

Outbound: leave open. Docker-published ports bypass `ufw`, so this firewall is the real control;
do not add `ufw`.

**Verify:** `ssh deploy@5.222.8.228 'sudo unattended-upgrade --dry-run -d | tail -n 5'` runs
without errors; from your laptop a port scan (e.g. `Test-NetConnection 5.222.8.228 -Port 8000`)
times out.

## Step 2 — DNS (wePanel at Planeetta)

Add exactly two records in the `bizvision.fi` zone:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `agents` | `5.222.8.228` | 300 |
| AAAA | `agents` | `2a01:4f9:c015:68b9::1` | 300 |

Leave apex, `www`, MX and `notify` alone. If the zone has a **CAA** record it must allow
`letsencrypt.org`; tell me if you see one.

**Verify:** `nslookup agents.bizvision.fi` and `nslookup -type=AAAA agents.bizvision.fi` return the
two addresses. Do this before the first start of Caddy (the certificate request needs DNS and
port 80).

## Step 3 — Neon pre-check (SQL editor on the `production` branch)

```sql
SELECT name, default_version, installed_version FROM pg_available_extensions WHERE name = 'vector';
```

Need `default_version` **≥ 0.8.0** (retrieval runs `SET LOCAL hnsw.iterative_scan`). If lower:
stop and tell me. The migrations create the extension themselves.

## Step 4 — Server `.env`

```bash
ssh deploy@5.222.8.228
cd /srv/bizvision-agents
install -m 600 deploy/env.production.example .env
nano .env     # fill in the values from Step 0
stat -c '%a %U' .env    # must print: 600 deploy
```

`DATABASE_URL` and `DATABASE_URL_DIRECT` get the **same** direct string. Keep a copy of the whole
file in your password manager: it is the only thing needed to rebuild the server.

## Step 5 — First deploy (builds the image, applies migrations, starts api + caddy)

From your laptop, with the commit pushed to `origin`:

```bash
scripts/deploy.sh
```

The deploy succeeded only if the output ends with `remote steps done`, the public health check and
`deployed.`

Needs the DNS from Step 2 for the public health check (`SKIP_PUBLIC_CHECK=1` to skip it).
Caddy fetches the certificate on first start: `ssh deploy@5.222.8.228 'cd /srv/bizvision-agents && docker compose -f docker-compose.prod.yml logs caddy | tail -n 30'`.

**Verify:**
- `curl -sI https://agents.bizvision.fi/healthz` → `200`; also `curl -6 -sI https://agents.bizvision.fi/healthz`
- `curl -sI http://agents.bizvision.fi/healthz` → redirect to https
- `curl -s https://agents.bizvision.fi/widget.v1.js | head -c 200` is JavaScript
- On the server: `docker compose -f docker-compose.prod.yml run --rm api alembic current` shows `head`
- `SELECT extversion FROM pg_extension WHERE extname='vector';` in Neon is ≥ 0.8.0

## Step 6 — Tenant and FAQ

On the server (`cd /srv/bizvision-agents`; shorthand `bvx='docker compose -f docker-compose.prod.yml run --rm api bv'`):

```bash
alias bvx='docker compose -f docker-compose.prod.yml run --rm api bv'
bvx tenants create bizvision --config tenants/bizvision.yaml
bvx faq import bizvision faq/bizvision.yaml
bvx tenants show bizvision | grep -A3 allowed_origins
```

`faq/bizvision-pending.yaml` (placeholder entries) is **not** imported in production.

**Verify:** only the two `https://` origins are listed (no localhost); the FAQ summary says
`0 errors`.

## Step 7 — First ingest

```bash
bvx ingest bizvision --dry-run      # crawls and prints; no embeddings, no DB writes
bvx ingest bizvision                # embeds (a few cents of OpenAI); prints the cost
bvx search bizvision "mitä palveluita tarjoatte"
bvx evals run bizvision             # go-live bar: >= 90 % pass, 100 % on injection cases
```

The crawler reads the live site, which still has the old chatbot. **Re-run `bvx ingest bizvision`
after Step 10 (site changes)** so the index matches the final pages.

## Step 8 — Scheduled jobs, backups, monitoring

```bash
cd /srv/bizvision-agents
sudo cp deploy/systemd/bv-* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bv-cleanup.timer bv-notify-pending.timer bv-backup.timer
systemctl list-timers 'bv-*'
sudo systemctl start bv-backup.service && journalctl -u bv-backup.service -n 5 --no-pager
sudo systemctl start bv-cleanup.service && journalctl -u bv-cleanup.service -n 5 --no-pager
ls -lh /var/backups/bizvision
```

**Backup policy (ADR-008):** Neon PITR (paid plan) plus the weekly on-server dump is enough for now.
**Before the first paying customer tenant**, add an off-box copy (likely a Hetzner Storage Box) and
update ADR-008. Do one restore drill: create a throw-away Neon branch and
`pg_restore --no-owner -d <branch-url> /var/backups/bizvision/bv-*.dump`.

**Monitoring:** UptimeRobot (free) → HTTP(s) monitor on `https://agents.bizvision.fi/healthz`, 5-minute
interval, alert to your email. `/healthz` accepts both GET and HEAD.

## Step 9 — Production plan on Neon

Before announcing anything: upgrade the Neon project to a paid plan and **disable scale-to-zero** on
the `production` branch (Neon console → branch → compute settings). Free-tier cold starts add
seconds to the first message and the PITR window is short.

## Step 10 — Site changes and privacy notice

Hand `docs/bizvision-site-changes.md` to the Claude Code session in the `bizvision` repo.
The privacy notice (FI + EN) must list **Hetzner Online GmbH (Helsinki, Finland)** as a processor
(API hosting; IP addresses and conversation traffic pass through it) next to Anthropic, OpenAI,
Resend and Neon; its version must match `privacy_notice_version` in `tenants/bizvision.yaml`
(currently `2026-11`: bump it if the notice text changes, then `bvx tenants create bizvision --config tenants/bizvision.yaml`).
Re-check the consent wording in `widget/src/i18n/{fi,en}.json` against the notice.

## Step 11 — Smoke tests

```bash
read -rs ADMIN_API_KEY && export ADMIN_API_KEY
scripts/smoke.sh
```

By hand, in addition:
1. **Rate limit**: send > 20 chat messages in a minute from one machine → `429` with `Retry-After`.
   Then from a second network (phone hotspot): chat works while the first IP is limited (proves
   `TRUSTED_PROXY_COUNT=1` keys on the real client address).
2. **Lead**: submit the form in the real widget on bizvision.fi with consent → arrives at
   `info@bizvision.fi` (check headers: SPF/DKIM pass); `bvx leads list bizvision` shows it
   without `PENDING`; then `bvx leads delete bizvision <test-email> --yes`.
3. `bvx usage bizvision` shows the smoke traffic cost.
4. **No personal data in logs**: `docker compose -f docker-compose.prod.yml logs api | grep -i "<a phrase from your test chat>"` → nothing.
5. **Reboot**: `sudo reboot`; within ~2 minutes `https://agents.bizvision.fi/healthz` is back and the
   certificate is unchanged.
6. **Real devices**: iPhone Safari, Android Chrome, plus desktop Chrome/Firefox.

## Rollback

```bash
scripts/deploy.sh <previous-sha>      # the deploy output prints it
```

If the bad release included a migration: `alembic downgrade -1` on the server first (via
`docker compose -f docker-compose.prod.yml run --rm api alembic downgrade -1`, using the *new*
image), then deploy the old SHA. Site side: Vercel → Instant Rollback.
