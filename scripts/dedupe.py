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
)


@dataclass
class DedupeMatch:
    company: dict  # the Attio company record
    reason: str   # "domain" | "linkedin" | "name"
    score: float = 100.0
    in_inbound_deals: bool = False
    in_deal_pipeline: bool = False
    # True if there's an Inbound Deals entry for this company created
    # within DUPLICATE_RECENCY_DAYS. Stale matches (e.g. a 5-year-old
    # Company record with no recent Inbound activity) get this False
    # and the ingest treats the deal as fresh.
    recent_inbound: bool = False

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
    return fuzz.ratio(a.strip().lower(), b.strip().lower())


def find_duplicate(
    attio: AttioClient, deal: dict[str, Any]
) -> DedupeMatch | None:
    """Check a deal against Attio Companies. Return the first match or None."""
    # 1) domain
    domain = _normalize_domain(deal.get("domain") or deal.get("website"))
    if domain:
        for c in attio.find_companies_by_domain(domain):
            return _enrich(attio, DedupeMatch(company=c, reason="domain"))

    # 2) linkedin
    linkedin = deal.get("linkedin_url")
    if linkedin:
        for c in attio.find_companies_by_linkedin(linkedin):
            return _enrich(attio, DedupeMatch(company=c, reason="linkedin"))

    # 3) name fuzzy
    name = deal.get("company_name")
    if name:
        token = _first_significant_token(name)
        if token:
            candidates = attio.find_companies_by_name_contains(token, limit=50)
            best: DedupeMatch | None = None
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
                return _enrich(attio, best)

    return None


def _enrich(attio: AttioClient, match: DedupeMatch) -> DedupeMatch:
    """Populate list-membership and recency fields on a match."""
    cid = match.company_id
    if not cid:
        return match
    try:
        inbound_entries = attio.find_list_entries_for_company(
            INBOUND_DEALS_LIST_ID, cid
        )
    except Exception:
        inbound_entries = []
    try:
        pipeline_entries = attio.find_list_entries_for_company(
            DEAL_PIPELINE_LIST_ID, cid
        )
    except Exception:
        pipeline_entries = []

    match.in_inbound_deals = bool(inbound_entries)
    match.in_deal_pipeline = bool(pipeline_entries)
    match.recent_inbound = _has_recent_inbound(inbound_entries)
    return match


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
