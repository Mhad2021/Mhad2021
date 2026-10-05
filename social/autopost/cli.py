"""Inspect the pipeline without publishing anything.

    python -m autopost.cli platforms          the verified capability matrix
    python -m autopost.cli check FILE         validate a plan, show what would block
    python -m autopost.cli call FILE INDEX    the exact Zapier call for one post
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from autopost.caption import Caption
from autopost.platforms import AssetKind, Platform, spec
from autopost.queue import PostQueue, PostState, ScheduledPost


def cmd_platforms(args) -> int:
    for platform in Platform:
        s = spec(platform)
        mark = "publishes" if s.can_publish else "MANUAL ONLY"
        kinds = ", ".join(sorted(k.value for k in s.actions)) or "-"
        print(f"\n{platform.value.upper():<10} {mark}")
        print(f"  post types      {kinds}")
        print(f"  caption limit   {s.caption_limit}")
        print(f"  hashtag cap     {s.hashtag_soft_cap}")
        print(f"  AI disclosure   {'required' if s.requires_ai_disclosure else 'no'}")
        for note in s.notes:
            print(f"  - {note}")
    return 0


def _load(path: str) -> PostQueue:
    q = PostQueue("queue.jsonl")
    for row in json.loads(Path(path).read_text()):
        q.add(ScheduledPost(
            platform=Platform(row["platform"]),
            caption=Caption(
                text=row["text"],
                hashtags=tuple(row.get("hashtags", ())),
                ai_generated=bool(row.get("ai_generated", False)),
            ),
            asset_kind=AssetKind(row.get("asset_kind", "image")),
            assets=list(row.get("assets", [])),
            title=row.get("title", ""),
        ))
    return q


def cmd_check(args) -> int:
    q = _load(args.file)
    ok = [p for p in q.posts if p.state is PostState.DRAFT]
    print(f"{len(q.posts)} posts: {len(ok)} ready, {len(q.blocked())} blocked, "
          f"{len(q.manual_only())} manual only\n")
    for i, p in enumerate(q.posts):
        print(f"  [{i}] {p.state.value:<12} {p.platform.value:<10} {p.asset_kind.value:<6} "
              f"{p.caption.text.strip()[:52]}")
        for problem in p.problems:
            print(f"       - {problem}")
    return 1 if q.blocked() else 0


def cmd_call(args) -> int:
    q = _load(args.file)
    post = q.posts[args.index]
    if post.problems:
        raise SystemExit("post is not publishable:\n  " + "\n  ".join(post.problems))
    print(json.dumps(post.zapier_call(args.account_id), indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="autopost", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("platforms", help="verified capability matrix").set_defaults(fn=cmd_platforms)

    s = sub.add_parser("check", help="validate a plan file")
    s.add_argument("file")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("call", help="exact Zapier call for one post")
    s.add_argument("file")
    s.add_argument("index", type=int)
    s.add_argument("--account-id", default="ACCOUNT_ID_FROM_DYNAMIC_ENUM")
    s.set_defaults(fn=cmd_call)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
