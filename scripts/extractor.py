"""Claude-based extraction of structured deal info from a Slack message."""

from __future__ import annotations

import base64
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
      "sourcing_channel": string | null,
      "geo_skip": boolean,
      "direct_to_pipeline": boolean,
      "assigned_user_ids": [string]
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
- domain = root domain only (strip protocol, www., path, query). Set to null
  if the only URL is a personal LinkedIn (linkedin.com/in/...) — that's not
  a company website.
- linkedin_url = the COMPANY's LinkedIn page only (typically
  linkedin.com/company/... or linkedin.com/showcase/...). DO NOT put
  a personal LinkedIn profile (linkedin.com/in/...) here — personal
  profiles belong in founders[].linkedin. Set to null if no company
  LinkedIn page is mentioned.
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
- geo_skip = true ONLY if the deal entry is explicitly prefixed with a
  US country flag emoji (🇺🇸 or the Slack shortcode :us:) directly
  before the company name. We're a Europe-focused fund and want to
  drop US deals; the flag emoji is the only signal. Do NOT infer
  US-ness from company name, domain, or description — set this true
  only when the flag emoji is literally present immediately before
  the deal. Otherwise false / null.
- direct_to_pipeline = true if the message contains an explicit
  directive that these deals should bypass triage and go straight to
  the main Deal Pipeline (skipping the usual "New → manual review"
  step). The user is signaling they've already reached out / decided
  to engage. Look for explicit phrases like:
    "add to pipeline"
    "directly to pipeline" / "direct to pipeline"
    "skip triage"
    "already reached out"
    "promote this/these"
    "#promote" / "[promote]"
  Only set true when the phrase is clearly a DIRECTIVE about adding
  these deals — not when discussing pipeline mechanics in passing
  ("the founder wants to add us to their pipeline" -> false).
  CAN BE PER-DEAL. If the directive applies to one or more specific
  deals in a list (e.g. "Add Acme to pipeline, the rest for triage",
  or "Acme — already reached out; Beta — to triage"), set
  direct_to_pipeline only on those specific deals. If the directive
  applies to the whole message uniformly, set it on every deal.
  Otherwise false / null.
- assigned_user_ids = list of Slack user IDs (U… or W… — the bit
  between <@ and > in mention codes like <@U093USR1DDE>) that are
  specifically assigned to this individual deal. Use this when the
  message routes different deals to different people, e.g.
    "@pranav take Acme. @shrey take Beta."
    "deals for @pranav, except Acme is mine: @laetizia"
  → Acme gets assigned_user_ids=["U_laetizia_id"],
    Beta gets ["U_shrey_id"], all others get ["U_pranav_id"].
  Leave empty when the @-mentions apply to the whole message
  uniformly ("deals for @pranav: Acme, Beta, …") — the bot picks
  up message-level mentions automatically. Default: empty list.
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
        pass

    # Fallback 1: locate outermost {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ExtractionError(f"No JSON object in response: {text[:200]}")
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        # Fallback 2: try to recover from a truncated `deals: [...]` list
        # by chopping off after the last complete inner object and closing
        # the array + outer braces. Handles the "max_tokens cut us off
        # mid-deal" case so we keep most of the deals.
        repaired = _repair_truncated_deals_json(candidate)
        if repaired:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        raise ExtractionError(
            f"Could not parse JSON: {e} | head: {candidate[:200]!r} | "
            f"tail: {candidate[-200:]!r}"
        ) from e


def _repair_truncated_deals_json(text: str) -> str | None:
    """If the JSON is shaped like {"deals": [<obj>, <obj>, <obj truncated]}
    we keep only the complete inner objects and close the brackets.
    Returns the repaired string, or None if we can't make sense of it."""
    # Find the position right after the last complete inner deal object —
    # i.e. depth comes back to 1 (inside the deals array) after a '}'.
    depth = 0
    in_string = False
    escape = False
    last_complete_end = -1
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 1:
                last_complete_end = i
    if last_complete_end == -1:
        return None
    # Append closing bracket + brace to make it parseable.
    return text[: last_complete_end + 1] + "]}"


