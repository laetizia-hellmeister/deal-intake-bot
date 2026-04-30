"""Claude-based extraction of structured deal info from a Slack message."""

from __future__ import annotations

import json
import re
from typing import Any

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

SYSTEM_PROMPT = """You extract structured deal info from messages sent by a VC about companies
they're considering for investment. Return ONLY valid JSON, no preamble.

Schema:
{
  "is_deal": boolean,
  "company_name": string | null,
  "website": string | null,
  "domain": string | null,
  "linkedin_url": string | null,
  "founders": [ { "name": string, "linkedin": string | null } ],
  "stage": string | null,
  "round_size_eur_m": number | null,
  "sector": string | null,
  "description": string | null,
  "source": string | null
}

Rules:
- is_deal = true only if the message references an identifiable company
  (name or website). Casual chatter, status updates, and off-topic messages -> false.
- Do NOT invent data. Missing fields -> null.
- Normalize stage to one of: "Angel", "Pre-seed", "Seed", "Series A",
  "Series B", "Series C", "Unknown". Examples:
  "preseed" / "pre seed" / "pre-seed round" -> "Pre-seed"
  "seed round" / "seed stage" -> "Seed"
  "series a" -> "Series A"
- domain = root domain only (strip protocol, www., path, query).
- source = who shared this deal, e.g. "John from Atomico". null if not stated.
- Return only the JSON object, nothing else.
"""


class ExtractionError(Exception):
    pass


def _extract_json_object(text: str) -> dict:
    """Pull the first top-level JSON object out of the model's response."""
    text = text.strip()
    # Strip ```json fences if the model added them despite instructions
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: locate outermost {...} block
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ExtractionError(f"No JSON object in response: {text[:200]}")
        return json.loads(text[start : end + 1])


def extract_deal(message_text: str, client: Anthropic | None = None) -> dict[str, Any]:
    """Send a Slack message to Claude and return the parsed deal JSON."""
    c = client or Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = c.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message_text}],
    )
    # Concatenate all text blocks
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    raw = "".join(parts)
    if not raw.strip():
        raise ExtractionError("Empty response from Claude")
    data = _extract_json_object(raw)
    return _normalize(data)


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Apply defensive normalization to the parsed JSON."""
    out: dict[str, Any] = {
        "is_deal": bool(data.get("is_deal")),
        "company_name": _clean_str(data.get("company_name")),
        "website": _clean_str(data.get("website")),
        "domain": _normalize_domain(data.get("domain") or data.get("website")),
        "linkedin_url": _normalize_linkedin(data.get("linkedin_url")),
        "founders": _normalize_founders(data.get("founders")),
        "stage": _normalize_stage(data.get("stage")),
        "round_size_eur_m": _coerce_number(data.get("round_size_eur_m")),
        "sector": _clean_str(data.get("sector")),
        "description": _clean_str(data.get("description")),
        "source": _clean_str(data.get("source")),
    }
    return out


def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _coerce_number(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_domain(v: Any) -> str | None:
    s = _clean_str(v)
    if not s:
        return None
    s = s.lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/", 1)[0]
    s = s.split("?", 1)[0]
    s = s.rstrip("/.")
    return s or None


def _normalize_linkedin(v: Any) -> str | None:
    s = _clean_str(v)
    if not s:
        return None
    # Full URL expected; just trim trailing slash and query
    s = s.split("?", 1)[0].rstrip("/")
    if not s.startswith("http"):
        s = "https://" + s
    return s


_STAGE_CANON = {
    "angel": "Angel",
    "pre-seed": "Pre-seed",
    "preseed": "Pre-seed",
    "pre seed": "Pre-seed",
    "seed": "Seed",
    "series a": "Series A",
    "series b": "Series B",
    "series c": "Series C",
}


def _normalize_stage(v: Any) -> str | None:
    s = _clean_str(v)
    if not s:
        return None
    key = re.sub(r"\s+", " ", s.strip().lower())
    if key in _STAGE_CANON:
        return _STAGE_CANON[key]
    # Already canonical?
    allowed = {"Angel", "Pre-seed", "Seed", "Series A", "Series B", "Series C", "Unknown"}
    if s in allowed:
        return s
    return "Unknown"


def _normalize_founders(v: Any) -> list[dict[str, str | None]]:
    if not isinstance(v, list):
        return []
    out = []
    for f in v:
        if not isinstance(f, dict):
            continue
        name = _clean_str(f.get("name"))
        if not name:
            continue
        out.append({"name": name, "linkedin": _normalize_linkedin(f.get("linkedin"))})
    return out
