"""Fuzzy dedupe of extracted deal info against existing Attio companies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from rapidfuzz import fuzz

from attio_client import AttioClient
from config import (
    DEAL_PIPELINE_LIST_ID,
    DUPLICATE_RECENCY_DAYS,
    INBOUND_DEALS_LIST_ID,
    NAME_FUZZY_THRESHOLD,
    NAME_STOP_WORDS,
    PIPELINE_TERMINAL_STATUSES,
)


@dataclass
class DedupeMatch:
    company: dict  # the Attio company record
    reason: str   # "domain" | "linkedin" | "name"
    score: float = 100.0
    in_inbound_deals: bool = False
    in_deal_pipeline: bool = False
    # Granular activity flags computed by _enrich. Drive the ingest's
    # outcome decision (Duplicate vs passed-recent vs resurface).
    recent_inbound: bool = False              # Inbound entry created ≤60d ago
    pipeline_has_active: bool = False         # Pipeline entry in any non-terminal status
    pipeline_has_recent_terminal: bool = False  # Pipeline entry in terminal status, created ≤60d
    # Earliest created_at across Inbound + Pipeline for this Company —
    # anchors the "Days since first seen" metric on each new Inbound entry.
    first_seen_at: datetime | None = None

    @property
    def company_id(self) -> str | None:
        return (self.company.get("id") or {}).get("record_id")


def _normalize_domain(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0]
    return s.rstrip("/.") or None


def _normalize_linkedin(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    s = re.sub(r"^https?://", "", s)
    s = s.split("?", 1)[0]
    return s.rstrip("/") or None


def _first_significant_token(name: str) -> str | None:
    """Return the first word of the company name that isn't a stop word."""
    if not name:
        return None
    parts = re.split(r"[\s,\.]+", name.strip())
    for p in parts:
        p_clean = re.sub(r"[^A-Za-z0-9]", "", p)
        if not p_clean:
            continue
        if p_clean.lower() in NAME_STOP_WORDS:
            continue
        return p_clean
    return None


def _score_name(a: str, b: str) -> float:
    """Token-set ratio: tolerant of extra/reordered tokens, so
    "Acme" vs "Acme Robotics" -> 100 and "Acme Robotics" vs
    "Robotics, Acme" -> 100. Much better recall than plain ratio for
    real-world company-name variation across shares."""
    return fuzz.token_set_ratio(a.strip().lower(), b.strip().lower())


def _stealth_founder_part(name: str | None) -> str | None:
    """For a stealth-style company name, return the distinguishing part.

      "Stealth (Maya Patel)"  -> "Maya Patel"
      "Stealth - Maya Patel"  -> "Maya Patel"
      "Stealth Physical AI"   -> "Physical AI"
      "Stealth"               -> ""   (bare placeholder, nothing to match on)
      "Acme Robotics"         -> None (not a stealth name)

    Used so we compare the founder/descriptor portion of two stealth
    names rather than the whole string (which all share the "Stealth"
    prefix and would falsely score high)."""
    if not name:
        return None
    if not re.match(r"^\s*stealth\b", name, re.IGNORECASE):
        return None
    paren = re.search(r"\(([^)]*)\)", name)
    if paren:
        return paren.group(1).strip()
    rest = re.sub(r"^\s*stealth\b[\s\-:–—]*", "", name, flags=re.IGNORECASE)
    return rest.strip()


def _deal_founder_linkedins(deal: dict[str, Any]) -> list[str]:
    """Normalized founder LinkedIn URLs from a deal."""
    out: list[str] = []
    for f in deal.get("founders") or []:
        li = _normalize_linkedin(f.get("linkedin"))
        if li:
            out.append(li)
    return out


def _founder_linkedin_to_company(
    inbound_index: dict[str, list[dict]] | None,
) -> dict[str, str]:
    """Build {normalized_founder_linkedin -> company_id} from the
    `founder_linkedin` field stored on existing Inbound entries. This is
    the dedupe key that actually works for stealth companies, where no
    domain or company-LinkedIn exists."""
    out: dict[str, str] = {}
    if not inbound_index:
        return out
    for cid, entries in inbound_index.items():
        for e in entries:
            ev = e.get("entry_values") or {}
            raw = _first_text_value(ev.get("founder_linkedin"))
            if not raw:
                continue
            for url in raw.split():
                n = _normalize_linkedin(url)
                if n and n not in out:
                    out[n] = cid
    return out


