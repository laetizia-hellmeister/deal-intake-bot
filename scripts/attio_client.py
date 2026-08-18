"""Thin wrapper over the Attio v2 REST API.

Only the endpoints the bot actually needs are implemented.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
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
        self._api_key = api_key or ATTIO_API_KEY
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=ATTIO_API_BASE,
            headers={
                "Authorization": f"Bearer {self._api_key}",
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

    # -- generic record fetch -----------------------------------------

    def get_record(self, object_slug: str, record_id: str) -> dict | None:
        """GET /objects/{object_slug}/records/{record_id} — returns the
        record (with values) or None if not found."""
        if not record_id:
            return None
        try:
            data = self._request(
                "GET", f"/objects/{object_slug}/records/{record_id}"
            )
        except AttioError as e:
            print(f"[attio] get_record({object_slug}, {record_id}) failed: {e}")
            return None
        return data.get("data")

    # -- companies ---------------------------------------------------------

    def query_companies(
        self, filter_: dict, limit: int = 25, offset: int = 0
    ) -> list[dict]:
        """POST /objects/companies/records/query with a filter."""
        body = {"filter": filter_, "limit": limit, "offset": offset}
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

    def find_companies_by_name_contains(
        self, token: str, limit: int = 300
    ) -> list[dict]:
        """Companies whose name contains `token`. Paginates so a common
        token (e.g. "Stealth", shared by many records) doesn't silently
        truncate at the first page and hide the real match."""
        if not token:
            return []
        filt = {"name": {"$contains": token}}
        out: list[dict] = []
        page_size = 100
        offset = 0
        while len(out) < limit:
            page = self.query_companies(
                filt, limit=min(page_size, limit - len(out)), offset=offset
            )
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return out

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
        self,
        list_id: str,
        filter_: dict | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        body: dict[str, Any] = {"limit": limit, "offset": offset}
        if filter_:
            body["filter"] = filter_
        data = self._request(
            "POST", f"/lists/{list_id}/entries/query", json=body
        )
        return data.get("data", [])

    def build_company_index(
        self,
        list_id: str,
        page_size: int = 500,
        max_scan: int = 50_000,
    ) -> dict[str, list[dict]]:
        """Fetch ALL entries from a list (paginated) and group by
        parent_record_id. Used by code paths that need to look up many
        companies at once (cleanup, multi-deal ingest) — one paginated
        pass per list, then O(1) lookups instead of paginating per
        company.

        Returns {company_record_id: [entries...]}. Companies with no
        entries simply have no key (a `dict.get(cid, [])` works as the
        client-side fallback)."""
        from collections import defaultdict
        index: dict[str, list[dict]] = defaultdict(list)
        offset = 0
        scanned = 0
        while scanned < max_scan:
            try:
                page = self.query_list_entries(
                    list_id, filter_=None, limit=page_size, offset=offset
                )
            except Exception as e:
                print(f"[index] failed fetching {list_id} at offset {offset}: {e}")
                break
            if not page:
                break
            for entry in page:
                cid = AttioClient.parent_record_id(entry)
                if cid:
                    index[cid].append(entry)
            scanned += len(page)
            if len(page) < page_size:
                break
            offset += page_size
        return index

    def find_list_entries_for_company(
        self, list_id: str, company_record_id: str, limit: int = 50
    ) -> list[dict]:
        """Return list entries whose parent record is the given Company.

        Attio's POST /lists/{list_id}/entries/query `filter` parameter
        only operates on attribute values (entry_values like step,
        source). It silently ignores entry-level metadata such as
        parent_record_id, so server-side filtering on it returns zero
        matches every time. We have to paginate the full list and
        filter client-side.

        Iterates pages of 500 until either:
          - we've collected `limit` matches,
          - a page comes back smaller than the page size (last page), or
          - we've scanned MAX_SCAN entries (safety stop on huge lists).

        For the Deal Pipeline (years of history, thousands of entries),
        this can cost several API calls per call. That's still fast in
        absolute terms and fine for daily ingest / promote runs.
        """
        PAGE_SIZE = 500
        MAX_SCAN = 50_000  # ~100 pages — pragmatic upper bound
        matches: list[dict] = []
        offset = 0
        scanned = 0
        while scanned < MAX_SCAN:
            page = self.query_list_entries(
                list_id, filter_=None, limit=PAGE_SIZE, offset=offset
            )
            if not page:
                break
            for entry in page:
                if AttioClient.parent_record_id(entry) == company_record_id:
                    matches.append(entry)
                    if len(matches) >= limit:
                        return matches
            scanned += len(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return matches

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

    def get_list_entry_by_id(self, list_id: str, entry_id: str) -> dict | None:
        """Fetch a single list entry by its own entry_id. `entry_id` is a
        real, unique, queryable text attribute on every list entry (unlike
        `parent_record_id`, which is entry-level metadata that Attio's
        filter silently ignores — see find_list_entries_for_company's
        docstring) so a plain filtered query is reliable here."""
        matches = self.query_list_entries(
            list_id, filter_={"entry_id": entry_id}, limit=1
        )
        return matches[0] if matches else None

    # -- files -------------------------------------------------------------
    # Attio's documented Files API is read-only plus "connect a file that
    # already lives in Drive/Dropbox/Box/OneDrive". Raw uploads go through
    # the undocumented POST /v2/files/upload, which takes multipart/form-data
    # and stores the bytes natively (storage_provider "attio"), exactly as a
    # drag-and-drop into the record's Files tab does.

    def list_record_files(
        self, record_id: str, object_slug: str = PARENT_OBJECT
    ) -> list[dict]:
        """Files already attached to a record. Used to avoid re-uploading
        the same deck when a Slack message gets reprocessed."""
        if not record_id:
            return []
        try:
            data = self._request(
                "GET",
                "/files",
                params={"object": object_slug, "record_id": record_id},
            )
        except AttioError as e:
            print(f"[attio] list_record_files({record_id}) failed: {e}")
            return []
        return data.get("data", []) or []

    def upload_record_file(
        self,
        record_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        object_slug: str = PARENT_OBJECT,
    ) -> dict | None:
        """Upload bytes into a record's Files tab. Returns the created file
        object, or None on failure.

        Needs its own httpx call rather than `_request`: the shared client
        pins `Content-Type: application/json`, and httpx has to set the
        multipart content type itself so it can include the boundary.
        """
        if not record_id or not content:
            return None
        url = f"{ATTIO_API_BASE}/files/upload"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        files = {"file": (filename, content, content_type)}
        data = {"object": object_slug, "record_id": record_id}
        for attempt in (1, 2):
            try:
                r = httpx.post(
                    url,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=max(self._timeout, 120.0),
                )
            except Exception as e:
                print(f"[attio] file upload {filename!r} error: {e}")
                return None
            if r.status_code in _RETRYABLE_STATUSES and attempt == 1:
                print(
                    f"[attio] file upload {filename!r} -> {r.status_code}, "
                    "retrying in 2s"
                )
                time.sleep(2.0)
                continue
            if r.status_code >= 400:
                print(
                    f"[attio] file upload {filename!r} -> {r.status_code}: "
                    f"{r.text[:300]}"
                )
                return None
            return (r.json() or {}).get("data")
        return None

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
        # Some API surfaces nest the parent under different keys.
        for key in ("parent_record", "parent"):
            inner = entry.get(key)
            if isinstance(inner, dict):
                rid = inner.get("record_id")
                if rid:
                    return rid
        return None

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

    @staticmethod
    def person_linkedin(record: dict) -> str | None:
        """Mirrors company_linkedin, for a Person record."""
        values = (record or {}).get("values") or {}
        items = values.get("linkedin") or []
        if items and isinstance(items, list):
            return items[0].get("value")
        return None

    @staticmethod
    def company_team_ids(company_record: dict) -> set[str]:
        """Extract the set of team-member Person record_ids from a Company.
        Shared by promote.py (matching a shared VC's mentioned contact
        against a company's team) and outreach_chase.py (resolving a
        company's linked founder for the digest's LinkedIn link)."""
        values = (company_record or {}).get("values") or {}
        team = values.get("team") or []
        if not isinstance(team, list):
            return set()
        out: set[str] = set()
        for ref in team:
            if not isinstance(ref, dict):
                continue
            rid = ref.get("target_record_id")
            if not rid:
                inner = ref.get("target") or {}
                rid = inner.get("record_id")
            if rid:
                out.add(rid)
        return out

    # -- list entry read-shape helpers --------------------------------------
    # Generic readers for a list entry's `entry_values`. Attio's read shape
    # differs by attribute type (status/select wrap the value in
    # {"status"/"option": {...}}, plain text/date attributes are simpler),
    # so these centralize the parsing that used to be duplicated per-script.

    @staticmethod
    def _first_text(v: Any) -> str | None:
        if not v:
            return None
        if isinstance(v, str):
            return v.strip() or None
        if isinstance(v, list) and v:
            return AttioClient._first_text(v[0])
        if isinstance(v, dict):
            return v.get("value") or v.get("formatted") or None
        return None

    @staticmethod
    def entry_text_value(entry: dict, slug: str) -> str | None:
        """Plain-text field reader, e.g. for `next_steps`."""
        values = (entry or {}).get("entry_values") or {}
        return AttioClient._first_text(values.get(slug))

    @staticmethod
    def entry_status_value(entry: dict, slug: str = "stage") -> str | None:
        """Status/select field reader, e.g. for `stage`. Read shape is
        `[{"status": {"title": "..."}}]` (status attrs) or
        `[{"option": {"title": "..."}}]` (plain select attrs)."""
        values = (entry or {}).get("entry_values") or {}
        raw = values.get(slug)
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict):
            return None
        inner = raw.get("status") or raw.get("option")
        if isinstance(inner, dict):
            return inner.get("title") or inner.get("name")
        return raw.get("title") or raw.get("value")

    @staticmethod
    def entry_date_value(entry: dict, slug: str) -> date | None:
        """Date field reader, e.g. for `last_chased` / `review_date`."""
        text = AttioClient.entry_text_value(entry, slug)
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

    @staticmethod
    def entry_created_at(entry: dict) -> datetime | None:
        """The list entry's own `created_at` metadata (when it was added
        to the list) — a UTC-aware datetime, or None."""
        raw = (entry or {}).get("created_at")
        if not raw:
            raw = ((entry or {}).get("entry_values") or {}).get("created_at")
        text = AttioClient._first_text(raw)
        if not text:
            return None
        s = text.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
