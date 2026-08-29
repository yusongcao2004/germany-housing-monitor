#!/usr/bin/env python3
"""Idempotent, approval-gated landlord contact ledger.

This module deliberately does not open websites or send messages.  It prepares
and records drafts, enforces per-listing approval, and deduplicates replies so
that a later browser adapter cannot contact a landlord twice by accident.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONTACT_PROFILE_PATH = Path(
    os.environ.get(
        "HOUSING_MONITOR_CONTACT_PROFILE",
        str(ROOT / "state" / "contact_profile.json"),
    )
).expanduser()
CONTACT_STATUSES = (
    "approval_pending",
    "approved",
    "sent",
    "replied",
    "closed",
    "failed",
)


class ContactWorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class DraftCreationResult:
    created: bool
    listing_key: str
    possible_cross_platform_matches: tuple[str, ...]
    revision: int = 1
    draft_hash: str = ""


@dataclass(frozen=True)
class SendClaim:
    send_id: int
    listing_key: str
    revision: int
    draft_hash: str
    channel: str
    subject: str
    body: str
    lease_token: str


@dataclass(frozen=True)
class ReplyNotificationClaim:
    delivery_id: int
    reply_key: str
    listing_key: str
    channel: str
    sender_label: str
    body: str
    received_at: str
    lease_token: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_contact_profile_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate profile structure without persisting or normalizing personal data."""

    if not isinstance(payload, dict):
        raise ContactWorkflowError("Contact profile root must be a JSON object")
    if payload.get("approval_mode") != "per_listing_id":
        raise ContactWorkflowError("Contact profile must require per-listing approval")
    if not payload.get("profile_version"):
        raise ContactWorkflowError("Contact profile version is missing")
    applicants = payload.get("applicants")
    if not isinstance(applicants, list) or not 1 <= len(applicants) <= 4:
        raise ContactWorkflowError("Contact profile needs one to four applicants")
    names = [str(item.get("name") or "").strip() for item in applicants]
    if any(
        not name or len(name) > 100 or re.search(r"[\r\n<>]", name)
        for name in names
    ):
        raise ContactWorkflowError("Contact profile contains an invalid applicant name")
    statements = payload.get("verified_statements_de")
    required_statements = (
        "introduction",
        "tenancy",
        "move_in",
        "household",
        "financial",
    )
    if not isinstance(statements, dict):
        raise ContactWorkflowError("Contact profile needs verified German statements")
    for key in required_statements:
        value = str(statements.get(key) or "").strip()
        if not value or len(value) > 600 or "\x00" in value:
            raise ContactWorkflowError(f"Contact profile statement is invalid: {key}")
    return payload