def extract_deals(
    message_text: str,
    client: Anthropic | None = None,
    documents: list[tuple[str, bytes]] | None = None,
) -> list[dict[str, Any]]:
    """Send a Slack message to Claude and return a list of parsed deals.

    The list may have 0, 1, or more entries. Each entry has the same keys
    as the previous single-deal extractor returned. Empty list = chatter.

    documents: optional list of (mime_type, file_bytes) tuples for any
    attachments (typically PDF pitchdecks/memos). Each gets passed to
    Claude as a document content block so it can read layout + images
    natively — much better than parsing locally and feeding text. The
    LLM treats the documents as additional context for deal extraction
    alongside the Slack message text. Only mime types Claude supports
    are forwarded; unsupported types are silently skipped.
    """
    c = client or Anthropic(api_key=ANTHROPIC_API_KEY)
    content = _build_user_content(message_text, documents or [])
    resp = c.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16384,  # generous — large fund-update lists can hit ~30 deals
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    # Concatenate all text blocks
    parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    raw = "".join(parts)
    if not raw.strip():
        raise ExtractionError("Empty response from Claude")
    try:
        data = _extract_json_object(raw)
    except ExtractionError:
        # Surface the raw response in the run log so we can diagnose what
        # the LLM produced when parsing fails.
        print(
            f"[extractor] JSON parse failed. "
            f"Raw length={len(raw)}, head={raw[:300]!r}, tail={raw[-300:]!r}"
        )
        raise

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
        "geo_skip": bool(data.get("geo_skip")),
        "direct_to_pipeline": bool(data.get("direct_to_pipeline")),
        "assigned_user_ids": _normalize_user_ids(data.get("assigned_user_ids")),
    }
    return out


# Anthropic claude-opus document support: PDF natively. Other types
# (PPTX, DOCX, etc.) would need conversion or Files-API upload, which
# we're not building yet — they're silently skipped with a log line.
_SUPPORTED_DOC_MIMES = {"application/pdf"}


def _build_user_content(text: str, documents: list[tuple[str, bytes]]) -> list[dict]:
    """Build the user-message `content` array. Documents (if any) go
    first as document content blocks; the Slack text follows.

    If no documents are attached, returns a single text block so the
    request shape stays minimal for the common case.
    """
    if not documents:
        return [{"type": "text", "text": text or ""}]

    blocks: list[dict] = []
    for mime, data in documents:
        if mime not in _SUPPORTED_DOC_MIMES:
            print(
                f"[extractor] skipping attachment of unsupported mime "
                f"{mime!r} ({len(data)} bytes)"
            )
            continue
        b64 = base64.standard_b64encode(data).decode("ascii")
        blocks.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": b64,
                },
            }
        )

    # Always include a text block after documents — when the Slack
    # message itself has no text, prompt Claude to extract from the
    # attachment alone.
    blocks.append(
        {
            "type": "text",
            "text": (
                text
                if text and text.strip()
                else "(No text in the message — extract any deals from the "
                "attached document(s).)"
            ),
        }
    )
    return blocks


def _normalize_user_ids(v: Any) -> list[str]:
    """Filter the raw `assigned_user_ids` list to plausible Slack user IDs.
    Tolerates the LLM occasionally returning a `<@U_xxx>` wrapper or
    extra whitespace."""
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for item in v:
        if not isinstance(item, str):
            continue
        s = item.strip()
        # Strip <@…> wrapper if the model included it.
        m = re.match(r"^<@?([UW][A-Z0-9]+)>?$", s)
        if m:
            out.append(m.group(1))
            continue
        if re.match(r"^[UW][A-Z0-9]+$", s):
            out.append(s)
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
