# outreach / linkedin_engage

Finds the LinkedIn posts worth commenting on, drafts the comment, and holds it
for approval. Self-contained: it imports nothing from `backend/` or `agent/`
and lifts out into its own repository unchanged.

---

## Read this before building on it

**LinkedIn cannot be read through its own API, and Zapier does not change that.**

Checked directly against the live Zapier connection. The LinkedIn integration
exposes exactly four actions:

| Action | What it does |
|---|---|
| Create Share Update | posts to your own feed |
| Create Company Update | posts to a Company Page |
| Make API GET Request | raw GET, *"requests can only be made to the app's known API endpoints"* |
| Make API Mutating Request | raw POST/PUT/PATCH/DELETE, same fence |

No find-posts action, no comment action, and the raw-request escape hatch is
fenced to endpoints the integration already knows. That ceiling is LinkedIn's,
not Zapier's: there is no keyword post search for standard apps, and commenting
in third-party threads is not in the member scope these integrations hold.
Connecting more apps does not unlock it.

So **discovery and publishing are deliberately behind a seam**
(`providers/base.py`). Which provider fills it is a commercial decision —
a paid vendor API, a logged-in browser session, or a human pasting URLs — and
the rest of the engine does not change when you pick one.

## What this actually gives you

The hard part of commenting at scale was never fetching posts. It is these three:

**1. Timing, scored.** A comment on a 90-minute-old post rides the post's own
distribution. The same comment at six hours is read by nobody. Tools in this
space rank by keyword match and ignore timing, which is why they generate
busywork. Here, freshness and comment-saturation are first-class multipliers,
and a post past its useful window is **excluded**, not merely downranked:

```
 95.5  Dana Reyes       48m old, 6 comments      fresh 1.00  sat 1.00  head 1.07
 74.7  Sam Okonkwo      75m old, 2 comments      fresh 1.00  sat 0.80  head 1.09
 30.8  Tomas Lindqvist  310m old, 11 comments    fresh 0.36  sat 1.00  head 1.00

Excluded:
 Priya Raman    engagement_bait    commenting looks desperate
 Grace Mbeki    below_threshold    scored 18.8, below 25
 Ahmed Farouk   too_old            28.0h old (limit 24h)
```

`head` is headroom: many reactions against few comments means reach the comment
section has not caught up with — unusually cheap visibility.

**2. A slop filter that refuses to publish.** The real risk is not being caught,
it is being read. "Great insight, Sarah! This really resonates" is instantly
recognisable, and the people worth reaching are the ones most likely to
recognise it. One of those costs more credibility than fifty good comments earn.
Every draft is checked against `voice.py` and regenerated on a violation; a
draft that cannot pass is dropped rather than softened:

```
[filtered] generic compliment: 'Great post'; filler: 'Love this';
           pitch in a comment: 'we help'; solicitation: 'DM me'; 3 emoji (max 1)
```

**3. Memory and caps.** Author cooldowns stop you appearing in the same person's
threads every week. A daily cap and a minimum gap between comments exist because
automated commenting at volume is what gets profiles restricted:

```
daily cap reached: 15/15 comments in the last 24h
last comment was 0s ago; wait 239s
```

Nothing reaches LinkedIn while `ENGAGE_DRY_RUN` is true, which is the default.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m linkedin_engage.cli scan -v        # score and rank, with exclusions
.venv/bin/python -m linkedin_engage.cli review         # approve or skip, one by one
.venv/bin/python -m linkedin_engage.cli sheet-rows     # TSV for the queue Sheet
.venv/bin/python -m pytest tests/ -q                   # 48 tests
```

Ships with `fixtures/sample_posts.json`, so the whole pipeline runs with no
LinkedIn access and no API key.

### Drafting without an API key

`draft` calls the Claude API and bills per token. If you would rather not,
`import-drafts` takes comments written anywhere else and puts them through the
same filter and queue:

```bash
cat drafts.json   # [{"urn": "...", "comment": "...", "rationale": "..."}]
.venv/bin/python -m linkedin_engage.cli import-drafts drafts.json
```

Only `draft` needs `anthropic` or a key. Everything else is free to run.

## Configuration

Environment variables, prefix `ENGAGE_`. The ones that matter:

| Variable | Default | |
|---|---|---|
| `ENGAGE_DRY_RUN` | `true` | nothing is published while true |
| `ENGAGE_MIN_SCORE` | `25` | below this a post is not worth a comment |
| `ENGAGE_MAX_AGE_HOURS` | `24` | hard exclusion, not a penalty |
| `ENGAGE_AUTHOR_COOLDOWN_DAYS` | `14` | days before commenting on the same person again |
| `ENGAGE_DAILY_COMMENT_CAP` | `15` | per rolling 24h |
| `ENGAGE_MIN_SECONDS_BETWEEN_COMMENTS` | `240` | |
| `ENGAGE_POST_SOURCE` | `fixture` | `fixture` is the only one wired up |

## Layout

```
linkedin_engage/
  scoring.py      freshness, saturation, headroom, hard gates
  voice.py        the voice rules and the slop filter, as enforceable patterns
  drafting.py     Claude API call, regenerate-on-violation loop
  queue.py        append-only JSONL, author memory, rate limits, Sheet rows
  providers/      the seam: PostSource and CommentSink
  cli.py          scan / draft / import-drafts / review / publish / sheet-rows
tests/            48 tests, no network
```

## What is not built

- A live `PostSource`. Nothing can discover posts until one exists.
- A live `CommentSink`. `publish` runs against a dry-run sink.
- The Google Sheet write. `sheet-rows` emits the rows; pushing them needs either
  a service account or an assistant with the Sheets connector.
- ICP and relevance scores arrive on the `Post`; computing them is upstream work.