def find_duplicate(
    attio: AttioClient,
    deal: dict[str, Any],
    *,
    inbound_index: dict[str, list[dict]] | None = None,
    pipeline_index: dict[str, list[dict]] | None = None,
) -> DedupeMatch | None:
    """Check a deal against Attio Companies. Return the first match or None.

    inbound_index / pipeline_index are optional pre-built {company_id ->
    [entries]} maps from AttioClient.build_company_index. When provided,
    enrichment skips paginating those lists per match. Useful when many
    deals are processed in a single run (e.g. one Slack message containing
    a list of 10+ deals).
    """
    # 1) domain — most authoritative for real companies.
    domain = _normalize_domain(deal.get("domain") or deal.get("website"))
    if domain:
        for c in attio.find_companies_by_domain(domain):
            return _enrich(
                attio,
                DedupeMatch(company=c, reason="domain"),
                inbound_index=inbound_index,
                pipeline_index=pipeline_index,
            )

    # 2) company LinkedIn page.
    linkedin = deal.get("linkedin_url")
    if linkedin:
        for c in attio.find_companies_by_linkedin(linkedin):
            return _enrich(
                attio,
                DedupeMatch(company=c, reason="linkedin"),
                inbound_index=inbound_index,
                pipeline_index=pipeline_index,
            )

    # 3) founder LinkedIn — the reliable key for stealth companies, which
    # have no domain and no company-LinkedIn. Match against founder
    # LinkedIns recorded on prior Inbound entries.
    founder_lis = _deal_founder_linkedins(deal)
    if founder_lis:
        li_map = _founder_linkedin_to_company(inbound_index)
        for li in founder_lis:
            cid = li_map.get(li)
            if cid:
                minimal = {"id": {"record_id": cid}}
                return _enrich(
                    attio,
                    DedupeMatch(company=minimal, reason="founder_linkedin"),
                    inbound_index=inbound_index,
                    pipeline_index=pipeline_index,
                )

    # 4) name fuzzy.
    name = deal.get("company_name")
    if name:
        stealth_part = _stealth_founder_part(name)
        if stealth_part is not None:
            # Stealth-style name. Bare "Stealth" (empty founder part) is a
            # generic placeholder — never name-match it (would collide with
            # unrelated stealth companies). Otherwise compare the founder /
            # descriptor portion against other stealth records' portions.
            if not stealth_part:
                return None
            candidates = attio.find_companies_by_name_contains("Stealth")
            best: DedupeMatch | None = None
            for c in candidates:
                cand_part = _stealth_founder_part(AttioClient.company_name(c))
                if not cand_part:
                    continue
                score = _score_name(stealth_part, cand_part)
                if score >= NAME_FUZZY_THRESHOLD and (
                    best is None or score > best.score
                ):
                    best = DedupeMatch(company=c, reason="stealth_name", score=score)
            if best:
                return _enrich(
                    attio, best,
                    inbound_index=inbound_index,
                    pipeline_index=pipeline_index,
                )
            return None

        token = _first_significant_token(name)
        if token:
            candidates = attio.find_companies_by_name_contains(token)
            best = None
            for c in candidates:
                cand_name = AttioClient.company_name(c)
                if not cand_name:
                    continue
                score = _score_name(name, cand_name)
                if score >= NAME_FUZZY_THRESHOLD and (
                    best is None or score > best.score
                ):
                    best = DedupeMatch(company=c, reason="name", score=score)
            if best:
                return _enrich(
                    attio,
                    best,
                    inbound_index=inbound_index,
                    pipeline_index=pipeline_index,
                )

    return None


def _enrich(
    attio: AttioClient,
    match: DedupeMatch,
    *,
    inbound_index: dict[str, list[dict]] | None = None,
    pipeline_index: dict[str, list[dict]] | None = None,
) -> DedupeMatch:
    """Populate list-membership and recency / status fields on a match.

    If inbound_index / pipeline_index are supplied, the relevant entries
    are pulled from there (O(1)) instead of paginating the list per
    match. Falls back to paginating via find_list_entries_for_company
    when no index is available.
    """
    cid = match.company_id
    if not cid:
        return match
    if inbound_index is not None:
        inbound_entries = list(inbound_index.get(cid, []))
    else:
        try:
            inbound_entries = attio.find_list_entries_for_company(
                INBOUND_DEALS_LIST_ID, cid
            )
        except Exception:
            inbound_entries = []
    if pipeline_index is not None:
        pipeline_entries = list(pipeline_index.get(cid, []))
    else:
        try:
            pipeline_entries = attio.find_list_entries_for_company(
                DEAL_PIPELINE_LIST_ID, cid
            )
        except Exception:
            pipeline_entries = []

    match.in_inbound_deals = bool(inbound_entries)
    match.in_deal_pipeline = bool(pipeline_entries)
    match.recent_inbound = _has_recent_inbound(inbound_entries)
    match.pipeline_has_active = _has_active_pipeline_entry(pipeline_entries)
    match.pipeline_has_recent_terminal = _has_recent_terminal_pipeline_entry(
        pipeline_entries
    )
    # `first_seen_at` anchors days_since_first_seen and only counts
    # actual list activity — earliest Inbound or Pipeline entry. We
    # deliberately do NOT include the Companies record's own
    # created_at: a Company can land in Attio for various reasons
    # (email-thread auto-creation, manual entry) without ever having
    # been a deal. We only care about "the company has been a deal
    # before" as the signal for both `days_since_first_seen` and the
    # duplicate / resurface classification.
    match.first_seen_at = _earliest_created_at(
        inbound_entries + pipeline_entries
    )
    return match


