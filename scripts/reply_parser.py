"""Parses Laetizia's short Slack replies to the outreach-chase digest
into (row number, action) pairs.

Deliberately not an LLM call, unlike extractor.py's free-text deal
extraction: this feeds writes into live Attio pipeline stage, so a failed
match must fail safe (ambiguous, no write) rather than confidently guess
wrong. The reply vocabulary is small and she controls the convention, so a
regex + fuzzy-keyword matcher is enough — no need for NLP here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from config import COLLEAGUE_FIRST_NAMES

ACTION_FOLLOWED_UP = "followed_up"
ACTION_PARTNER = "partner"
ACTION_PASS = "pass"
ACTION_SKIP = "skip"
ACTION_AMBIGUOUS = "ambiguous"

_MATCH_THRESHOLD = 75

# Order matters only for readability — every phrase for every action is
# tried and the single best-scoring one wins.
_ACTION_PHRASES = {
    ACTION_FOLLOWED_UP: [
        "followed up",
        "follow up",
        "followed",
        "chased",
        "chase",
        "sent follow up",
    ],
    ACTION_PARTNER: [
        "partner attempt",
        "partner",
        "warm intro",
        "warm-intro",
        "intro attempt",
    ],
    ACTION_PASS: ["pass", "passed", "passing", "to pass"],
    ACTION_SKIP: ["skip", "skipped", "later", "not yet", "leave it"],
}

# "tried via Adrian" / "via Rasmus" / "through Nicole" — a colleague's name
# after via/through/by confidently means Partner Attempt/Warm Intro, not
# just the literal words "partner"/"warm intro".
_VIA_COLLEAGUE_RE = re.compile(
    r"\b(?:via|through|by)\s+("
    + "|".join(re.escape(n) for n in COLLEAGUE_FIRST_NAMES)
    + r")\b",
    re.IGNORECASE,
)
# "tried their linkedin" / "tried via linkedin" — also a Partner Attempt
# signal, independent of any colleague name being mentioned.
_TRIED_LINKEDIN_RE = re.compile(
    r"\btried\b.*\blinkedin\b|\blinkedin\b.*\btried\b", re.IGNORECASE
)

# Splits "1 followed up 2 pass 3 skip" into (row_number, start_of_phrase)
# anchor points; the phrase for row N runs until the next row number.
_ROW_ANCHOR_RE = re.compile(r"(\d+)\s*[:\-\.]?\s*")


@dataclass
class ReplyItem:
    row: int
    action: str
    raw_phrase: str
    colleague: str | None = None


def parse_reply(text: str) -> list[ReplyItem]:
    """Split reply text into one ReplyItem per row number mentioned. Row
    numbers are always literal digits already present in the text — never
    invented or guessed by fuzzy matching."""
    text = (text or "").strip()
    if not text:
        return []
    anchors = list(_ROW_ANCHOR_RE.finditer(text))
    if not anchors:
        return []
    items: list[ReplyItem] = []
    for i, m in enumerate(anchors):
        row = int(m.group(1))
        start = m.end()
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        phrase = text[start:end].strip()
        items.append(_classify(row, phrase))
    return items


def _classify(row: int, phrase: str) -> ReplyItem:
    if not phrase:
        return ReplyItem(row, ACTION_AMBIGUOUS, phrase)

    via_match = _VIA_COLLEAGUE_RE.search(phrase)
    if via_match or _TRIED_LINKEDIN_RE.search(phrase):
        return ReplyItem(
            row,
            ACTION_PARTNER,
            phrase,
            colleague=via_match.group(1) if via_match else None,
        )

    lowered = phrase.lower()
    best_action, best_score = None, 0
    for action, phrases in _ACTION_PHRASES.items():
        for candidate in phrases:
            score = fuzz.partial_ratio(lowered, candidate)
            if score > best_score:
                best_action, best_score = action, score

    if best_score >= _MATCH_THRESHOLD:
        return ReplyItem(row, best_action, phrase)
    return ReplyItem(row, ACTION_AMBIGUOUS, phrase)
