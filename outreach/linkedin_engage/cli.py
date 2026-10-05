"""Command line. `scan`, `import-drafts`, `review` and `sheet-rows` need no API key.

    python -m linkedin_engage.cli scan                  shortlist, scored and explained
    python -m linkedin_engage.cli draft                 scan + draft via the Claude API
    python -m linkedin_engage.cli import-drafts FILE    queue comments written elsewhere
    python -m linkedin_engage.cli review                approve or skip, one by one
    python -m linkedin_engage.cli publish               publish approved (dry run default)
    python -m linkedin_engage.cli sheet-rows            TSV to paste into the queue Sheet
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from linkedin_engage.config import get_settings
from linkedin_engage.models import Draft, DraftState
from linkedin_engage.providers import DryRunCommentSink, FixturePostSource
from linkedin_engage.queue import Queue, RateLimited, sheet_header, to_sheet_rows
from linkedin_engage.scoring import rank, score_post
from linkedin_engage.voice import find_violations


def _source(settings):
    if settings.post_source == "fixture":
        return FixturePostSource(Path(settings.fixture_path))
    raise SystemExit(
        f"post source '{settings.post_source}' is not wired up yet.\n"
        "LinkedIn's own API cannot search posts, so discovery needs an outside\n"
        "provider. Implement PostSource in linkedin_engage/providers/ and set\n"
        "ENGAGE_POST_SOURCE to it."
    )


def cmd_scan(args) -> int:
    settings = get_settings()
    q = Queue(settings.queue_path)
    posts = _source(settings).fetch(limit=settings.fetch_limit)

    gates = {
        "engaged_post_urns": q.engaged_post_urns(),
        "author_last_engaged": q.author_last_engaged(),
        "author_cooldown_days": settings.author_cooldown_days,
        "min_score": settings.min_score,
        "max_age_hours": settings.max_age_hours,
    }
    ranked = rank(posts, **gates)

    print(f"{len(posts)} fetched, {len(ranked)} worth commenting on\n")
    for post, score in ranked:
        print(f"  {score.total:5.1f}  {post.author.name:<18} {score.note}")
        print(f"         fresh {score.freshness:.2f}  sat {score.saturation:.2f}  "
              f"head {score.headroom:.2f}  base {score.base:.2f}")
        print(f"         {post.text.strip()[:96]}")
        print(f"         {post.url}\n")

    if args.verbose:
        print("Excluded:")
        for post in posts:
            s = score_post(post, **gates)
            if not s.engageable:
                print(f"  {post.author.name:<18} {s.verdict.value:<18} {s.note}")
    return 0


def cmd_draft(args) -> int:
    settings = get_settings()
    try:
        from linkedin_engage.drafting import DraftingError, draft_comment
    except ImportError as exc:
        raise SystemExit(f"the anthropic SDK is not installed: {exc}") from exc

    q = Queue(settings.queue_path)
    posts = _source(settings).fetch(limit=settings.fetch_limit)
    ranked = rank(
        posts,
        engaged_post_urns=q.engaged_post_urns(),
        author_last_engaged=q.author_last_engaged(),
        author_cooldown_days=settings.author_cooldown_days,
        min_score=settings.min_score,
        max_age_hours=settings.max_age_hours,
    )[: args.limit]

    for post, score in ranked:
        try:
            draft = draft_comment(post, score, settings=settings)
        except DraftingError as exc:
            print(f"  !! {post.author.name}: {exc}", file=sys.stderr)
            continue
        q.add(draft)
        flag = {DraftState.PENDING: "ok", DraftState.SKIPPED: "model skipped",
                DraftState.REJECTED_BY_FILTER: "filtered out"}.get(draft.state, draft.state.value)
        print(f"  [{flag}] {post.author.name}: {draft.text or '-'}")
        if draft.violations:
            print(f"           {'; '.join(draft.violations)}")
    return 0


def cmd_import_drafts(args) -> int:
    """Queue comments drafted anywhere else. Same filter, no API key needed.

    FILE is JSON: [{"urn": "...", "comment": "...", "rationale": "..."}]
    """
    settings = get_settings()
    q = Queue(settings.queue_path)
    posts = {p.urn: p for p in _source(settings).fetch(limit=settings.fetch_limit)}
    rows = json.loads(Path(args.file).read_text())

    queued = dropped = 0
    for row in rows:
        post = posts.get(row["urn"])
        if post is None:
            print(f"  ?? unknown post {row['urn']}", file=sys.stderr)
            continue
        score = score_post(post, min_score=settings.min_score,
                           max_age_hours=settings.max_age_hours)
        text = (row.get("comment") or "").strip()
        first = post.author.name.split()[0] if post.author.name else ""
        violations = find_violations(text, author_first_name=first)
        draft = Draft(post=post, score=score, text=text,
                      rationale=row.get("rationale", ""), attempts=1)
        if violations:
            draft.state = DraftState.REJECTED_BY_FILTER
            draft.violations = violations
            dropped += 1
            print(f"  [filtered] {post.author.name}: {'; '.join(violations)}")
        else:
            draft.state = DraftState.PENDING
            queued += 1
            print(f"  [ok] {post.author.name}: {text}")
        q.add(draft)
    print(f"\n{queued} queued, {dropped} filtered out")
    return 0


def cmd_review(args) -> int:
    settings = get_settings()
    q = Queue(settings.queue_path)
    rows = q.pending()
    if not rows:
        print("nothing pending")
        return 0
    for row in rows:
        print(f"\n  score {row['score']}  {row['author_name']}  "
              f"({row['age_minutes']:.0f}m old, {row['comment_count']} comments)")
        print(f"  post: {row['post_excerpt'][:160]}")
        print(f"  --> {row['comment']}")
        print(f"  why: {row['rationale']}")
        print(f"  {row['post_url']}")
        choice = input("  [a]pprove / [s]kip / [q]uit: ").strip().lower()
        if choice.startswith("q"):
            break
        if choice.startswith("a"):
            q.set_state(row["post_urn"], DraftState.APPROVED)
            print("  approved")
        else:
            q.set_state(row["post_urn"], DraftState.SKIPPED)
            print("  skipped")
    return 0


def cmd_publish(args) -> int:
    settings = get_settings()
    q = Queue(settings.queue_path)
    sink = DryRunCommentSink()
    if not settings.dry_run and not args.force:
        raise SystemExit(
            "ENGAGE_DRY_RUN is false but no live CommentSink is wired up.\n"
            "Implement CommentSink in linkedin_engage/providers/ first."
        )
    approved = [r for r in q._rows if r.get("state") == DraftState.APPROVED.value]
    seen, todo = set(), []
    for r in reversed(approved):
        if r["post_urn"] in seen:
            continue
        seen.add(r["post_urn"])
        if (q.find(r["post_urn"]) or {}).get("state") == DraftState.APPROVED.value:
            todo.append(r)
    if not todo:
        print("nothing approved")
        return 0
    for row in todo:
        try:
            q.check_rate_limits(
                daily_cap=settings.daily_comment_cap,
                min_gap_seconds=settings.min_seconds_between_comments,
            )
        except RateLimited as exc:
            print(f"stopping: {exc}")
            break
        pid = sink.publish(post_urn=row["post_urn"], text=row["comment"])
        q.set_state(row["post_urn"], DraftState.POSTED, published_id=pid)
        print(f"  {'(dry run) ' if sink.dry_run else ''}posted to {row['author_name']}: {pid}")
    return 0


def cmd_sheet_rows(args) -> int:
    settings = get_settings()
    q = Queue(settings.queue_path)
    rows = q.pending()
    print("\t".join(sheet_header()))
    for row in to_sheet_rows(rows):
        print("\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in row))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="linkedin_engage", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="score and rank candidate posts")
    s.add_argument("-v", "--verbose", action="store_true", help="also show what was excluded")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("draft", help="draft comments via the Claude API")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(fn=cmd_draft)

    s = sub.add_parser("import-drafts", help="queue comments drafted elsewhere")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import_drafts)

    s = sub.add_parser("review", help="approve or skip pending drafts")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("publish", help="publish approved comments")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser("sheet-rows", help="TSV for the Google Sheet queue")
    s.set_defaults(fn=cmd_sheet_rows)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
