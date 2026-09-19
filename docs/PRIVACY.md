# What this system records

This is the employee-facing description of the monitoring. The same text is in
the application at `/privacy` and is shown in the tracker on first run.

## In one line

It records **when you are working and whether your computer is being used** —
not *what* you are doing on it.

## Collected

- The time you clock in and clock out
- Lunch and short breaks you start and end, and how long they lasted
- **How many seconds since your last keyboard or mouse input** — a number only
- Whether your workstation is locked
- Whether your laptop is reachable: on, asleep, shut down or disconnected
- Your computer's name, operating system version and the tracker version
- Your assigned working schedule, so late arrivals can be identified

## Not collected

- **Keystroke content.** The tracker asks Windows how long since the last
  input. That API returns a timestamp; it does not expose which key was pressed.
- Passwords of any kind
- Messages, emails or chat content
- Screenshots or screen recordings
- Webcam or microphone — no access is requested
- Personal files or documents
- Browsing history or websites visited
- Which applications are open
- Location

This is a property of how the software is built, not a policy promise. There is
no keyboard hook, no screen capture, and no window-title enumeration anywhere in
the agent, and the heartbeat has no field that could carry such content.

## When a team leader is notified

| Condition | Default |
|---|---|
| No keyboard or mouse input, with no break running | 10 minutes |
| A short break runs over | 10 minutes + 2 minutes grace |
| A lunch break runs over | 60 minutes + 2 minutes grace |
| Your laptop stops reporting while you are clocked in | 10 minutes |

Administrators can change these; the current values are always shown on
`/privacy`.

**Approved breaks stop the alerts.** While a lunch or short break is running, no
inactivity alert is sent. Use the break buttons when you step away and you will
not be flagged.

## Employee access

Every employee can see their own record at **My day**: today's clock-in and
clock-out, active and idle time, breaks taken, and the events logged. If
something is wrong, a manager can correct it — corrections keep the original
value and are recorded in the audit log with who made them.

---

## Legal note for the employer

**This is not legal advice.** Workplace monitoring is regulated, and the
requirements depend on where your employees are.

Broadly, and in most jurisdictions:

- **Tell people first, in writing.** In the UK and EU this is a requirement,
  not a courtesy. Several US states (including New York, Connecticut and
  Delaware) also require notice.
- **Under UK/EU GDPR**, monitoring needs a lawful basis. "Legitimate interests"
  is the usual one, and it requires a documented Legitimate Interests
  Assessment and, for systematic monitoring, likely a Data Protection Impact
  Assessment.
- **Collect the minimum.** This system is built to do that — it is one reason
  it records idle *duration* rather than activity *content*.
- **Set a retention period.** There is no automatic deletion in this system;
  decide how long you keep attendance records and enforce it.
- **Handle access requests.** Employees can generally request the data held
  about them. The employee report and CSV export cover this.
- **Consult where required.** Some countries (Germany's works councils,
  for example) require consultation before monitoring is introduced.

Have your HR and legal teams sign off on the rollout, and give employees this
document before the agent is installed rather than after.
