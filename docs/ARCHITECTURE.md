# Architecture

## The one decision everything else follows from

**The server is the source of truth. The agent is not trusted.**

The desktop agent reports two raw signals — how many seconds since the last
keyboard or mouse input, and whether the workstation is locked. It makes no
judgement about what those mean. Every decision (is this person idle? should
the team leader be told? has this break run over?) is made server-side.

This matters because of one requirement in particular: *an employee must not be
able to appear active by closing the tracking application*. If the agent decided
its own state, closing it would freeze that state at whatever it last claimed.
Because the server decides, a closed agent produces **no heartbeat**, and the
absence of a heartbeat is itself the signal. There is nothing for the employee
to suppress.

```
┌──────────────────────────┐         ┌──────────────────────────────────────┐
│  Employee laptop         │         │  Server                              │
│                          │         │                                      │
│  ┌────────────────────┐  │  HTTPS  │  ┌────────────────────────────────┐  │
│  │ Tray agent         │──┼────────▶│  │ Agent API                      │  │
│  │  idle_seconds: 720 │  │  every  │  │  records the raw report        │  │
│  │  is_locked: false  │  │  30-60s │  └───────────────┬────────────────┘  │
│  └────────────────────┘  │         │                  ▼                   │
│         ▲                │         │  ┌────────────────────────────────┐  │
│         │ directives     │◀────────┼──│ Activity intervals             │  │
│         │ (state, break, │         │  │  one open interval per session │  │
│         │  policy)       │         │  └───────────────┬────────────────┘  │
└──────────────────────────┘         │                  ▼                   │
                                     │  ┌────────────────────────────────┐  │
   silence for > grace period ───────┼─▶│ Monitor  (every 30s)           │  │
   is itself a reportable event      │  │  idle? overrun? heartbeat lost?│  │
                                     │  └───────────────┬────────────────┘  │
                                     │                  ▼                   │
                                     │  ┌────────────────────────────────┐  │
                                     │  │ Alerts ──▶ Slack / Teams /     │  │
                                     │  │            email / WhatsApp    │  │
                                     │  └────────────────────────────────┘  │
                                     └──────────────────────────────────────┘
```

## Components

| Component | What it is | Where |
|---|---|---|
| Agent API | Enrolment, heartbeat, clock in/out, breaks, lifecycle events | `backend/app/api/v1/agent.py` |
| Web API | Dashboards, reports, alerts, administration | `backend/app/api/v1/` |
| Monitor | The 30-second loop that raises every alert | `backend/app/services/monitor.py` |
| Notification engine | Queue, providers, retry with backoff | `backend/app/services/notifications/` |
| Dashboards | Server-rendered, role-scoped | `backend/app/web/` |
| Desktop agent | Tray app, idle detection, heartbeat | `agent/tracker/` |

## Time accounting

The unit of accounting is the **activity interval**: a contiguous run in one
state. A session has exactly one open interval at any moment. When the state
changes, the interval closes and its duration is added to the matching total.

```
09:00 ─────────────── active ──────────────▶ 11:48  (2h48m → active_seconds)
11:48 ──── idle ────▶ 12:00                         (12m   → idle_seconds)
12:00 ──── lunch ───▶ 12:45                         (45m   → lunch_seconds)
12:45 ─────────────── active ──────────────▶ 17:30  (4h45m → active_seconds)
```

Two properties fall out of this, and both are tested:

- **Every second is in exactly one bucket.** The interval totals reconcile
  against the session's wall-clock span with zero drift.
- **Storage is bounded by state changes, not by time.** Storing every heartbeat
  would be ~96,000 rows a day at 200 employees. Intervals are a few dozen.

### Backdating

When a heartbeat reports `idle_seconds: 720`, the employee did not become idle
*now* — they became idle twelve minutes ago. The idle interval is therefore
backdated, and the preceding active interval truncated to match. Without this,
up to one heartbeat interval of idle time would be miscounted as active on
every transition.

The same applies to a lost heartbeat: the offline run starts at the **last
confirmed beat**, not at the moment the monitor noticed. A laptop that dies at
10:00 and is noticed at 10:03 shows as offline from 10:00.

Backdating is clamped so an interval can never start before the interval it
replaced — an employee who clocks in and immediately reports 700 seconds idle
was not idle before they arrived.

## How each guarantee is enforced

| Requirement | Mechanism |
|---|---|
| Idle > 10 min alerts the team leader | Monitor compares `state_since` against the threshold every 30s |
| Approved breaks suppress alerts | `check_idle` returns early when an open break exists |
| Break overruns alert | Monitor compares elapsed against the allowance snapshotted at break start |
| Closing the app is caught | No heartbeat → `heartbeat_grace_seconds` elapses → offline + alert |
| Laptop off / disconnected | Identical path — the server cannot tell them apart, and does not need to |
| Alerts reach the right person | `team_leader_id` → department default → any admin, so nothing is dropped |

## Why alerts do not storm

Three mechanisms:

1. **Dedup key.** A unique index on `alerts.dedup_key` means the same condition
   cannot be raised twice, however many times the monitor ticks.
2. **Re-alert window.** A still-idle employee re-alerts only once per
   `idle_realert_seconds` (default 30 minutes), by bucketing the idle duration
   into the dedup key.
3. **Resolution.** When the condition clears — the employee returns, the laptop
   reconnects — the open alert is resolved rather than left to be manually
   dismissed.

## Scaling to 200 employees

At 200 employees with a 45-second heartbeat, the server sees **~4.4 requests
per second**. That is not a demanding load; the design choices are about
storage and correctness, not throughput.

- Web containers scale horizontally.
- **The scheduler must be a single instance.** Two schedulers would both
  evaluate the same open sessions, and while the dedup index prevents duplicate
  alerts, they would race on interval writes. The compose stack enforces this
  by running the monitor in its own container with
  `PRESENCE_DISABLE_SCHEDULER=1` set on the web replicas.

## Privacy by construction

The agent cannot leak what it never collects:

- Idle time comes from `GetLastInputInfo`, which returns **a timestamp**.
  Windows does not expose which key was pressed through this API.
- There is no keyboard hook, no screen capture, no window-title enumeration,
  no webcam or microphone access, and no browser integration anywhere in the
  agent.
- The heartbeat payload is a fixed set of fields: idle seconds, lock state,
  agent version, hostname, OS version. It has no free-text field for activity
  content.

`/privacy` states all of this to employees in plain language, and the agent
shows the same disclosure on first run.