def _earliest_created_at(entries: list[dict]) -> datetime | None:
    """Return the oldest created_at from a flat list of list entries."""
    candidates: list[datetime] = []
    for entry in entries:
        ts = _entry_created_at(entry)
        if ts:
            candidates.append(ts)
    return min(candidates) if candidates else None


def extract_inbound_step(entry: dict) -> str | None:
    """Pull the Step title (e.g. 'New', 'Duplicate') from an Inbound
    Deals list entry. Step is a select attribute; the response shape
    varies by surface so probe a few."""
    ev = (entry or {}).get("entry_values") or {}
    raw = ev.get("step")
    if not raw:
        return None
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return None
    option = raw.get("option")
    if isinstance(option, dict):
        return option.get("title") or option.get("value")
    return raw.get("title") or raw.get("value")


def extract_pipeline_stage(entry: dict) -> str | None:
    """Public re-export — same as the internal _extract_pipeline_stage,
    used by promote.py during the cleanup pass."""
    return _extract_pipeline_stage(entry)


def _has_recent_inbound(entries: list[dict]) -> bool:
    """True if any entry was created within the last DUPLICATE_RECENCY_DAYS."""
    if not entries:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=DUPLICATE_RECENCY_DAYS)
    for entry in entries:
        ts = _entry_created_at(entry)
        if ts and ts >= cutoff:
            return True
    return False


def _has_active_pipeline_entry(entries: list[dict]) -> bool:
    """True if any Pipeline entry has a non-terminal status (anything
    except Passed / Lost). Captures 'currently being worked on' regardless
    of when the entry was created."""
    for entry in entries:
        stage = _extract_pipeline_stage(entry)
        if stage and stage not in PIPELINE_TERMINAL_STATUSES:
            return True
    return False


def _has_recent_terminal_pipeline_entry(entries: list[dict]) -> bool:
    """True if any Pipeline entry is in a terminal status (Passed/Lost)
    AND was created within the last DUPLICATE_RECENCY_DAYS. Captures
    'we passed on this recently'."""
    if not entries:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=DUPLICATE_RECENCY_DAYS)
    for entry in entries:
        stage = _extract_pipeline_stage(entry)
        if not stage or stage not in PIPELINE_TERMINAL_STATUSES:
            continue
        ts = _entry_created_at(entry)
        if ts and ts >= cutoff:
            return True
    return False


def _extract_pipeline_stage(entry: dict) -> str | None:
    """Pull the status title (e.g. "Outreach", "Passed") from a Pipeline
    entry's `stage` attribute. The api_slug for the Pipeline Status field
    is `stage` (yes, confusingly). Status comes back in different shapes
    across the API, so probe a few."""
    ev = (entry or {}).get("entry_values") or {}
    raw = ev.get("stage")
    if not raw:
        return None
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        return None
    inner_status = raw.get("status")
    if isinstance(inner_status, dict):
        return inner_status.get("title") or inner_status.get("name")
    return raw.get("title") or raw.get("value") or raw.get("name")


def _entry_created_at(entry: dict) -> datetime | None:
    """Pull a UTC datetime out of an entry's `created_at` value."""
    raw = (entry or {}).get("created_at")
    if not raw:
        # Some Attio responses nest entry attributes under entry_values.
        raw = ((entry or {}).get("entry_values") or {}).get("created_at")
    iso = _first_text_value(raw)
    if not iso:
        return None
    return _parse_iso_utc(iso)


def _first_text_value(v: Any) -> str | None:
    if not v:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, list) and v:
        return _first_text_value(v[0])
    if isinstance(v, dict):
        return v.get("value") or v.get("formatted")
    return None


def _parse_iso_utc(iso: str) -> datetime | None:
    """Parse an ISO 8601 timestamp; ensure UTC tzinfo."""
    s = iso.strip()
    # Python 3.11 fromisoformat handles trailing 'Z' from 3.11+, but normalise
    # for safety on older fields like ".000Z".
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def location_label(match: DedupeMatch) -> str:
    """'Inbound Deals', 'Deal Pipeline', 'both', or 'Companies only'."""
    if match.in_inbound_deals and match.in_deal_pipeline:
        return "both"
    if match.in_deal_pipeline:
        return "Deal Pipeline"
    if match.in_inbound_deals:
        return "Inbound Deals"
    return "Companies only"
