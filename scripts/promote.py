"""Promote workflow entry point.

Runs daily at 17:00 Europe/Copenhagen (gated in-script to handle DST).
Moves Inbound Deals entries whose Step == "Add to pipeline" into the main
Deal Pipeline, then flips the Inbound Step to "Added".

SAFETY: this script MUST NOT call update/delete endpoints on Deal Pipeline.
The only Deal Pipeline endpoint used here is add-record-to-list.
"""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from attio_client import AttioClient, AttioError
from config import (
    DEAL_PIPELINE_LIST_ID,
    INBOUND_DEALS_LIST_ID,
    IN_SCOPE_STAGES,
    PARENT_OBJECT,
    PIPELINE_STAGE_NEW,
    STEP_ADDED,
)
from slack_client import SlackClient


def main() -> int:
    # DST gate: GitHub cron is UTC only; we schedule twice and exit silently
    # on the run that isn't local 17:00 Copenhagen.
    now_local = datetime.now(ZoneInfo("Europe/Copenhagen"))
    if now_local.hour != 17:
        print(f"Skipping — local hour is {now_local.hour}, not 17")
        return 0

    attio = AttioClient()
    slack = SlackClient()

    try:
        entries = attio.inbound_deals_entries_to_promote(limit=50)
    except AttioError as e:
        print(f"Failed to fetch Inbound Deals: {e}")
        attio.close()
        return 1

    promoted: list[str] = []
    failed: list[tuple[str, str]] = []

    for entry in entries:
        entry_id = AttioClient.entry_id(entry)
        company_id = AttioClient.parent_record_id(entry)
        company_name = _resolve_company_name(attio, entry, company_id)

        if not entry_id or not company_id:
            print(f"Skipping malformed entry: {entry}")
            continue

        try:
            _promote_one(attio, company_id, entry)
        except AttioError as e:
            msg = str(e)
            if _is_already_exists(msg):
                print(f"{company_name}: already in Deal Pipeline — marking Added")
                _mark_added(attio, entry_id, failed, company_name)
                promoted.append(company_name)
                continue
            print(f"{company_name}: promotion failed: {msg}")
            failed.append((company_name, _short(msg)))
            continue
        except Exception as e:
            print(f"{company_name}: unexpected error: {e}")
            failed.append((company_name, _short(str(e))))
            continue

        _mark_added(attio, entry_id, failed, company_name)
        promoted.append(company_name)

    _post_summary(slack, promoted, failed)
    attio.close()
    return 0


def _promote_one(attio: AttioClient, company_id: str, entry: dict) -> None:
    """Add the company to Deal Pipeline. Only non-list-mutating call here."""
    entry_values = _pipeline_entry_values(entry)
    attio.add_record_to_list(
        list_id=DEAL_PIPELINE_LIST_ID,
        parent_record_id=company_id,
        parent_object=PARENT_OBJECT,
        entry_values=entry_values,
        allow_duplicates=False,
    )


def _pipeline_entry_values(entry: dict) -> dict:
    """Build entry_values for the Deal Pipeline.

    Per spec: stage = "New" always. Pass through upcoming_round and
    upcoming_round_size_eum if present on the inbound entry.
    """
    values: dict = {"stage": PIPELINE_STAGE_NEW}

    inbound_values = entry.get("entry_values") or {}

    # upcoming_round — only include valid in-scope values
    upcoming_round = _extract_select(inbound_values.get("upcoming_round"))
    if upcoming_round and upcoming_round in IN_SCOPE_STAGES:
        values["upcoming_round"] = [upcoming_round]

    # upcoming_round_size_eum
    size = _extract_number(inbound_values.get("upcoming_round_size_eum"))
    if size is not None:
        values["upcoming_round_size_eum"] = size

    return values


def _mark_added(
    attio: AttioClient,
    entry_id: str,
    failed: list[tuple[str, str]],
    company_name: str,
) -> None:
    try:
        attio.update_list_entry(
            list_id=INBOUND_DEALS_LIST_ID,
            entry_id=entry_id,
            entry_values={"step": STEP_ADDED},
        )
    except AttioError as e:
        print(f"{company_name}: failed to set step=Added: {e}")
        failed.append((company_name, "could not flip step to Added"))


def _is_already_exists(err_msg: str) -> bool:
    lower = err_msg.lower()
    return (
        "already exists" in lower
        or "duplicate" in lower
        or "already in list" in lower
    )


def _resolve_company_name(
    attio: AttioClient, entry: dict, company_id: str | None
) -> str:
    # Try to read a name from the entry's parent record if inlined; else fall
    # back to a short form of the company id.
    parent = entry.get("parent_record") or entry.get("parent")
    if isinstance(parent, dict):
        name = AttioClient.company_name(parent)
        if name:
            return name
    if company_id:
        return f"company:{company_id[:8]}"
    return "unknown"


def _extract_select(v) -> str | None:
    if not v:
        return None
    if isinstance(v, list) and v:
        item = v[0]
        if isinstance(item, dict):
            opt = item.get("option") or {}
            return opt.get("title") or item.get("value")
    if isinstance(v, dict):
        opt = v.get("option") or {}
        return opt.get("title") or v.get("value")
    if isinstance(v, str):
        return v
    return None


def _extract_number(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list) and v:
        return _extract_number(v[0])
    if isinstance(v, dict):
        for k in ("value", "number_value"):
            if k in v and v[k] is not None:
                try:
                    return float(v[k])
                except (TypeError, ValueError):
                    return None
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _post_summary(
    slack: SlackClient,
    promoted: list[str],
    failed: list[tuple[str, str]],
) -> None:
    if not promoted and not failed:
        print("Nothing to promote.")
        return

    lines: list[str] = []
    if promoted:
        lines.append(
            f"🗓️ Daily promotion: moved {len(promoted)} deal(s) to Deal Pipeline."
        )
        for name in promoted:
            lines.append(f"• {name}")
    if failed:
        lines.append("")
        lines.append(f"⚠️ {len(failed)} failed:")
        for name, why in failed:
            lines.append(f"• {name} — {why}")

    try:
        slack.post_message("\n".join(lines))
    except Exception as e:
        print(f"Failed to post Slack summary: {e}")


def _short(s: str, n: int = 160) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


if __name__ == "__main__":
    sys.exit(main())
