# Presence

Employee attendance and activity tracking for an office: a tracker that installs
on each company laptop, and a web dashboard for managers.

Built for 10–200 employees on Windows laptops, with the architecture in place to
add macOS.

---

## What it does

**For employees** — a small tray app with six buttons: Clock In, Clock Out,
Start Lunch Break, End Lunch Break, Start 10-Minute Break, End Break. It shows
their status, hours worked so far, and how long is left on a running break.

**For team leaders** — a live board showing who is active, on break, idle,
offline or clocked out, and for how long. Alerts arrive in the dashboard and in
Slack, Teams, email or WhatsApp.

**For managers and admins** — daily, weekly and monthly attendance reports with
CSV export, working schedules that identify late arrivals and early departures,
configurable inactivity limits and break allowances, and a full audit log.

## The design decision that matters

**The server decides everything. The agent is not trusted.**

The tracker reports two things: how many seconds since the last keyboard or
mouse input, and whether the screen is locked. Every judgement — is this person
idle, should the team leader be told, has this break run over — is made
server-side.

That is what makes the anti-tamper requirement work. An employee who closes the
tracker does not freeze their status as "active"; they stop sending heartbeats,
and the absence of heartbeats is itself what the server reports:

```
12:03:14  agent killed with SIGKILL, no chance to report anything
12:04:13  board flips to 🔴 Offline — "No heartbeat for 1m"
12:04:22  alert to the team leader: "Sara is still clocked in but their
          laptop stopped reporting 1 minute ago."
```

That trace is from the test suite. Killing the agent does not make someone look
active — it makes them look offline, which is more visible, not less.

## Privacy

It records **when you are working and whether your computer is in use** — not
what you do on it.

| Collected | Never collected |
|---|---|
| Clock in / clock out times | Keystroke content |
| Breaks started and ended | Passwords |
| Seconds since last input | Messages, emails, chat |
| Screen locked or not | Screenshots or recordings |
| Whether the laptop is reachable | Webcam, microphone |
| Computer name, OS version | Files, browsing, open apps, location |

This is structural, not a policy promise: there is no keyboard hook, no screen
capture and no window-title enumeration anywhere in the agent, and the heartbeat
has no field that could carry such content. Employees see the same disclosure at
`/privacy` and in the tracker on first run.

Workplace monitoring is regulated in most places and generally requires telling
staff in writing beforehand. See [docs/PRIVACY.md](docs/PRIVACY.md).

## Quick start

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# SQLite, no Docker, no certificates — for evaluation only.
cp .env.development.example .env

.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli seed-demo
.venv/bin/python -m uvicorn app.main:app --reload
```

`.env.example` is the **production** template and is deliberately not usable
as-is; the server refuses to start with its placeholder secrets.

Open http://localhost:8000 and sign in as `demo.admin` / `DemoPass2024`.

Run the agent against it:

```bash
cd agent
PRESENCE_SERVER_URL=http://localhost:8000 python run_agent.py
```

For production, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Layout

```
backend/
  app/
    models/       users, sessions, activity intervals, alerts, audit
    services/     attendance, activity, monitor, alerts, notifications, reporting
    api/v1/       agent API + dashboard API
    web/          server-rendered dashboards
  alembic/        migrations
  tests/          74 tests
agent/
  tracker/
    idle/         Windows / macOS / Linux idle detection
    ui/           tray icon, window, first-run setup
    platform/     autostart, Windows session hooks
    engine.py     heartbeat loop
  build/          PyInstaller spec, Inno Setup installer, watchdog task
  tests/          22 tests
deploy/           nginx, backups
docs/             architecture, deployment, admin guide, privacy, API
```

## Documentation

| | |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | How it works and why it is built this way |
| [Deployment](docs/DEPLOYMENT.md) | Production install, operations, troubleshooting |
| [Admin guide](docs/ADMIN_GUIDE.md) | Day-to-day use for managers |
| [Privacy](docs/PRIVACY.md) | What is recorded, and the employer's obligations |
| [API](docs/API.md) | Endpoint reference |
| [Agent build](agent/build/README.md) | Building and deploying the Windows tracker |

## Tests

```bash
cd backend && .venv/bin/python -m pytest tests/ -q          # 74 tests
cd backend && .venv/bin/python -m pytest ../agent/tests -q  # 22 tests
```

The monitoring tests cover the guarantees the system is built around: an
employee who walks away is noticed, an employee on an approved break is not, a
break that runs over is flagged, and a laptop that stops reporting is caught
whether it was shut down, disconnected, or the app was closed.

## Stack

FastAPI · PostgreSQL · SQLAlchemy 2 · Alembic · APScheduler · Jinja2 · Docker

The agent uses the Python standard library for everything load-bearing; the
tray icon and Windows session hooks are optional and degrade to a visible
window rather than stopping tracking.
