# API reference

Base URL: `https://your-server/api/v1`

Interactive docs are served at `/api/docs` in non-production environments.

## Authentication

Two separate schemes.

**Dashboards** use JWT bearer tokens (also accepted as an httpOnly cookie, which
is how the server-rendered pages authenticate).

```http
POST /api/v1/auth/login
{"username": "john.doe", "password": "..."}

200 {"access_token": "...", "refresh_token": "...", "expires_in": 1800,
     "must_change_password": false}
```

Access tokens last 30 minutes; refresh tokens 14 days and rotate on use.
Eight failed sign-ins lock the account for 15 minutes.

**Agents** use a long-lived device token in a header:

```http
X-Device-Token: <token from enrollment or agent login>
```

Device tokens are issued per laptop and can be revoked individually, which
immediately stops that machine from reporting.

## Agent endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/agent/enroll` | Bind a laptop using a one-time code |
| POST | `/agent/login` | Sign in from an already-approved laptop |
| POST | `/agent/heartbeat` | The 30–60s pulse |
| GET | `/agent/status` | Full snapshot, for start-up |
| GET | `/agent/config` | Current policy |
| POST | `/agent/clock-in` | Start a working session |
| POST | `/agent/clock-out` | End it |
| POST | `/agent/break/start` | `{"break_type": "lunch" \| "short"}` |
| POST | `/agent/break/end` | End the running break |
| POST | `/agent/events` | One lifecycle event |
| POST | `/agent/events/batch` | Events buffered while offline |
| POST | `/agent/accept-notice` | Record the monitoring acknowledgement |

### The heartbeat

This is the endpoint everything else depends on.

```http
POST /api/v1/agent/heartbeat
X-Device-Token: ...

{"idle_seconds": 720, "is_locked": false, "agent_version": "1.0.0"}
```

```json
{
  "state": "idle",
  "detail": "No keyboard or mouse activity",
  "clocked_in": true,
  "session_id": 42,
  "break": null,
  "totals": {"active_seconds": 6480, "idle_seconds": 720,
             "break_seconds": 0, "worked_seconds": 7200},
  "breaks_used": {"lunch": 0, "short": 1},
  "config": {"idle_threshold_seconds": 600, "heartbeat_interval_seconds": 45},
  "server_time": "2026-09-19T14:32:11+00:00"
}
```

The request carries **only** `idle_seconds` and `is_locked` as activity data.
There is no field for window titles, application names or input content, by
design.

The response tells the agent what state it is in — the agent does not decide.
`config` lets an administrator change the policy centrally and have every laptop
pick it up on its next beat.

**Missing heartbeats are the point.** If these stop arriving for longer than
`heartbeat_grace_seconds`, the server marks the session offline and, after
`offline_alert_after_seconds`, alerts the team leader. Nothing the agent does or
fails to do can suppress that.

## Dashboard endpoints

### Live

| Method | Path | Role |
|---|---|---|
| GET | `/live/board` | Manager — every visible employee's status |
| GET | `/live/me` | Any — your own status |
| GET | `/live/employee/{id}` | Manager — timeline and events |
| GET | `/live/stream` | Manager — server-sent event stream |

```json
GET /api/v1/live/board
{
  "summary": {"active": 12, "idle": 2, "on_break": 3, "offline": 1,
              "clocked_out": 4, "total": 22, "needs_attention": 3},
  "employees": [{
    "full_name": "John Doe", "state": "idle", "emoji": "🟠",
    "duration_display": "12m", "detail": "No keyboard or mouse activity",
    "worked_display": "6h 20m", "warnings": ["Idle past threshold, no break active"]
  }]
}
```

### Reports

| Method | Path | Notes |
|---|---|---|
| GET | `/reports/daily?date=&department_id=` | One row per employee |
| GET | `/reports/daily.csv` | Same, as CSV |
| GET | `/reports/weekly?date=` | Per employee plus department rollup |
| GET | `/reports/monthly?date=` | Same, monthly |
| GET | `/reports/period.csv?period=week\|month` | CSV export |
| GET | `/reports/employee/{id}?start=&end=` | Day by day for one person |
| GET | `/reports/employee/{id}/sessions?date=` | Raw sessions and breaks |
| PATCH | `/reports/sessions/{id}` | Admin — correct times (note required) |
| POST | `/reports/sessions` | Admin — add a manual session |

### Alerts

| Method | Path |
|---|---|
| GET | `/alerts?only_open=true&alert_type=&severity=` |
| GET | `/alerts/summary` |
| GET | `/alerts/{id}` |
| POST | `/alerts/{id}/acknowledge` |
| POST | `/alerts/acknowledge-all` |

Alert types: `idle_no_break`, `break_overrun`, `agent_offline`,
`agent_terminated`, `late_arrival`, `early_departure`, `missing_clock_out`,
`no_show`.

### Administration

| Method | Path | Role |
|---|---|---|
| GET/POST | `/employees` | Manager / Admin |
| POST | `/employees/with-password` | Admin — returns the initial password once |
| PATCH | `/employees/{id}` | Admin |
| POST | `/employees/{id}/team-leader` | Admin — route their alerts |
| POST | `/employees/{id}/reset-password` | Admin |
| POST | `/employees/{id}/enrollment-code` | Admin |
| GET | `/employees/{id}/devices` | Manager |
| DELETE | `/devices/{id}` | Admin — revoke a laptop |
| GET/POST/PATCH | `/departments` | Admin |
| GET/POST/PATCH | `/schedules` | Admin |
| GET/PATCH | `/settings/policy?department_id=` | Admin |
| GET/POST/PATCH/DELETE | `/settings/channels` | Admin |
| POST | `/settings/channels/{id}/test` | Admin |
| GET | `/settings/audit` | Admin |

## Errors

```json
{"detail": "You have already used all 2 10-minute break(s) allowed today."}
```

| Status | Meaning |
|---|---|
| 400 | Bad request — e.g. an expired enrollment code |
| 401 | Not authenticated, or the device token was revoked |
| 403 | Authenticated but not permitted |
| 409 | Conflict — already clocked in, break already running |
| 422 | Validation failed |
| 423 | Account locked after repeated failed sign-ins |
| 502 | An integration test failed; the provider's error is included |

`detail` is written to be shown directly to the person who triggered it.
