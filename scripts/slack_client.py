"""Slack helpers used by ingest and promote."""

from __future__ import annotations

import time
from typing import Any

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

    # -- filtering ---------------------------------------------------------

    @staticmethod
    def is_from_bot(msg: dict) -> bool:
        return msg.get("subtype") == "bot_message" or bool(msg.get("bot_id"))

    @staticmethod
    def is_thread_reply(msg: dict) -> bool:
        thread_ts = msg.get("thread_ts")
        return bool(thread_ts and thread_ts != msg.get("ts"))

    @staticmethod
    def has_processed_reaction(msg: dict) -> bool:
        for r in msg.get("reactions") or []:
            if r.get("name") in PROCESSED_REACTIONS:
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
