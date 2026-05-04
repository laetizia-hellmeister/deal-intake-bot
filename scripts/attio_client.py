"""Thin wrapper over the Attio v2 REST API.

Only the endpoints the bot actually needs are implemented.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

import httpx

from config import (
    ATTIO_API_BASE,
    ATTIO_API_KEY,
    DEAL_PIPELINE_LIST_ID,
    INBOUND_DEALS_LIST_ID,
    PARENT_OBJECT,
)


# HTTP statuses we retry once on, after a short pause. Attio occasionally
# returns transient 401s (with a message like "API Key provided could not
# be found") and the usual 5xx server-side hiccups. A single quick retry
# keeps these from permanently dropping a deal.
_RETRYABLE_STATUSES = {401, 429, 500, 502, 503, 504}


class AttioError(Exception):
    """Raised on non-2xx responses from Attio."""


class AttioClient:
    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=ATTIO_API_BASE,
            headers={
                "Authorization": f"Bearer {api_key or ATTIO_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AttioClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low-level ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        # First attempt.
        r = self._client.request(method, path, **kwargs)
        if r.status_code in _RETRYABLE_STATUSES:
            # One brief retry — clears most transient Attio hiccups.
            print(
                f"[attio] {method} {path} -> {r.status_code}, retrying in 2s"
            )
            time.sleep(2.0)
            r = self._client.request(method, path, **kwargs)
        if r.status_code >= 400:
            raise AttioError(
                f"Attio {method} {path} -> {r.status_code}: {r.text}"
            )
        if not r.content:
            return {}
        return r.json()

    # -- companies ---------------------------------------------------------

    def query_companies(self, filter_: dict, limit: int = 25) -> list[dict]:
        """POST /objects/companies/records/query with a filter."""
        body = {"filter": filter_, "limit": limit}
        data = self._request("POST", "/objects/companies/records/query", json=body)
        return data.get("data", [])

    def find_companies_by_domain(self, domain: str) -> list[dict]:
        if not domain:
            return []
        return self.query_companies({"domains": {"domain": domain}}, limit=5)

    def find_companies_by_linkedin(self, linkedin_url: str) -> list[dict]:
        if not linkedin_url:
            return []
        return self.query_companies({"linkedin": linkedin_url}, limit=5)

    def find_companies_by_name_contains(self, token: str, limit: int = 50) -> list[dict]:
        if not token:
            return []
        filt = {"name": {"$contains": token}}
        return self.query_companies(filt, limit=limit)

    # -- people ------------------------------------------------------------

    def query_people(self, filter_: dict, limit: int = 25) -> list[dict]:
        body = {"filter": filter_, "limit": limit}
        data = self._request("POST", "/objects/people/records/query", json=body)
        return data.get("data", [])

    def find_people_by_linkedin(self, linkedin_url: str) -> list[dict]:
        if not linkedin_url:
            return []
        return self.query_people({"linkedin": linkedin_url}, limit=5)

    def find_people_by_name_contains(
        self, token: str, limit: int = 50
    ) -> list[dict]:
        """Search People whose full_name contains a token.

        People `name` is a `personal-name` attribute; the contains filter
        is keyed on `full_name`. Falls back to a simpler form on 400.
        """
        if not token:
            return []
        try:
            return self.query_people(
                {"name": {"full_name": {"$contains": token}}}, limit=limit
            )
        except AttioError:
            try:
                return self.query_people(
                    {"name": {"$contains": token}}, limit=limit
                )
            except AttioError:
                return []

    @staticmethod
    def person_name(record: dict) -> str | None:
        values = (record or {}).get("values") or {}
        items = values.get("name") or []
        if items and isinstance(items, list):
            v = items[0]
            return v.get("full_name") or v.get("value")
        return None

    def create_company(self, values: dict[str, Any]) -> dict:
        """POST /objects/companies/records — returns the created record."""
        body = {"data": {"values": values}}
        data = self._request("POST", "/objects/companies/records", json=body)
        return data.get("data", {})

    def assert_company(self, values: dict[str, Any], matching: str = "domains") -> dict:
        """PUT /objects/companies/records with a matching_attribute — upserts."""
        body = {"data": {"values": values}}
        path = f"/objects/companies/records?matching_attribute={matching}"
        data = self._request("PUT", path, json=body)
        return data.get("data", {})

    # -- list entries ------------------------------------------------------

    def query_list_entries(
        self, list_id: str, filter_: dict | None = None, limit: int = 50
    ) -> list[dict]:
        body: dict[str, Any] = {"limit": limit}
        if filter_:
            body["filter"] = filter_
        data = self._request(
            "POST", f"/lists/{list_id}/entries/query", json=body
        )
        return data.get("data", [])

    def find_list_entries_for_company(
        self, list_id: str, company_record_id: str
    ) -> list[dict]:
        filt = {"parent_record_id": company_record_id}
        return self.query_list_entries(list_id, filter_=filt, limit=5)

    def add_record_to_list(
        self,
        list_id: str,
        parent_record_id: str,
        entry_values: dict[str, Any] | None = None,
        parent_object: str = PARENT_OBJECT,
        allow_duplicates: bool = False,
    ) -> dict:
        """POST /lists/{list_id}/entries — adds an existing record to a list."""
        body = {
            "data": {
                "parent_record_id": parent_record_id,
                "parent_object": parent_object,
                "entry_values": entry_values or {},
            }
        }
        if not allow_duplicates:
            # Attio accepts a query parameter flag
            path = f"/lists/{list_id}/entries?allow_duplicates=false"
        else:
            path = f"/lists/{list_id}/entries"
        data = self._request("POST", path, json=body)
        return data.get("data", {})

    def update_list_entry(
        self, list_id: str, entry_id: str, entry_values: dict[str, Any]
    ) -> dict:
        body = {"data": {"entry_values": entry_values}}
        data = self._request(
            "PATCH", f"/lists/{list_id}/entries/{entry_id}", json=body
        )
        return data.get("data", {})

    # -- convenience -------------------------------------------------------

    def inbound_deals_entries_to_promote(self, limit: int = 50) -> list[dict]:
        """Inbound Deals entries whose Step == 'Add to pipeline'."""
        filt = {"step": "Add to pipeline"}
        return self.query_list_entries(
            INBOUND_DEALS_LIST_ID, filter_=filt, limit=limit
        )

    @staticmethod
    def record_id(record: dict) -> str | None:
        """Return the record_id uuid from a response object."""
        return (record.get("id") or {}).get("record_id")

    @staticmethod
    def entry_id(entry: dict) -> str | None:
        return (entry.get("id") or {}).get("entry_id")

    @staticmethod
    def parent_record_id(entry: dict) -> str | None:
        pid = entry.get("parent_record_id")
        if pid:
            return pid
        # Fallback — some responses nest it
        return (entry.get("parent") or {}).get("record_id")

    @staticmethod
    def company_name(record: dict) -> str | None:
        values = record.get("values", {}) or {}
        name_list = values.get("name") or []
        if name_list and isinstance(name_list, list):
            v = name_list[0]
            return v.get("value") or v.get("full_name")
        return None

    @staticmethod
    def company_domains(record: dict) -> list[str]:
        values = record.get("values", {}) or {}
        out = []
        for d in values.get("domains") or []:
            dom = d.get("domain") or d.get("value")
            if dom:
                out.append(dom)
        return out

    @staticmethod
    def company_linkedin(record: dict) -> str | None:
        values = record.get("values", {}) or {}
        items = values.get("linkedin") or []
        if items and isinstance(items, list):
            return items[0].get("value")
        return None

    @staticmethod
    def company_web_url(record_id: str) -> str:
        """User-facing Attio URL for a company record."""
        return f"https://app.attio.com/_/objects/companies/record/{record_id}"
