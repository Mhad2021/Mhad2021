# social / autopost

Scheduled publishing to Instagram and Facebook. TikTok is prepared and exported
for manual upload, because it cannot be automated.

Self-contained: imports nothing from `backend/`, `agent/` or `outreach/`.

## What was verified, and what it rules out

Checked against the live Zapier connection rather than documentation:

- **Instagram publishes** — `publish_video` (posts as a Reel) and
  `publish_media_v2` (1–10 photos). **Business account only**; the action's own
  help text excludes Personal *and Creator* accounts.
- **Facebook publishes** — `page_video`, `page_stream`, `page_photo`. Pages
  only; there is no API for a personal profile.
- **TikTok cannot publish.** Zapier carries only TikTok Lead Generation
  (read-only) and TikTok Conversions (server-side ad events). There is no
  publishing integration to enable, so this is not fixable by connecting more
  apps. TikTok's Content Posting API needs your own audited developer app.
- **AI video generation is paid-only** on the connected ElevenLabs plan — the
  video node was refused outright. Still images do run, at roughly 1.8 cents
  and 33 seconds each.

The useful consequence: both publish actions accept *"a publicly accessible URL
we can pull the video from"*. So the automation never needed AI video. Produce
reels anywhere, give the pipeline a URL, and scheduled posting works. A paid
plan buys generated footage, not automation.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m autopost.cli platforms                      # capability matrix
.venv/bin/python -m autopost.cli check content/plan-today.json  # validate a plan
.venv/bin/python -m autopost.cli call  content/plan-today.json 0 --account-id ID
.venv/bin/python -m pytest tests/ -q                            # 22 tests
```

`check` exits non-zero when a post would fail or breach policy. `call` prints
the exact `execute_zapier_write_action` arguments and refuses to build them
while any problem is outstanding — a malformed post cannot reach the API.

See **PLAYBOOK.md** for the ordered runbook, including the two authorisation
links and how to resolve the dynamic account ids.

## Design notes

`platforms.py` holds the limits as data, sourced from the live schemas, because
these are what decide whether a scheduled post publishes or fails unattended.
Facebook video takes `description` and a `title`; Instagram takes `caption`;
Instagram photos take a list while video takes a single URL. Those asymmetries
are real and are the kind of thing that breaks a hand-rolled integration.

`caption.py` separates mechanical failures (length, format, missing asset) from
policy failures. The AI-disclosure label is the latter: it would not fail the
API call, it would get the post taken down later.

Nothing publishes unless a post is `APPROVED` and `dry_run` is off.

## Layout

```
autopost/
  platforms.py   verified limits, formats and the Zapier action mapping
  caption.py     caption assembly, AI disclosure, mechanical + policy checks
  queue.py       scheduled queue, dry-run gate, exact Zapier param builder
  cli.py         platforms / check / call
content/
  plan-today.json  a working plan, with placeholders marked
tests/             22 tests, no network
```
