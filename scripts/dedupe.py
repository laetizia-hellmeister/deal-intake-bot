"""Fuzzy dedupe of extracted deal info against existing Attio companies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from attio_client import AttioClient
from config import (
    DEAL_PIPELINE_LIST_ID,
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
    """Populate `in_inbound_deals` / `in_deal_pipeline` for a match."""
    cid = match.company_id
    if not cid:
        return match
    try:
        match.in_inbound_deals = bool(
            attio.find_list_entries_for_company(INBOUND_DEALS_LIST_ID, cid)
        )
        match.in_deal_pipeline = bool(
            attio.find_list_entries_for_company(DEAL_PIPELINE_LIST_ID, cid)
        )
    except Exception:
        # Enrichment is best-effort; don't let it hide the dedupe result.
        pass
    return match


def location_label(match: DedupeMatch) -> str:
    """'Inbound Deals', 'Deal Pipeline', 'both', or 'Companies only'."""
    if match.in_inbound_deals and match.in_deal_pipeline:
        return "both"
    if match.in_deal_pipeline:
        return "Deal Pipeline"
    if match.in_inbound_deals:
        return "Inbound Deals"
    return "Companies only"
