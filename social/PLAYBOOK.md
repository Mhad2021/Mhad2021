# Publishing runbook

What to run, in order, to put a post live. Every action key, parameter name and
limit below was read off the live Zapier schemas on 2026-10-05, not from memory.

## Capability matrix (verified)

| Platform | Automated? | Action | Account param | Asset param | Text param |
|---|---|---|---|---|---|
| Instagram reel | yes | `publish_video` | `instagramPageId` | `video` (single URL) | `caption` |
| Instagram photo | yes | `publish_media_v2` | `instagramPageId` | `media` (list, 1–10) | `caption` |
| Facebook video | yes | `page_video` | `page` | `source` (single URL) | `description` (+ `title`) |
| Facebook photo/text | yes | `page_stream` | `page` | `source` (list, optional) | `message` |
| TikTok | **no** | — | — | — | — |

## Step 0 — one-time, needs you

1. **Instagram must be a Business account.** The `publish_video` help text
   excludes Personal *and Creator*. Creator accounts do not work.
2. The Instagram account must be linked to a Facebook Page you administer.
3. Authorise both apps:
   - Instagram: `https://mcp.zapier.com/api/v1/connect-auth/InstagramBusinessCLIAPI?accountId=28416392`
   - Facebook: `https://mcp.zapier.com/api/v1/connect-auth/FacebookV2CLIAPI?accountId=28416392`

Until this is done `inspect_zapier_actions` reports `connections: {total: 0}`
and every publish fails.

## Step 1 — resolve the account id

`instagramPageId` and `page` are dynamic enums. They cannot be guessed.

```
inspect_zapier_actions(
    tool_name="instagram_for_business_publish_video",
    enum_property="instagramPageId",
)
```

Take the `value` from `dynamic_enum_values`. Same for `page` on
`facebook_pages_create_page_video`. These ids are stable once found.

## Step 2 — host the asset

Both platforms pull media from a **public URL**; a local path is rejected. The
validator enforces this. Formats, from the action help text:

- Instagram video: `mp4 mov avi wmv flv webm mkv m4v 3gp 3g2 asf`
- Instagram photo: `jpg gif png ico bmp`
- Facebook photo: `jpg bmp png gif tiff`, **max 4MB**, PNG under 1MB

## Step 3 — validate before publishing

```bash
python -m autopost.cli check content/plan-today.json
```

Exits non-zero if anything is blocked. It catches caption overruns, hashtag
spam, unsupported formats, local paths, and a missing AI-disclosure label.

## Step 4 — publish

```bash
python -m autopost.cli call content/plan-today.json 0 --account-id <resolved id>
```

Hand the printed JSON to `execute_zapier_write_action`. `zapier_params` refuses
to build while any validation problem is outstanding, so a malformed post
cannot reach the API.

## Step 5 — TikTok

There is no automated path. The plan file still carries the TikTok entry so the
caption and asset are prepared; `check` reports it as `manual_only`. Upload it
by hand.

## Policy, not optional

Meta and TikTok both require AI-generated content to be disclosed. Set
`ai_generated: true` on any post whose asset came from a generator and the
label is appended automatically. The validator fails the post if the label is
missing. This is a takedown risk, not a lint warning.

## Scheduling

Once Step 0 and Step 1 are done, this becomes a Routine firing on a cron:
resolve ids once, then per run — pick the next post, `check`, `call`,
`execute_zapier_write_action`, record the result. Nothing in Steps 2–5 changes.
