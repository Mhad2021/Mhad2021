# Deployment

Target: 10–200 employees, one server, Windows laptops.

## 1. Server prerequisites

A small VM is enough. At 200 employees the server handles about 4.4 requests
per second.

- 2 vCPU, 4GB RAM, 40GB disk
- Docker Engine with the Compose plugin
- A DNS name the laptops can resolve, e.g. `presence.yourcompany.com`
- A TLS certificate (a certificate from your internal CA is fine)

## 2. Configure

```bash
git clone <your-repo> presence && cd presence
cp backend/.env.example .env
```

Generate the two secrets — do not skip this, the server refuses to start in
production with the defaults:

```bash
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
echo "ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" >> .env
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" >> .env
```

Then edit `.env`:

```ini
ENVIRONMENT=production
BASE_URL=https://presence.yourcompany.com
TRUSTED_HOSTS=presence.yourcompany.com
SECURE_COOKIES=true
DATABASE_URL=postgresql+psycopg://presence:<POSTGRES_PASSWORD>@db:5432/presence
```

`SECRET_KEY` signs sessions; rotating it signs everyone out.
`ENCRYPTION_KEY` encrypts integration credentials; **rotating it makes existing
Slack and SMTP credentials unreadable** and they must be re-entered.

## 3. Certificates

```bash
mkdir -p deploy/certs
cp your-cert.crt deploy/certs/presence.crt
cp your-key.key  deploy/certs/presence.key
chmod 600 deploy/certs/presence.key
```

If you use an internal CA, push the CA certificate to every laptop through
Group Policy, or the agent's TLS verification will fail.

## 4. Start

```bash
docker compose up -d
docker compose logs -f migrate     # confirm migrations applied
curl -k https://presence.yourcompany.com/health
```

Create the first administrator:

```bash
docker compose exec web python -m app.cli create-admin \
  --username admin --email admin@yourcompany.com \
  --name "Your Name" --timezone Europe/London
```

Then verify the configuration:

```bash
docker compose exec web python -m app.cli check-config
```

## 5. Set it up in the dashboard

Sign in at `https://presence.yourcompany.com` and work through, in this order:

1. **Teams → schedules.** Create the working patterns, e.g. "Office hours,
   09:00–18:00, Mon–Fri, Europe/London". Without a schedule nothing can be
   flagged as late or early.
2. **Teams → departments.** Create them and set a default team leader for each.
   The default is the fallback that stops alerts being dropped for anyone whose
   own leader is unset.
3. **People.** Add team leaders first, then employees, so each employee can be
   assigned to a leader as you create them.
4. **Policy.** Defaults are 10-minute inactivity limit, 10-minute short breaks
   (2/day), 60-minute lunch (1/day). Adjust to your rules.
5. **Alerts setup.** Add at least one channel. A Slack incoming webhook is the
   fastest and most likely to actually be read. Press **Send test** — an
   untested integration is an integration that will fail on the day it matters.

## 6. Roll out to laptops

Build and package the agent on a Windows machine (see `agent/build/README.md`),
then deploy silently:

```
PresenceTracker-Setup.exe /VERYSILENT /NORESTART
```

**Roll out to one team first.** Watch the live board for a day. Idle thresholds
that sound right in a meeting often turn out to be wrong for how a particular
team actually works — people on phone calls look idle, and you would rather
discover that with five people than two hundred.

Each employee then enrols once, either by signing in with their work account or
with a one-time code from **People → Enroll device**.

## Operations

### Backups

Attendance records are pay evidence. Back them up and test the restore.

```bash
# Add to the host's crontab
0 2 * * * cd /opt/presence && docker compose exec -T db /usr/local/bin/backup.sh
```

The script verifies each dump with `pg_restore --list` before rotating old
ones, because a backup that has never been read is not a backup.

### Upgrades

```bash
git pull
docker compose build
docker compose up -d          # migrations run automatically
```

### Health

```bash
curl https://presence.yourcompany.com/health
docker compose logs -f scheduler | grep -i "monitor tick"
```

The **scheduler** container is the one that matters. If it stops, the dashboards
keep working and nothing looks broken — but no alerts are raised at all. Monitor
it specifically.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No alerts at all | Scheduler container stopped | `docker compose restart scheduler` |
| Alerts in the dashboard but not Slack | Channel misconfigured | Alerts setup → Send test; the error is shown |
| Everyone shows Offline | Laptops cannot reach the server | Check DNS, firewall, and the TLS chain |
| One person always Offline | Agent not running, or TLS fails | Run `PresenceTracker.exe --check` on that laptop |
| Alerts go to the wrong person | Team leader not assigned | People → set their team leader |
| Duplicate alerts | Two schedulers running | Ensure only one container runs the monitor |
| Server refuses to start | Production checks failed | `check-config`; it names each problem |

## Before you go live

- [ ] `SECRET_KEY` and `ENCRYPTION_KEY` set to generated values
- [ ] `SECURE_COOKIES=true` and `TRUSTED_HOSTS` set to your real domain
- [ ] TLS working; internal CA pushed to laptops if applicable
- [ ] Nightly backup scheduled **and a restore tested**
- [ ] At least one notification channel added and tested
- [ ] Every employee assigned a team leader and a schedule
- [ ] Exactly one scheduler instance running
- [ ] Employees told about the monitoring **in writing, before it starts**
  (see `PRIVACY.md` — in the UK, EU and several US states this is a legal
  requirement, not a courtesy)
