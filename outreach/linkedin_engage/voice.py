"""The voice rules, as code rather than as advice.

A drafted comment is checked against these before it can ever reach the queue.
This exists because the failure mode of automated commenting isn't getting
caught -- it's getting read. "Great insight, Sarah! This really resonates" is
instantly recognisable as a bot, and the people worth reaching are the ones
most likely to recognise it. One of those comments costs more credibility than
fifty good ones earn.

`find_violations` is deliberately strict and cheap. Drafting regenerates on a
violation rather than publishing something borderline.
"""
from __future__ import annotations

import re

MAX_WORDS = 60
MAX_EMOJI = 1
MAX_EM_DASHES = 1

# Openers and fillers that mark a comment as automated.
_SLOP = [
    (r"\b(great|awesome|amazing|excellent|fantastic|brilliant)\s+(post|insight|point|share|thread|read)\b",
     "generic compliment"),
    (r"\bcould\s*n[o']?t\s+agree\s+more\b", "agreement filler"),
    (r"\b(totally|absolutely|completely)\s+agree\b", "agreement filler"),
    (r"\bthis\s+(really\s+)?resonates?\b", "AI-tell phrasing"),
    (r"\b(so|very)\s+true\b", "agreement filler"),
    (r"\bwell\s+said\b", "agreement filler"),
    (r"\bspot\s+on\b", "agreement filler"),
    (r"\bthanks?\s+for\s+sharing\b", "filler"),
    (r"\blove\s+this\b", "filler"),
    (r"\bgreat\s+to\s+see\b", "filler"),
    (r"\bthis\s+is\s+(so\s+)?important\b", "filler"),
    (r"^\s*(wow|yes|this)\b", "empty opener"),
    (r"\bcan['’]?t\s+agree\s+enough\b", "agreement filler"),
    (r"\b100\s*%\s*(this|agree)?\b", "agreement filler"),
    (r"\bwell\s+put\b", "agreement filler"),
]

# Pitching inside a comment thread. The comment's job is to make them curious
# about who we are, never to sell.
_PITCH = [
    (r"\bwe\s+(help|work\s+with|specialis|specializ|partner\s+with)", "pitch in a comment"),
    (r"\bour\s+(platform|product|tool|service|agency|team\s+can)\b", "pitch in a comment"),
    (r"\b(dm|pm)\s+me\b", "solicitation"),
    (r"\bhappy\s+to\s+(share|send|walk|jump|hop)\b", "solicitation"),
    (r"\b(book|grab|schedule)\s+a\s+(call|chat|slot|time)\b", "solicitation"),
    (r"\bfree\s+(audit|review|assessment|consultation|trial)\b", "free-audit offer"),
    (r"\bcheck\s+(out|us)\b", "solicitation"),
    (r"\breach\s+out\b", "solicitation"),
    (r"\blink\s+in\s+(my\s+)?(bio|comments)\b", "solicitation"),
]

# Category jargon. Nobody outside the category speaks like this, and leading
# with it signals vendor rather than peer.
_JARGON = [
    (r"\bGEO\b", "category jargon"),
    (r"\bLLM\s+optimi[sz]ation\b", "category jargon"),
    (r"\bAI\s+visibility\s+score\b", "category jargon"),
    (r"\bentity\s+optimi[sz]ation\b", "category jargon"),
    (r"\bcited\s+pages\b", "category jargon"),
    (r"\bsynerg", "corporate filler"),
    (r"\bleverage\b", "corporate filler"),
    (r"\bgame[\s-]?chang", "cliche"),
    (r"\bmove\s+the\s+needle\b", "cliche"),
    (r"\bdouble\s+down\b", "cliche"),
    (r"\bin\s+today['’]?s\s+(fast[\s-]?paced\s+)?(world|landscape|market)\b", "cliche"),
    (r"\bat\s+the\s+end\s+of\s+the\s+day\b", "cliche"),
]

# Posts where commenting is a trap: the thread is a lead-magnet funnel and
# every comment reads as someone wanting something.
_BAIT = [
    r"\bcomment\s+(\"|')?\w+(\"|')?\s+(below|and\s+i)",
    r"\b(drop|leave)\s+a\s+(comment|\+1|yes)\b",
    r"\bagree\s*\?\s*$",
    r"\btype\s+(\"|')?\w+(\"|')?\s+below\b",
    r"\bwho['’]?s\s+with\s+me\b",
    r"\btag\s+(someone|a\s+friend)\b",
    r"\brepost\s+(if|to)\b",
    r"\bi['’]?ll\s+(dm|send)\s+(it|you)\b",
    r"👇\s*$",
]

_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]"
)

_COMPILED_SLOP = [(re.compile(p, re.I), why) for p, why in _SLOP + _PITCH + _JARGON]
_COMPILED_BAIT = [re.compile(p, re.I | re.M) for p in _BAIT]


def looks_like_engagement_bait(post_text: str) -> bool:
    """True when the post is fishing for comments rather than saying something."""
    return any(rx.search(post_text) for rx in _COMPILED_BAIT)


def find_violations(text: str, author_first_name: str = "") -> list[str]:
    """Every reason this draft must not be posted. Empty list means it may be queued."""
    problems: list[str] = []
    stripped = text.strip()

    if not stripped:
        return ["empty draft"]

    words = len(stripped.split())
    if words > MAX_WORDS:
        problems.append(f"too long: {words} words (max {MAX_WORDS})")

    for rx, why in _COMPILED_SLOP:
        found = rx.search(stripped)
        if found:
            problems.append(f"{why}: {found.group(0).strip()!r}")

    emoji = _EMOJI.findall(stripped)
    if len(emoji) > MAX_EMOJI:
        problems.append(f"{len(emoji)} emoji (max {MAX_EMOJI})")

    em_dashes = stripped.count("—")
    if em_dashes > MAX_EM_DASHES:
        problems.append(f"{em_dashes} em dashes (max {MAX_EM_DASHES})")

    if author_first_name:
        opener = rf"^\s*{re.escape(author_first_name)}\s*[,!]"
        if re.match(opener, stripped, re.I):
            problems.append("opens by addressing the author by name")

    if stripped.count("?") > 1:
        problems.append("more than one question; stacked questions read as interrogation")

    if re.match(r"^\s*(i|my|we|our)\b", stripped, re.I):
        problems.append("opens with self-reference rather than their situation")

    return problems


SYSTEM_PROMPT = """\
You write LinkedIn comments on behalf of one businessperson, addressed to another.

The comment's only job is to make the author, and the people reading their \
thread, wonder who you are. It is never to sell, and never to agree.

Hard rules:
- 1 to 3 sentences. Under 60 words. Shorter is stronger.
- Never open with a compliment, with the author's name, or with "I"/"we".
- Add something they do not already have: a specific number, a counter-example, \
a consequence they did not name, or a question only someone who has actually \
done this would ask.
- Reference a concrete detail from their post, so it is obvious a human read it.
- No pitching, no offers, no "happy to share", no links, no DM requests.
- No agreement filler, no "great post", no "this resonates", no corporate \
vocabulary, no clichés.
- At most one emoji, and usually none. At most one em dash.
- Never invent a statistic, a case study or a client. If you do not have a real \
specific, ask a sharper question instead.
- Plain, varied sentences. Write how a competent operator types on their phone.

If the post genuinely gives you nothing worth saying, return an empty comment \
and say so in the rationale. A skipped comment costs nothing; a hollow one costs \
credibility."""
