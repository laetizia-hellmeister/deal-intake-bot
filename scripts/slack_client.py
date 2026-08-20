"""Slack helpers used by ingest and promote."""

from __future__ import annotations

import time
from typing import Any

import httpx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import PROCESSED_REACTIONS, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID


class SlackClient:
    def __init__(self, token: str | None = None, channel_id: str = SLACK_CHANNEL_ID):
        self._client = WebClient(token=token or SLACK_BOT_TOKEN)
        self.channel_id = channel_id
        self._bot_user_id: str | None = None

    # -- identity ----------------------------------------------------------

    @property
    def bot_user_id(self) -> str:
        if self._bot_user_id is None:
            resp = self._client.auth_test()
            self._bot_user_id = resp["user_id"]
        return self._bot_user_id

    # -- reading -----------------------------------------------------------

    def fetch_thread_replies(self, thread_ts: str) -> list[dict]:
        """Return every message in a thread (root first, then replies in ts
        order) via conversations.replies. `conversations_history` does NOT
        reliably surface plain (non-broadcast) thread replies, so this is a
        separate call rather than reusing fetch_recent_messages."""
        resp = self._client.conversations_replies(
            channel=self.channel_id, ts=thread_ts
        )
        return resp.get("messages", []) or []

    def fetch_recent_messages(self, lookback_seconds: int, limit: int) -> list[dict]:
        """Return messages from the channel, newest first, within lookback window."""
        # Slack's `oldest` expects a Unix timestamp formatted as
        # <seconds>.<microseconds> (max 6 decimal places). Python's default
        # str(time.time()) can emit 7 decimals, which Slack mis-parses (the
        # decimal point shifts and we end up filtering on a date in 2532).
        # Use an integer second — sub-second precision is irrelevant for
        # an hour-scale lookback.
        oldest = int(time.time() - lookback_seconds)
        resp = self._client.conversations_history(
            channel=self.channel_id,
            oldest=str(oldest),
            limit=limit,
        )
        return resp.get("messages", []) or []

    def fetch_messages_since(
        self, oldest_ts: int, max_messages: int
    ) -> list[dict]:
        """Return up to `max_messages` channel messages newer than
        `oldest_ts` (a Unix second), following cursors across pages.

        Separate from fetch_recent_messages because that one is a single
        capped page — fine for ingest's 4-hour window, not for the
        multi-week sweep the deck-reply pass needs.
        """
        out: list[dict] = []
        cursor: str | None = None
        while len(out) < max_messages:
            kwargs: dict[str, Any] = {
                "channel": self.channel_id,
                "oldest": str(int(oldest_ts)),
                "limit": min(200, max_messages - len(out)),
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = self._client.conversations_history(**kwargs)
            page = resp.get("messages", []) or []
            out.extend(page)
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor or not page:
                break
        return out

    # -- filtering ---------------------------------------------------------

    @staticmethod
    def is_from_bot(msg: dict) -> bool:
        return msg.get("subtype") == "bot_message" or bool(msg.get("bot_id"))

    @staticmethod
    def is_thread_reply(msg: dict) -> bool:
        thread_ts = msg.get("thread_ts")
        return bool(thread_ts and thread_ts != msg.get("ts"))

    @staticmethod
    def has_processed_reaction(
        msg: dict, reactions: set[str] = PROCESSED_REACTIONS
    ) -> bool:
        for r in msg.get("reactions") or []:
            if r.get("name") in reactions:
                return True
        return False

    # -- writing -----------------------------------------------------------

    def post_thread_reply(self, thread_ts: str, text: str) -> None:
        self._client.chat_postMessage(
            channel=self.channel_id,
            thread_ts=thread_ts,
            text=text,
        )

    def post_message(self, text: str) -> None:
        self._client.chat_postMessage(channel=self.channel_id, text=text)

    def add_reaction(self, ts: str, name: str) -> None:
        try:
            self._client.reactions_add(
                channel=self.channel_id, timestamp=ts, name=name
            )
        except SlackApiError as e:
            # already_reacted is benign (we re-ran on a partially-processed message)
            if e.response.get("error") == "already_reacted":
                return
            raise

    # -- file download -----------------------------------------------------

    def download_file(self, url: str) -> bytes | None:
        """Download a file from a Slack `url_private_download` (or
        `url_private`) URL using the bot token for authentication.
        Returns raw bytes, or None on error. Caller is responsible for
        knowing what mime-type to expect.

        When the bot token lacks the `files:read` scope Slack does NOT
        return 403 — it serves an HTML sign-in page with HTTP 200. A
        status-only check passes that HTML through as if it were the
        file, so we check the content type as well.
        """
        if not url:
            return None
        token = self._client.token or ""
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60.0,
                follow_redirects=True,
            )
        except Exception as e:
            print(f"[slack] file download error for {url}: {e}")
            return None
        if resp.status_code >= 400:
            print(
                f"[slack] file download HTTP {resp.status_code} for {url}"
            )
            return None
        content_type = (resp.headers.get("content-type") or "").lower()
        if content_type.startswith("text/html"):
            print(
                f"[slack] file download for {url} returned an HTML page, "
                "not the file — the bot token is almost certainly missing "
                "the `files:read` scope (add it under Bot Token Scopes and "
                "reinstall the app)"
            )
            return None
        return resp.content

    # -- permalinks --------------------------------------------------------

    def permalink(self, ts: str) -> str | None:
        try:
            resp = self._client.chat_getPermalink(
                channel=self.channel_id, message_ts=ts
            )
            return resp.get("permalink")
        except SlackApiError:
            return None

    def user_display_name(self, user_id: str) -> str | None:
        if not user_id:
            return None
        try:
            resp = self._client.users_info(user=user_id)
            user = resp.get("user") or {}
            profile = user.get("profile") or {}
            return (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
            )
        except SlackApiError:
            return None
