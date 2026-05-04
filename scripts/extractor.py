"""Claude-based extraction of structured deal info from a Slack message."""

from __future__ import annotations

import json
import re
from typing import Any

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

SYSTEM_PROMPT = """You extract structured deal info from messages sent by a VC about companies
they're considering for investment. A single message may contain multiple deals
(e.g. a bulleted list). Return ONLY valid JSON, no preamble.

Schema:
{
  "deals": [
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
      "source": string | null,
      "sourcing_channel": string | null
    }
  ]
}

Rules:
- The top-level object always has a single key "deals" containing an array.
- Extract each distinct deal as a separate item in the array. A bulleted list
  of three companies = three items. A single company in prose = one item.
- If the message is casual chatter / status updates / off-topic, return
  {"deals": []} (empty array).
- For each item, is_deal = true only if it references an identifiable
  company (a name) OR an identifiable founder (a person's name with or
  without a website). Stealth companies that name only a founder are still
  deals (set company_name = null and put the founder in `founders`).
- Do NOT invent data. Missing fields -> null.
- Normalize stage to one of: "Angel", "Pre-seed", "Seed", "Series A",
  "Series B", "Series C", "Unknown". Examples:
  "preseed" / "pre seed" / "pre-seed round" -> "Pre-seed"
  "seed round" / "seed stage" -> "Seed"
  "series a" -> "Series A"
- domain = root domain only (strip protocol, www., path, query).
- source = who shared this deal, e.g. "Hillary from TestCo VC", "shared by
  Tom Smith". If the source is stated once at the top of a multi-deal
  message, copy it onto each deal. null if not stated.
- sourcing_channel = the channel this deal came in through. Pick one of:
  "VC", "Angel", "Personal Network", "Demo Day", "Conference / Event",
  "LinkedIn", "Cold Email (Inbound)", "Database (Specter, Dealroom)",
  "Sector Research", "Accelerator / Incubator", "University",
  "Founder Network", "Portfolio Founder", "Advisory / Broker Firm",
  "Ecosystem / AI Campus", "Active Sourcing".
  Examples:
    "Hillary from TestCo VC" -> "VC"
    "Tom Smith, an angel investor" / "this angel shared" -> "Angel"
    "my friend John mentioned" / "via personal contact" -> "Personal Network"
    "saw at the AI Demo Day" / "from XYZ demo day" -> "Demo Day"
    "from a LinkedIn DM" / "via LinkedIn" -> "LinkedIn"
    "cold email from..." / "inbound from..." -> "Cold Email (Inbound)"
    "from the AI Campus ecosystem" -> "Ecosystem / AI Campus"
    "via [accelerator/incubator name]" -> "Accelerator / Incubator"
    "introduced by founder of [portfolio company]" -> "Portfolio Founder"
  Use null when the channel isn't clear from the message.
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


def extract_deals(
    message_text: str, client: Anthropic | None = None
) -> list[dict[str, Any]]:
    """Send a Slack message to Claude and return a list of parsed deals.

    The list may have 0, 1, or more entries. Each entry has the same keys
    as the previous single-deal extractor returned. Empty list = chatter.
    """
    c = client or Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = c.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,  # bumped — multi-deal messages need more tokens
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

    # Accept either the new shape `{"deals": [...]}` or the legacy single
    # object shape (backward-compat in case the LLM forgets the wrapper).
    if isinstance(data, dict) and isinstance(data.get("deals"), list):
        items = data["deals"]
    elif isinstance(data, dict) and ("is_deal" in data or "company_name" in data):
        items = [data]
    else:
        items = []

    return [_normalize(item) for item in items if isinstance(item, dict)]


# Back-compat alias — older callers may still use extract_deal.
def extract_deal(
    message_text: str, client: Anthropic | None = None
) -> dict[str, Any]:
    deals = extract_deals(message_text, client=client)
    if not deals:
        return _normalize({"is_deal": False})
    return deals[0]


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
        "sourcing_channel": _normalize_sourcing_channel(
            data.get("sourcing_channel")
        ),
    }
    return out


def _normalize_sourcing_channel(v: Any) -> str | None:
    """Map the LLM's channel string to the exact spelling Pipeline expects.
    Returns None if the value doesn't match a known option."""
    from config import PIPELINE_SOURCING_CHANNELS  # local import: avoid cycle
    s = _clean_str(v)
    if not s:
        return None
    # Exact match.
    if s in PIPELINE_SOURCING_CHANNELS:
        return s
    # Case-insensitive match.
    lower = s.lower()
    for opt in PIPELINE_SOURCING_CHANNELS:
        if opt.lower() == lower:
            return opt
    return None


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
