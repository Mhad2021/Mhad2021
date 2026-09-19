# Administrator guide

## Roles

| Role | Can do |
|---|---|
| **Admin** | Everything: people, teams, schedules, policy, integrations, audit log, corrections |
| **Team leader** | Live board and reports for their own team; receives and acknowledges alerts |
| **Employee** | Their own day and history only |

Team leaders cannot see other teams, and cannot change any setting.

## Daily use

### Live board

`/team/live` refreshes every 8 seconds and shows each person's state and how
long they have held it:

| | State | Means |
|---|---|---|
| 🟢 | Active | Keyboard or mouse input within the threshold |
| 🟡 | On Break | An approved lunch or short break is running |
| 🟠 | Idle | No input past the threshold, no break running |
| 🔴 | Offline | The laptop stopped reporting — off, asleep, disconnected, or the app was closed |
| ⚫ | Clocked Out | Not currently clocked in |

The board sorts problems to the top: offline first, then idle, then breaks.
Click anyone for their timeline and event history.

### Alerts

`/alerts` is the inbox. Acknowledging an alert marks it handled; alerts also
resolve themselves when the condition clears, so an employee who comes back from
lunch does not leave a stale alert behind.

**If alerts are noisy, the threshold is wrong, not the employees.** People on
phone calls, in meetings, or reading long documents look idle to any system that
measures keyboard input. Either raise the threshold or make sure those teams use
the break buttons.

## Two ways employees can track

| | Browser page (`/track`) | Desktop tracker |
|---|---|---|
| Install | none | installer per laptop |
| Clock in/out, breaks, hours | yes | yes |
| Late arrival, early departure | yes | yes |
| Break overrun alerts | yes | yes |
| **Inactivity alerts** | **no — see below** | yes |
| Detects laptop off or asleep | no | yes |
| Works on macOS | yes | not yet |

**Why the browser page raises no inactivity alerts.** A web page can only see
input inside its own tab. Someone working all day in Excel with the tracker in
a background window looks completely idle to it. Alerting on that would tell a
team leader an employee was inactive while they were working, so the signal is
not collected at all.

What that costs you: you cannot tell whether a clocked-in web user is at their
desk. What it buys you: no false accusations, and no installer to deploy.

Mixed teams are fine. The board marks web users with a `web` badge and shows a
note saying how many there are, so nobody misreads a green dot.

## Setup tasks

### Assign a team leader

People → the employee's row → the **Team leader** dropdown. This decides who
receives their alerts. Unassigned employees fall back to the department default,
then to an admin, so alerts are never silently dropped — but the fallback is a
safety net, not a plan.

### Working schedules

Teams → **Add a work schedule**. Set start, end, timezone, and which days are
working days (`1111100` is Monday–Friday). Grace periods absorb normal variation
so a 09:03 arrival is not flagged.

Late arrival and early departure are only detected against a schedule. An
employee with no schedule is never flagged.

### Policy

Policy → set the company-wide defaults, or pick a department to override just
some values for it.

The one setting to get right is **Mark offline after**. It must be more than
twice the heartbeat interval, or a single dropped packet reports a laptop as
offline. The form rejects values that would cause this.

### Notification channels

Alerts setup → add Slack, Teams, email, WhatsApp or a generic webhook.

Slack is the fastest to set up and the most likely to be read. Create an
incoming webhook in Slack, paste the URL, and press **Send test**.

Channels can be scoped to a department and to a minimum severity, so the
support team's leader is not paged about another department.

### Enrolling a laptop

People → **Enroll device** issues a one-time code, valid 72 hours. The employee
types it into the tracker once. Alternatively they sign in with their work
username and password.

**Revoking a device** (People → the employee → their devices) immediately stops
that laptop from reporting. Use it when a laptop is lost or reassigned.

## Corrections

When someone forgets to clock in, or their laptop was broken:

- **Adjust a session**: correct the clock-in or clock-out time. A note is
  required, and the original values are kept in the audit log.
- **Add a manual session**: record a day that was never tracked.

Both are admin-only and both appear in reports flagged as `edited`, so a
corrected day is never silently indistinguishable from a tracked one.

## Reports

- **Daily** — clock-in, clock-out, worked, active, idle, lunch, breaks, late
  and early flags, per person
- **Weekly / monthly** — per employee and rolled up by department
- **Per employee** — any date range, day by day

Every report exports to CSV for payroll.

**A note on "activity rate."** It is active time over worked time. It measures
keyboard and mouse input, which is a reasonable proxy for desk work and a poor
one for thinking, phone calls, meetings and reading. Use it to spot a laptop
that has been left logged in, not to rank people.

## Audit log

`/admin/audit` records every configuration change and manual correction: who,
what, when, from which IP, with before and after values. It is append-only.

## Common situations

**Someone's laptop is offline but they are at their desk.** Almost always the
network or the TLS chain. Run `PresenceTracker.exe --check` on that laptop.

**An employee says the tracker is spying on them.** Show them `/privacy`, which
lists what is and is not collected, and their own `/me` page, which shows
everything held about them. The honest answer is that it records *whether* the
computer is in use, not *what* it is used for.

**Someone closed the tracker.** They show as Offline, and their team leader is
alerted. This is not something to work around — it already works. What you do
about it is a management question, not a technical one.

**Alerts stopped arriving.** Check the scheduler container is running. If it
stops, the dashboards keep working normally and nothing looks broken, but no
alerts are raised at all.