def load_contact_profile(path: Path | None = None) -> dict[str, Any]:
    selected = path or CONTACT_PROFILE_PATH
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContactWorkflowError(
            "Missing contact profile; copy examples/contact_profile.example.json "
            "to the ignored state/contact_profile.json path"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ContactWorkflowError("Contact profile could not be read as JSON") from exc
    return load_contact_profile_from_payload(payload)


def canonical_listing_key(platform: str, listing_id: str) -> str:
    normalized_platform = re.sub(r"[^a-z0-9]+", "", platform.lower())
    normalized_id = listing_id.strip()
    if not normalized_platform or not re.fullmatch(r"[A-Za-z0-9_-]+", normalized_id):
        raise ContactWorkflowError("Invalid platform or listing ID")
    return f"{normalized_platform}:{normalized_id}"


def _normalize_text(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore"
    ).decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def property_fingerprint(
    *,
    address: str,
    warm_rent_eur: float | None,
    area_m2: float | None,
    rooms: float | None,
) -> str:
    """Create a conservative cross-platform duplicate hint, not a final identity."""

    normalized_address = _normalize_text(address)
    if not normalized_address:
        return ""
    payload = {
        "address": normalized_address,
        "warm_rent_eur": round(warm_rent_eur or 0, 0),
        "area_m2": round(area_m2 or 0, 0),
        "rooms": round(rooms or 0, 1),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def draft_content_hash(*, subject: str, body: str, profile_version: str) -> str:
    payload = {
        "subject": subject.replace("\r\n", "\n"),
        "body": body.replace("\r\n", "\n"),
        "profile_version": profile_version,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def initialize_contact_database(connection: sqlite3.Connection) -> None:
    statuses = ",".join(f"'{item}'" for item in CONTACT_STATUSES)
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS contact_applications (
            listing_key TEXT PRIMARY KEY,
            property_fingerprint TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            profile_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ({statuses})),
            draft_subject TEXT NOT NULL DEFAULT '',
            draft_body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            sent_at TEXT,
            provider_conversation_id TEXT,
            last_error TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS landlord_replies (
            reply_key TEXT PRIMARY KEY,
            listing_key TEXT NOT NULL,
            provider_message_id TEXT NOT NULL,
            sender_label TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL,
            received_at TEXT NOT NULL,
            telegram_notified INTEGER NOT NULL DEFAULT 0,
            email_notified INTEGER NOT NULL DEFAULT 0,
            UNIQUE(listing_key, provider_message_id),
            FOREIGN KEY(listing_key) REFERENCES contact_applications(listing_key)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_contact_status "
        "ON contact_applications(status, updated_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_contact_fingerprint "
        "ON contact_applications(property_fingerprint)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_conversation "
        "ON contact_applications(channel, provider_conversation_id) "
        "WHERE provider_conversation_id IS NOT NULL"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_draft_revisions (
            draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            profile_version TEXT NOT NULL,
            channel TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL,
            draft_hash TEXT NOT NULL,
            generator TEXT NOT NULL DEFAULT 'deterministic',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            superseded_at TEXT,
            UNIQUE(listing_key, revision),
            UNIQUE(listing_key, draft_hash),
            FOREIGN KEY(listing_key) REFERENCES contact_applications(listing_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_approvals (
            approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_key TEXT NOT NULL,
            draft_id INTEGER NOT NULL,
            draft_hash TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            approval_source TEXT NOT NULL,
            approval_message_id TEXT,
            approved_at TEXT NOT NULL,
            expires_at TEXT,
            consumed_at TEXT,
            revoked_at TEXT,
            UNIQUE(approval_source, approval_message_id),
            FOREIGN KEY(listing_key) REFERENCES contact_applications(listing_key),
            FOREIGN KEY(draft_id) REFERENCES contact_draft_revisions(draft_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_send_outbox (
            send_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_key TEXT NOT NULL UNIQUE,
            draft_id INTEGER NOT NULL,
            draft_hash TEXT NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            lease_token TEXT,
            lease_expires_at TEXT,
            provider_message_id TEXT,
            provider_conversation_id TEXT,
            queued_at TEXT NOT NULL,
            sent_at TEXT,
            last_error_class TEXT,
            CHECK(status IN ('queued','claimed','sent','ambiguous','blocked','cancelled')),
            FOREIGN KEY(listing_key) REFERENCES contact_applications(listing_key),
            FOREIGN KEY(draft_id) REFERENCES contact_draft_revisions(draft_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_send_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            send_id INTEGER NOT NULL,
            attempt_number INTEGER NOT NULL,
            lease_token TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            outcome TEXT,
            provider_receipt_json TEXT,
            error_class TEXT,
            UNIQUE(send_id, attempt_number),
            FOREIGN KEY(send_id) REFERENCES contact_send_outbox(send_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_bindings (
            binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            provider_conversation_id TEXT NOT NULL,
            listing_key TEXT NOT NULL UNIQUE,
            bound_at TEXT NOT NULL,
            UNIQUE(platform, account_scope, provider_conversation_id),
            FOREIGN KEY(listing_key) REFERENCES contact_applications(listing_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS reply_notification_deliveries (
            delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reply_key TEXT NOT NULL,
            channel TEXT NOT NULL,
            recipient_scope TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            lease_token TEXT,
            lease_expires_at TEXT,
            sent_at TEXT,
            provider_message_id TEXT,
            last_error_class TEXT,
            UNIQUE(reply_key, channel, recipient_scope),
            CHECK(channel IN ('telegram','email')),
            CHECK(status IN ('pending','claimed','sent','ambiguous','blocked')),
            FOREIGN KEY(reply_key) REFERENCES landlord_replies(reply_key)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_send_outbox_due "
        "ON contact_send_outbox(status, next_attempt_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_reply_delivery_due "
        "ON reply_notification_deliveries(channel, status, next_attempt_at)"
    )
    connection.commit()


def _safe_inline(value: str, label: str, maximum: int = 180) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum or "\n" in value or "\r" in value:
        raise ContactWorkflowError(f"Invalid {label}")
    return cleaned


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def render_initial_contact(
    profile: dict[str, Any],
    *,
    listing_title: str,
    listing_text: str,
    location: str,
    landlord_salutation: str = "Guten Tag",
    evidence_highlight: str = "",
) -> tuple[str, str]:
    """Render a conservative German draft from verified profile/listing facts."""

    title = _safe_inline(listing_title, "listing title")
    place = _safe_inline(location, "location")
    salutation = _safe_inline(landlord_salutation, "landlord salutation", 100)
    highlight_line = ""
    if evidence_highlight:
        highlight = _safe_inline(evidence_highlight, "listing highlight", 160)
        if _normalize_text(highlight) not in _normalize_text(listing_text):
            raise ContactWorkflowError("Personalized highlight is not grounded in listing text")
        highlight_line = (
            f" Besonders angesprochen hat uns die Angabe „{highlight}“ in der Anzeige."
        )

    validated_profile = load_contact_profile_from_payload(profile)
    names = [str(item["name"]).strip() for item in validated_profile["applicants"]]
    statements = dict(validated_profile["verified_statements_de"])
    signature = " und ".join(names)

    subject = f"Anfrage zur Wohnung: {title}"
    body = (
        f"{salutation},\n\n"
        f"wir interessieren uns sehr für die Wohnung „{title}“ in {place}."
        f"{highlight_line}\n\n"
        f"{statements['introduction']} {statements['tenancy']} "
        f"{statements['move_in']} {statements['household']}\n\n"
        f"{statements['financial']}\n\n"
        "Über die Möglichkeit einer Besichtigung würden wir uns sehr freuen.\n\n"
        "Mit freundlichen Grüßen\n"
        f"{signature}"
    )
    validate_contact_draft(profile, body)
    return subject, body


def validate_contact_draft(profile: dict[str, Any], body: str) -> None:
    validated_profile = load_contact_profile_from_payload(profile)
    required = tuple(
        [str(item["name"]).strip() for item in validated_profile["applicants"]]
        + [
            str(value).strip()
            for value in dict(validated_profile["verified_statements_de"]).values()
        ]
    )
    missing = [item for item in required if item not in body]
    if missing:
        raise ContactWorkflowError(f"Draft is missing required facts: {missing}")
    if not dict(validated_profile.get("financial_support") or {}).get(
        "formal_guarantee_confirmed", False
    ):
        forbidden_patterns = (
            r"Bürgschaft (?:liegt vor|ist vorhanden)",
            r"Elternbürgschaft (?:liegt vor|ist vorhanden)",
            r"gesichertes Einkommen",
            r"SCHUFA.{0,20}(?:liegt vor|vorhanden)",
        )
        for pattern in forbidden_patterns:
            if re.search(pattern, body, flags=re.IGNORECASE):
                raise ContactWorkflowError("Draft contains an unverified financial claim")


def create_contact_draft(
    connection: sqlite3.Connection,
    *,
    listing_key: str,
    channel: str,
    profile_version: str,
    subject: str,
    body: str,
    fingerprint: str = "",
    generator: str = "deterministic",
    evidence: dict[str, Any] | None = None,
) -> DraftCreationResult:
    initialize_contact_database(connection)
    listing_exists = connection.execute(
        "SELECT 1 FROM seen WHERE listing_key = ?", (listing_key,)
    ).fetchone()
    if listing_exists is None:
        raise ContactWorkflowError("Cannot draft a contact for an unknown listing")
    profile = load_contact_profile()
    if profile_version != str(profile["profile_version"]):
        raise ContactWorkflowError("Draft profile version is not current")
    validate_contact_draft(profile, body)
    safe_channel = _safe_inline(channel, "contact channel", 60)
    safe_subject = _safe_inline(subject, "draft subject", 240)
    safe_generator = _safe_inline(generator, "draft generator", 80)
    content_hash = draft_content_hash(
        subject=safe_subject, body=body, profile_version=profile_version
    )
    now = utc_now()
    matches = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT listing_key FROM contact_applications "
            "WHERE property_fingerprint = ? AND property_fingerprint <> '' "
            "AND listing_key <> ? ORDER BY listing_key",
            (fingerprint, listing_key),
        ).fetchall()
    )
    evidence_json = json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True)
    with connection:
        existing = connection.execute(
            "SELECT status FROM contact_applications WHERE listing_key = ?",
            (listing_key,),
        ).fetchone()
        duplicate = connection.execute(
            "SELECT revision FROM contact_draft_revisions "
            "WHERE listing_key = ? AND draft_hash = ?",
            (listing_key, content_hash),
        ).fetchone()
        if duplicate is not None:
            return DraftCreationResult(
                False, listing_key, matches, int(duplicate[0]), content_hash
            )
        if existing is None:
            connection.execute(
                """
                INSERT INTO contact_applications(
                    listing_key, property_fingerprint, channel, profile_version,
                    status, draft_subject, draft_body, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'approval_pending', ?, ?, ?, ?)
                """,
                (
                    listing_key,
                    fingerprint,
                    safe_channel,
                    profile_version,
                    safe_subject,
                    body,
                    now,
                    now,
                ),
            )
            revision = 1
        else:
            status = str(existing[0])
            if status in {"sent", "replied", "closed"}:
                raise ContactWorkflowError(
                    "A contacted listing cannot receive another initial draft"
                )
            outbox = connection.execute(
                "SELECT status FROM contact_send_outbox WHERE listing_key = ?",
                (listing_key,),
            ).fetchone()
            if outbox is not None and str(outbox[0]) in {
                "claimed",
                "sent",
                "ambiguous",
            }:
                raise ContactWorkflowError(
                    "Cannot revise a draft with an in-flight or uncertain send"
                )
            connection.execute(
                "UPDATE contact_draft_revisions SET superseded_at = ? "
                "WHERE listing_key = ? AND superseded_at IS NULL",
                (now, listing_key),
            )
            connection.execute(
                "UPDATE contact_approvals SET revoked_at = ? "
                "WHERE listing_key = ? AND revoked_at IS NULL AND consumed_at IS NULL",
                (now, listing_key),
            )
            connection.execute(
                "UPDATE contact_send_outbox SET status = 'cancelled', "
                "last_error_class = 'draft_superseded' "
                "WHERE listing_key = ? AND status IN ('queued','blocked')",
                (listing_key,),
            )
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 "
                    "FROM contact_draft_revisions WHERE listing_key = ?",
                    (listing_key,),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE contact_applications SET property_fingerprint = ?, "
                "channel = ?, profile_version = ?, status = 'approval_pending', "
                "draft_subject = ?, draft_body = ?, updated_at = ?, "
                "approved_at = NULL, last_error = NULL WHERE listing_key = ?",
                (
                    fingerprint,
                    safe_channel,
                    profile_version,
                    safe_subject,
                    body,
                    now,
                    listing_key,
                ),
            )
        connection.execute(
            """
            INSERT INTO contact_draft_revisions(
                listing_key, revision, profile_version, channel, subject, body,
                draft_hash, generator, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_key,
                revision,
                profile_version,
                safe_channel,
                safe_subject,
                body,
                content_hash,
                safe_generator,
                evidence_json,
                now,
            ),
        )
    return DraftCreationResult(True, listing_key, matches, revision, content_hash)


def approve_contact(
    connection: sqlite3.Connection,
    listing_key: str,
    *,
    expected_draft_hash: str,
    approval_message_id: str,
    approved_by: str = "local_operator",
    approval_source: str = "local_cli",
    expires_at: str | None = None,
) -> str:
    initialize_contact_database(connection)
    now = utc_now()
    if not re.fullmatch(r"[0-9a-f]{8,64}", expected_draft_hash):
        raise ContactWorkflowError("Approval must identify the current draft hash")
    safe_approver = _safe_inline(approved_by, "approver", 120)
    safe_source = _safe_inline(approval_source, "approval source", 80)
    safe_message_id = _safe_inline(
        approval_message_id, "approval message ID", 180
    )
    with connection:
        application = connection.execute(
            "SELECT status, channel FROM contact_applications WHERE listing_key = ?",
            (listing_key,),
        ).fetchone()
        if application is None or str(application[0]) != "approval_pending":
            raise ContactWorkflowError(
                "Contact is missing or is not awaiting approval"
            )
        draft = connection.execute(
            "SELECT draft_id, revision, draft_hash, created_at "
            "FROM contact_draft_revisions "
            "WHERE listing_key = ? AND superseded_at IS NULL "
            "ORDER BY revision DESC LIMIT 1",
            (listing_key,),
        ).fetchone()
        if draft is None:
            raise ContactWorkflowError("Current draft revision is missing")
        draft_id, _revision, current_hash, draft_created_at = (
            int(draft[0]),
            int(draft[1]),
            str(draft[2]),
            str(draft[3]),
        )
        if not current_hash.startswith(expected_draft_hash):
            raise ContactWorkflowError("Approval does not match the current draft hash")
        if _table_exists(connection, "dedupe_candidates"):
            duplicate = connection.execute(
                """
                SELECT other.listing_key
                FROM source_listings AS current
                JOIN dedupe_candidates AS candidate
                  ON current.source_listing_id IN (
                     candidate.left_source_listing_id,
                     candidate.right_source_listing_id
                  )
                JOIN source_listings AS other
                  ON other.source_listing_id = CASE
                     WHEN candidate.left_source_listing_id = current.source_listing_id
                     THEN candidate.right_source_listing_id
                     ELSE candidate.left_source_listing_id
                  END
                JOIN contact_applications AS other_application
                  ON other_application.listing_key = other.listing_key
                WHERE current.listing_key = ?
                  AND candidate.status = 'pending'
                  AND other_application.status IN ('approved','sent','replied')
                LIMIT 1
                """,
                (listing_key,),
            ).fetchone()
            if duplicate is not None:
                raise ContactWorkflowError(
                    f"Possible duplicate already has an active contact: {duplicate[0]}"
                )
        if _table_exists(connection, "eligibility_evaluations"):
            eligibility = connection.execute(
                "SELECT status, expires_at, evaluated_at FROM eligibility_evaluations "
                "WHERE listing_key = ? ORDER BY evaluation_id DESC LIMIT 1",
                (listing_key,),
            ).fetchone()
            if eligibility is None or str(eligibility[0]) != "eligible":
                raise ContactWorkflowError(
                    "Listing still needs eligibility review before approval"
                )
            if str(eligibility[2]) < draft_created_at:
                raise ContactWorkflowError(
                    "Eligibility review predates the current draft revision"
                )
            if eligibility[1]:
                expiry = dt.datetime.fromisoformat(str(eligibility[1]))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=dt.timezone.utc)
                if expiry <= dt.datetime.now(dt.timezone.utc):
                    raise ContactWorkflowError("Eligibility review has expired")
        connection.execute(
            "INSERT INTO contact_approvals("
            "listing_key, draft_id, draft_hash, approved_by, approval_source, "
            "approval_message_id, approved_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                listing_key,
                draft_id,
                current_hash,
                safe_approver,
                safe_source,
                safe_message_id,
                now,
                expires_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO contact_send_outbox(
                listing_key, draft_id, draft_hash, channel, status,
                next_attempt_at, queued_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
            ON CONFLICT(listing_key) DO UPDATE SET
                draft_id = excluded.draft_id,
                draft_hash = excluded.draft_hash,
                channel = excluded.channel,
                status = 'queued',
                next_attempt_at = excluded.next_attempt_at,
                queued_at = excluded.queued_at,
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error_class = NULL
            WHERE contact_send_outbox.status IN ('cancelled','blocked')
            """,
            (listing_key, draft_id, current_hash, str(application[1]), now, now),
        )
        outbox = connection.execute(
            "SELECT status, draft_hash FROM contact_send_outbox WHERE listing_key = ?",
            (listing_key,),
        ).fetchone()
        if outbox is None or str(outbox[0]) != "queued" or str(outbox[1]) != current_hash:
            raise ContactWorkflowError("A conflicting send record already exists")
        connection.execute(
            "UPDATE contact_applications SET status = 'approved', approved_at = ?, "
            "updated_at = ? WHERE listing_key = ?",
            (now, now, listing_key),
        )
    return current_hash


def claim_contact_send(
    connection: sqlite3.Connection,
    listing_key: str | None = None,
    *,
    lease_seconds: int = 300,
) -> SendClaim | None:
    """Atomically lease one approved send.

    A provider transport must claim before touching external UI.  Once an
    external submit may have happened, failures go to ``ambiguous`` and are not
    eligible for automatic retry.
    """

    initialize_contact_database(connection)
    now = utc_now()
    expiry = (
        dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(seconds=max(30, int(lease_seconds)))
    ).isoformat()
    token = secrets.token_hex(20)
    with connection:
        connection.execute(
            "UPDATE contact_send_outbox SET status = 'blocked', "
            "last_error_class = 'approval_missing_or_expired' "
            "WHERE status = 'queued' AND NOT EXISTS ("
            "SELECT 1 FROM contact_approvals AS approval "
            "WHERE approval.listing_key = contact_send_outbox.listing_key "
            "AND approval.draft_id = contact_send_outbox.draft_id "
            "AND approval.draft_hash = contact_send_outbox.draft_hash "
            "AND approval.revoked_at IS NULL AND approval.consumed_at IS NULL "
            "AND (approval.expires_at IS NULL OR approval.expires_at > ?))",
            (now,),
        )
        if listing_key:
            row = connection.execute(
                """
                SELECT outbox.send_id, outbox.listing_key, draft.revision,
                       outbox.draft_hash, outbox.channel, draft.subject, draft.body,
                       outbox.attempts
                FROM contact_send_outbox AS outbox
                JOIN contact_draft_revisions AS draft
                  ON draft.draft_id = outbox.draft_id
                JOIN contact_applications AS application
                  ON application.listing_key = outbox.listing_key
                WHERE outbox.listing_key = ? AND outbox.status = 'queued'
                  AND application.status = 'approved'
                  AND outbox.next_attempt_at <= ?
                  AND EXISTS (
                      SELECT 1 FROM contact_approvals AS approval
                      WHERE approval.listing_key = outbox.listing_key
                        AND approval.draft_id = outbox.draft_id
                        AND approval.draft_hash = outbox.draft_hash
                        AND approval.revoked_at IS NULL
                        AND approval.consumed_at IS NULL
                        AND (approval.expires_at IS NULL OR approval.expires_at > ?)
                  )
                """,
                (listing_key, now, now),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT outbox.send_id, outbox.listing_key, draft.revision,
                       outbox.draft_hash, outbox.channel, draft.subject, draft.body,
                       outbox.attempts
                FROM contact_send_outbox AS outbox
                JOIN contact_draft_revisions AS draft
                  ON draft.draft_id = outbox.draft_id
                JOIN contact_applications AS application
                  ON application.listing_key = outbox.listing_key
                WHERE outbox.status = 'queued' AND application.status = 'approved'
                  AND outbox.next_attempt_at <= ?
                  AND EXISTS (
                      SELECT 1 FROM contact_approvals AS approval
                      WHERE approval.listing_key = outbox.listing_key
                        AND approval.draft_id = outbox.draft_id
                        AND approval.draft_hash = outbox.draft_hash
                        AND approval.revoked_at IS NULL
                        AND approval.consumed_at IS NULL
                        AND (approval.expires_at IS NULL OR approval.expires_at > ?)
                  )
                ORDER BY outbox.queued_at, outbox.send_id LIMIT 1
                """,
                (now, now),
            ).fetchone()
        if row is None:
            return None
        send_id = int(row[0])
        next_attempt = int(row[7]) + 1
        cursor = connection.execute(
            "UPDATE contact_send_outbox SET status = 'claimed', attempts = ?, "
            "lease_token = ?, lease_expires_at = ? "
            "WHERE send_id = ? AND status = 'queued'",
            (next_attempt, token, expiry, send_id),
        )
        if cursor.rowcount != 1:
            return None
        connection.execute(
            "INSERT INTO contact_send_attempts("
            "send_id, attempt_number, lease_token, started_at) VALUES (?, ?, ?, ?)",
            (send_id, next_attempt, token, now),
        )
    return SendClaim(
        send_id=send_id,
        listing_key=str(row[1]),
        revision=int(row[2]),
        draft_hash=str(row[3]),
        channel=str(row[4]),
        subject=str(row[5]),
        body=str(row[6]),
        lease_token=token,
    )


def complete_contact_send(
    connection: sqlite3.Connection,
    claim: SendClaim,
    *,
    provider_conversation_id: str,
    provider_message_id: str = "",
    provider_receipt: dict[str, Any] | None = None,
    account_scope: str = "primary",
) -> None:
    conversation_id = _safe_inline(
        provider_conversation_id, "provider conversation ID", 180
    )
    message_id = (
        _safe_inline(provider_message_id, "provider message ID", 180)
        if provider_message_id
        else ""
    )
    safe_account = _safe_inline(account_scope, "account scope", 80)
    platform = claim.listing_key.split(":", 1)[0]
    now = utc_now()
    with connection:
        cursor = connection.execute(
            "UPDATE contact_send_outbox SET status = 'sent', sent_at = ?, "
            "provider_message_id = ?, provider_conversation_id = ?, "
            "lease_token = NULL, lease_expires_at = NULL, last_error_class = NULL "
            "WHERE send_id = ? AND listing_key = ? AND draft_hash = ? "
            "AND status = 'claimed' AND lease_token = ?",
            (
                now,
                message_id or None,
                conversation_id,
                claim.send_id,
                claim.listing_key,
                claim.draft_hash,
                claim.lease_token,
            ),
        )
        if cursor.rowcount != 1:
            raise ContactWorkflowError("Send claim is stale or no longer valid")
        connection.execute(
            "UPDATE contact_send_attempts SET finished_at = ?, outcome = 'sent', "
            "provider_receipt_json = ? WHERE lease_token = ?",
            (
                now,
                json.dumps(provider_receipt or {}, ensure_ascii=False, sort_keys=True),
                claim.lease_token,
            ),
        )
        connection.execute(
            "UPDATE contact_approvals SET consumed_at = ? "
            "WHERE listing_key = ? AND draft_hash = ? AND revoked_at IS NULL "
            "AND consumed_at IS NULL",
            (now, claim.listing_key, claim.draft_hash),
        )
        connection.execute(
            "INSERT INTO conversation_bindings("
            "platform, account_scope, provider_conversation_id, listing_key, bound_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (platform, safe_account, conversation_id, claim.listing_key, now),
        )
        connection.execute(
            "UPDATE contact_applications SET status = 'sent', sent_at = ?, "
            "updated_at = ?, provider_conversation_id = ?, last_error = NULL "
            "WHERE listing_key = ? AND status = 'approved'",
            (now, now, conversation_id, claim.listing_key),
        )


def mark_contact_send_ambiguous(
    connection: sqlite3.Connection,
    claim: SendClaim,
    *,
    error_class: str,
) -> None:
    """Freeze a send whose provider outcome cannot be proven.

    Ambiguous sends are intentionally not requeued.  A human or read-only inbox
    reconciliation must decide whether the provider accepted the message.
    """

    safe_error = _safe_inline(error_class, "send error", 120)
    now = utc_now()
    with connection:
        cursor = connection.execute(
            "UPDATE contact_send_outbox SET status = 'ambiguous', "
            "lease_token = NULL, lease_expires_at = NULL, last_error_class = ? "
            "WHERE send_id = ? AND status = 'claimed' AND lease_token = ?",
            (safe_error, claim.send_id, claim.lease_token),
        )
        if cursor.rowcount != 1:
            raise ContactWorkflowError("Send claim is stale or no longer valid")
        connection.execute(
            "UPDATE contact_send_attempts SET finished_at = ?, outcome = 'ambiguous', "
            "error_class = ? WHERE lease_token = ?",
            (now, safe_error, claim.lease_token),
        )
        connection.execute(
            "UPDATE contact_applications SET last_error = ?, updated_at = ? "
            "WHERE listing_key = ?",
            (safe_error, now, claim.listing_key),
        )


def block_contact_send(
    connection: sqlite3.Connection,
    claim: SendClaim,
    *,
    error_class: str,
) -> None:
    """Record a proven pre-submit failure without automatically retrying it."""

    safe_error = _safe_inline(error_class, "send error", 120)
    now = utc_now()
    with connection:
        cursor = connection.execute(
            "UPDATE contact_send_outbox SET status = 'blocked', "
            "lease_token = NULL, lease_expires_at = NULL, last_error_class = ? "
            "WHERE send_id = ? AND status = 'claimed' AND lease_token = ?",
            (safe_error, claim.send_id, claim.lease_token),
        )
        if cursor.rowcount != 1:
            raise ContactWorkflowError("Send claim is stale or no longer valid")
        connection.execute(
            "UPDATE contact_send_attempts SET finished_at = ?, outcome = 'blocked', "
            "error_class = ? WHERE lease_token = ?",
            (now, safe_error, claim.lease_token),
        )


def mark_contact_sent(
    connection: sqlite3.Connection,
    listing_key: str,
    provider_conversation_id: str,
) -> None:
    """Compatibility helper for tests and verified provider reconciliation."""

    claim = claim_contact_send(connection, listing_key)
    if claim is None:
        raise ContactWorkflowError("Only an approved contact can be marked sent")
    complete_contact_send(
        connection,
        claim,
        provider_conversation_id=provider_conversation_id,
        provider_receipt={"recorded_by": "compatibility_helper"},
    )


def record_landlord_reply(
    connection: sqlite3.Connection,
    *,
    listing_key: str,
    provider_message_id: str,
    sender_label: str,
    body: str,
    received_at: str | None = None,
) -> bool:
    initialize_contact_database(connection)
    application = connection.execute(
        "SELECT status, provider_conversation_id FROM contact_applications "
        "WHERE listing_key = ?",
        (listing_key,),
    ).fetchone()
    if application is None or str(application[0]) not in {"sent", "replied"}:
        raise ContactWorkflowError("Reply does not belong to a sent contact")
    if not str(application[1] or ""):
        raise ContactWorkflowError("Reply has no verified conversation binding")
    message_id = _safe_inline(provider_message_id, "provider message ID", 180)
    reply_key = hashlib.sha256(
        f"{listing_key}\n{message_id}".encode("utf-8")
    ).hexdigest()
    now = received_at or utc_now()
    with connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO landlord_replies(
                reply_key, listing_key, provider_message_id, sender_label,
                body, received_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (reply_key, listing_key, message_id, sender_label.strip(), body, now),
        )
        if cursor.rowcount == 1:
            connection.execute(
                "UPDATE contact_applications SET status = 'replied', updated_at = ? "
                "WHERE listing_key = ?",
                (utc_now(), listing_key),
            )
            for channel, recipient_scope in (
                ("telegram", "user"),
                ("email", "roommate"),
            ):
                connection.execute(
                    "INSERT INTO reply_notification_deliveries("
                    "reply_key, channel, recipient_scope, next_attempt_at) "
                    "VALUES (?, ?, ?, ?)",
                    (reply_key, channel, recipient_scope, utc_now()),
                )
    return cursor.rowcount == 1


def record_provider_reply(
    connection: sqlite3.Connection,
    *,
    platform: str,
    account_scope: str,
    provider_conversation_id: str,
    provider_message_id: str,
    sender_label: str,
    body: str,
    received_at: str | None = None,
) -> bool:
    """Resolve a reply through its provider conversation, never caller identity."""

    conversation_id = _safe_inline(
        provider_conversation_id, "provider conversation ID", 180
    )
    binding = connection.execute(
        "SELECT listing_key FROM conversation_bindings "
        "WHERE platform = ? AND account_scope = ? "
        "AND provider_conversation_id = ?",
        (
            re.sub(r"[^a-z0-9]+", "", platform.lower()),
            _safe_inline(account_scope, "account scope", 80),
            conversation_id,
        ),
    ).fetchone()
    if binding is None:
        raise ContactWorkflowError("Provider conversation is not bound to a listing")
    return record_landlord_reply(
        connection,
        listing_key=str(binding[0]),
        provider_message_id=provider_message_id,
        sender_label=sender_label,
        body=body,
        received_at=received_at,
    )


def pending_reply_notifications(
    connection: sqlite3.Connection, channel: str
) -> list[sqlite3.Row | tuple[Any, ...]]:
    if channel not in {"telegram", "email"}:
        raise ContactWorkflowError("Unsupported reply notification channel")
    return connection.execute(
        "SELECT reply.reply_key, reply.listing_key, reply.sender_label, "
        "reply.body, reply.received_at "
        "FROM landlord_replies AS reply "
        "JOIN reply_notification_deliveries AS delivery "
        "ON delivery.reply_key = reply.reply_key "
        "WHERE delivery.channel = ? AND delivery.status IN ('pending','blocked') "
        "ORDER BY reply.received_at, reply.reply_key",
        (channel,),
    ).fetchall()


def mark_reply_notified(
    connection: sqlite3.Connection, reply_key: str, channel: str
) -> None:
    column = {
        "telegram": "telegram_notified",
        "email": "email_notified",
    }.get(channel)
    if column is None:
        raise ContactWorkflowError("Unsupported reply notification channel")
    with connection:
        cursor = connection.execute(
            "UPDATE reply_notification_deliveries SET status = 'sent', "
            "sent_at = ?, lease_token = NULL, lease_expires_at = NULL "
            "WHERE reply_key = ? AND channel = ? "
            "AND status IN ('pending','blocked','claimed')",
            (utc_now(), reply_key, channel),
        )
        if cursor.rowcount != 1:
            raise ContactWorkflowError("Reply notification record is missing")
        connection.execute(
            f"UPDATE landlord_replies SET {column} = 1 WHERE reply_key = ?",
            (reply_key,),
        )


def claim_reply_notification(
    connection: sqlite3.Connection,
    channel: str,
    *,
    lease_seconds: int = 180,
) -> ReplyNotificationClaim | None:
    if channel not in {"telegram", "email"}:
        raise ContactWorkflowError("Unsupported reply notification channel")
    now = utc_now()
    token = secrets.token_hex(20)
    expiry = (
        dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(seconds=max(30, int(lease_seconds)))
    ).isoformat()
    with connection:
        # User notifications are at-least-once: an expired worker lease is
        # released so the channel cannot remain permanently stuck after a crash.
        connection.execute(
            "UPDATE reply_notification_deliveries SET status = 'pending', "
            "next_attempt_at = ?, lease_token = NULL, lease_expires_at = NULL, "
            "last_error_class = 'expired_claim_requeued' "
            "WHERE channel = ? AND status = 'claimed' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
            (now, channel, now),
        )
        row = connection.execute(
            """
            SELECT delivery.delivery_id, reply.reply_key, reply.listing_key,
                   reply.sender_label, reply.body, reply.received_at,
                   delivery.attempts
            FROM reply_notification_deliveries AS delivery
            JOIN landlord_replies AS reply ON reply.reply_key = delivery.reply_key
            WHERE delivery.channel = ? AND delivery.status = 'pending'
              AND delivery.next_attempt_at <= ?
            ORDER BY reply.received_at, delivery.delivery_id LIMIT 1
            """,
            (channel, now),
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            "UPDATE reply_notification_deliveries SET status = 'claimed', "
            "attempts = ?, lease_token = ?, lease_expires_at = ? "
            "WHERE delivery_id = ? AND status = 'pending'",
            (int(row[6]) + 1, token, expiry, int(row[0])),
        )
        if cursor.rowcount != 1:
            return None
    return ReplyNotificationClaim(
        delivery_id=int(row[0]),
        reply_key=str(row[1]),
        listing_key=str(row[2]),
        channel=channel,
        sender_label=str(row[3]),
        body=str(row[4]),
        received_at=str(row[5]),
        lease_token=token,
    )


def complete_reply_notification(
    connection: sqlite3.Connection,
    claim: ReplyNotificationClaim,
    *,
    provider_message_id: str = "",
) -> None:
    message_id = (
        _safe_inline(provider_message_id, "notification provider message ID", 180)
        if provider_message_id
        else None
    )
    column = {
        "telegram": "telegram_notified",
        "email": "email_notified",
    }[claim.channel]
    with connection:
        cursor = connection.execute(
            "UPDATE reply_notification_deliveries SET status = 'sent', sent_at = ?, "
            "provider_message_id = ?, lease_token = NULL, lease_expires_at = NULL, "
            "last_error_class = NULL WHERE delivery_id = ? AND status = 'claimed' "
            "AND lease_token = ?",
            (utc_now(), message_id, claim.delivery_id, claim.lease_token),
        )
        if cursor.rowcount != 1:
            raise ContactWorkflowError("Reply notification claim is stale")
        connection.execute(
            f"UPDATE landlord_replies SET {column} = 1 WHERE reply_key = ?",
            (claim.reply_key,),
        )


def mark_reply_notification_ambiguous(
    connection: sqlite3.Connection,
    claim: ReplyNotificationClaim,
    *,
    error_class: str,
) -> None:
    safe_error = _safe_inline(error_class, "notification error", 120)
    with connection:
        cursor = connection.execute(
            "UPDATE reply_notification_deliveries SET status = 'ambiguous', "
            "last_error_class = ?, lease_token = NULL, lease_expires_at = NULL "
            "WHERE delivery_id = ? AND status = 'claimed' AND lease_token = ?",
            (safe_error, claim.delivery_id, claim.lease_token),
        )
        if cursor.rowcount != 1:
            raise ContactWorkflowError("Reply notification claim is stale")
