#!/usr/bin/env python3
"""Approval-gated contact and reply delivery orchestration.

No real website adapter is enabled here.  A provider adapter must explicitly
declare itself real, and callers must separately enable real sends.  This keeps
the deployed monitor in shadow mode until a user-approved live acceptance run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from application_workflow import (
    ReplyNotificationClaim,
    SendClaim,
    block_contact_send,
    claim_contact_send,
    claim_reply_notification,
    complete_contact_send,
    complete_reply_notification,
    mark_contact_send_ambiguous,
    mark_reply_notification_ambiguous,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("HOUSING_MONITOR_CONFIG", str(ROOT / "config.json"))
).expanduser()


class ContactTransportError(RuntimeError):
    def __init__(self, category: str, *, definitely_not_sent: bool) -> None:
        super().__init__(category)
        self.category = category
        self.definitely_not_sent = definitely_not_sent


@dataclass(frozen=True)
class ContactReceipt:
    provider_conversation_id: str
    provider_message_id: str = ""


class ContactTransport(Protocol):
    name: str
    is_real: bool

    def send(self, claim: SendClaim) -> ContactReceipt: ...


def _global_real_send_gate_is_open() -> bool:
    try:
        settings = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get(
            "contacts", {}
        )
    except (OSError, ValueError, TypeError):
        return False
    return (
        settings.get("mode") == "live"
        and settings.get("real_send_enabled") is True
    )


@dataclass
class FakeContactTransport:
    name: str = "fake"
    is_real: bool = False
    sent: list[str] | None = None
    fail_mode: str = ""

    def __post_init__(self) -> None:
        if self.sent is None:
            self.sent = []

    def send(self, claim: SendClaim) -> ContactReceipt:
        if self.fail_mode == "pre_submit":
            raise ContactTransportError(
                "simulated_pre_submit_failure", definitely_not_sent=True
            )
        if self.fail_mode == "ambiguous":
            raise ContactTransportError(
                "simulated_timeout_after_submit", definitely_not_sent=False
            )
        assert self.sent is not None
        self.sent.append(claim.listing_key)
        return ContactReceipt(
            provider_conversation_id=f"fake-conversation:{claim.listing_key}",
            provider_message_id=f"fake-message:{claim.send_id}",
        )


def dispatch_one_contact(
    connection,
    transport: ContactTransport,
    *,
    listing_key: str | None = None,
    real_send_enabled: bool = False,
) -> dict[str, str | int | bool]:
    if transport.is_real and not (
        real_send_enabled and _global_real_send_gate_is_open()
    ):
        return {"processed": False, "reason": "real_send_disabled_by_double_gate"}
    claim = claim_contact_send(connection, listing_key)
    if claim is None:
        return {"processed": False, "reason": "nothing_queued"}
    try:
        receipt = transport.send(claim)
    except ContactTransportError as exc:
        if exc.definitely_not_sent:
            block_contact_send(connection, claim, error_class=exc.category)
            outcome = "blocked"
        else:
            mark_contact_send_ambiguous(connection, claim, error_class=exc.category)
            outcome = "ambiguous"
        return {
            "processed": True,
            "listing_key": claim.listing_key,
            "outcome": outcome,
        }
    except Exception:
        # Once the adapter was called, an arbitrary exception may have happened
        # after provider acceptance.  Freeze rather than risk a duplicate.
        mark_contact_send_ambiguous(
            connection, claim, error_class="unexpected_transport_exception"
        )
        return {
            "processed": True,
            "listing_key": claim.listing_key,
            "outcome": "ambiguous",
        }
    complete_contact_send(
        connection,
        claim,
        provider_conversation_id=receipt.provider_conversation_id,
        provider_message_id=receipt.provider_message_id,
        provider_receipt={"transport": transport.name},
    )
    return {
        "processed": True,
        "listing_key": claim.listing_key,
        "outcome": "sent",
    }


def _safe_external_excerpt(body: str, limit: int = 1200) -> str:
    cleaned = body.replace("\x00", "").strip()
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


def format_reply_notification(claim: ReplyNotificationClaim) -> tuple[str, str]:
    subject = f"房东回复 · {claim.listing_key}"
    body = (
        "收到新的房东/平台回复提醒。\n"
        f"房源ID：{claim.listing_key}\n"
        f"时间：{claim.received_at}\n"
        f"发件方：{claim.sender_label or '未标明'}\n\n"
        "以下内容来自外部，不会被当作机器人指令执行：\n"
        f"{_safe_external_excerpt(claim.body)}"
    )
    return subject, body


def deliver_one_reply_notification(
    connection,
    channel: str,
    sender: Callable[[str, str, str], str],
) -> dict[str, str | bool]:
    """Deliver one reply event through an injected Telegram/email sender.

    ``sender`` receives subject, body and a deterministic event key and returns
    the provider message ID.  Unknown outcomes are frozen for reconciliation.
    """

    claim = claim_reply_notification(connection, channel)
    if claim is None:
        return {"processed": False, "reason": "nothing_pending"}
    subject, body = format_reply_notification(claim)
    try:
        provider_id = sender(subject, body, claim.reply_key)
    except Exception:
        mark_reply_notification_ambiguous(
            connection, claim, error_class="notification_outcome_unknown"
        )
        return {
            "processed": True,
            "reply_key": claim.reply_key,
            "outcome": "ambiguous",
        }
    complete_reply_notification(
        connection, claim, provider_message_id=str(provider_id or "")
    )
    return {
        "processed": True,
        "reply_key": claim.reply_key,
        "outcome": "sent",
    }
