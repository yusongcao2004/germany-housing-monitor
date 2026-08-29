#!/usr/bin/env python3
"""Parse official housing alert/reply emails as an anti-bot-safe data source."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from email.parser import Parser
from email.utils import parseaddr
from typing import Iterable

from application_workflow import ContactWorkflowError, record_landlord_reply
from housing_pipeline import (
    SourceListing,
    ingest_source_listing,
    initialize_pipeline_database,
    listing_identity_from_url,
)


OFFICIAL_SENDER_DOMAINS = {
    "immobilienscout24.de": "immoscout24",
    "wg-gesucht.de": "wggesucht",
    "immowelt.de": "immowelt",
    "kleinanzeigen.de": "kleinanzeigen",
}


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    sender: str
    subject: str
    received_at: str
    body: str
    authentication_results: str = ""


@dataclass(frozen=True)
class MailIngestResult:
    messages_new: int
    listing_links_new: int
    replies_new: int
    ignored: int


def promote_mail_listings_to_seen(connection: sqlite3.Connection) -> int:
    """Expose official alerts and refresh later snapshots without re-notifying."""

    rows = connection.execute(
        """
        SELECT source.listing_key, source.external_listing_id, source.platform,
               source.source_label, source.first_seen, source.last_seen,
               snapshot.payload_json
        FROM source_listings AS source
        JOIN (SELECT DISTINCT listing_key FROM mail_listing_links) AS link
          ON link.listing_key = source.listing_key
        JOIN listing_snapshots AS snapshot
          ON snapshot.source_listing_id = source.source_listing_id
        WHERE snapshot.snapshot_id = (
              SELECT MAX(latest.snapshot_id) FROM listing_snapshots AS latest
              WHERE latest.source_listing_id = source.source_listing_id
          )
        ORDER BY source.first_seen, source.listing_key
        """
    ).fetchall()
    promoted = 0
    for (
        listing_key,
        external_id,
        platform,
        source_label,
        source_first_seen,
        source_last_seen,
        payload_json,
    ) in rows:
        source_payload = json.loads(str(payload_json))
        payload = {
            "source": str(source_label or "official_saved_search_email"),
            "listing_id": str(external_id),
            "title": str(source_payload.get("title") or f"{platform} listing"),
            "raw_text": str(source_payload.get("raw_text") or ""),
            "url": str(source_payload.get("url") or ""),
            "platform": str(platform),
            "image_url": str(source_payload.get("image_url") or ""),
            "warm_rent_eur": source_payload.get("warm_rent_eur"),
            "cold_rent_eur": source_payload.get("cold_rent_eur"),
            "area_m2": source_payload.get("area_m2"),
            "rooms": source_payload.get("rooms"),
            "address": str(source_payload.get("address") or ""),
            "property_kind": str(
                source_payload.get("property_kind") or "apartment"
            ),
            "priority": 0,
            "warm_rent_verified": bool(
                source_payload.get("warm_rent_verified", False)
            ),
            "discovery_method": "official_saved_search_email",
            "source_account_scope": str(
                source_payload.get("account_scope") or "primary"
            ),
        }
        rendered_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        existing = connection.execute(
            "SELECT last_seen, payload_json FROM seen WHERE listing_key = ?",
            (str(listing_key),),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO seen("
                "listing_key, listing_id, source, first_seen, last_seen, notified, "
                "payload_json) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (
                    str(listing_key),
                    str(external_id),
                    str(source_label or "official_saved_search_email"),
                    str(source_first_seen),
                    str(source_last_seen),
                    rendered_payload,
                ),
            )
            promoted += 1
            continue
        current_last_seen = str(existing[0])
        refreshed_last_seen = max(current_last_seen, str(source_last_seen))
        if str(existing[1]) == rendered_payload and refreshed_last_seen == current_last_seen:
            continue
        connection.execute(
            "UPDATE seen SET listing_id = ?, source = ?, last_seen = ?, "
            "payload_json = ? WHERE listing_key = ?",
            (
                str(external_id),
                str(source_label or "official_saved_search_email"),
                refreshed_last_seen,
                rendered_payload,
                str(listing_key),
            ),
        )
    connection.commit()
    return promoted


def _official_platform(sender: str) -> str:
    _display_name, address = parseaddr(sender)
    if address.count("@") != 1:
        return ""
    domain = address.rsplit("@", 1)[1].strip().rstrip(".").casefold()
    if not domain:
        return ""
    return next(
        (
            platform
            for official_domain, platform in OFFICIAL_SENDER_DOMAINS.items()
            if domain == official_domain or domain.endswith("." + official_domain)
        ),
        "",
    )


def _authenticated_official_platform(message: MailMessage) -> str:
    sender_platform = _official_platform(message.sender)
    if not sender_platform or not message.authentication_results.strip():
        return ""
    try:
        headers = Parser().parsestr(
            message.authentication_results, headersonly=True
        )
    except (TypeError, ValueError):
        return ""
    for raw_result in headers.get_all("Authentication-Results", []):
        result = " ".join(str(raw_result).split())
        authserv_match = re.match(r"^([^;\s]+)\s*;", result)
        if not authserv_match or authserv_match.group(1).casefold() != "mx.google.com":
            continue
        for method_result in result.split(";")[1:]:
            if not re.search(r"\bdmarc\s*=\s*pass\b", method_result, re.IGNORECASE):
                continue
            header_from_match = re.search(
                r"\bheader\.from\s*=\s*([^\s;()]+)",
                method_result,
                re.IGNORECASE,
            )
            if not header_from_match:
                continue
            authenticated_domain = (
                header_from_match.group(1)
                .strip("\"'<>[]")
                .rstrip(".")
                .casefold()
            )
            authenticated_platform = next(
                (
                    platform
                    for official_domain, platform in OFFICIAL_SENDER_DOMAINS.items()
                    if authenticated_domain == official_domain
                    or authenticated_domain.endswith("." + official_domain)
                ),
                "",
            )
            if authenticated_platform == sender_platform:
                return sender_platform
    return ""


def _safe_message_id(message: MailMessage) -> str:
    rendered = " ".join(message.message_id.split())
    if rendered and len(rendered) <= 300:
        return rendered
    if rendered:
        return "message-id-sha256:" + hashlib.sha256(
            message.message_id.encode("utf-8", "replace")
        ).hexdigest()
    fallback_identity = "\n".join(
        (
            "sender=" + " ".join(message.sender.casefold().split()),
            "received_at=" + " ".join(message.received_at.split()),
            "subject=" + " ".join(message.subject.split()),
            "body_sha256="
            + hashlib.sha256(message.body.encode("utf-8", "replace")).hexdigest(),
        )
    )
    return "fallback-sha256:" + hashlib.sha256(
        fallback_identity.encode("utf-8")
    ).hexdigest()


def extract_listing_links(text: str) -> tuple[tuple[str, str, str], ...]:
    decoded = html.unescape(text)
    urls = re.findall(r"https?://[^\s<>\"']+", decoded)
    results: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for raw in urls:
        cleaned = raw.rstrip(".,;:!?)]}>")
        try:
            platform, external_id, canonical = listing_identity_from_url(cleaned)
        except Exception:
            continue
        if (
            platform not in set(OFFICIAL_SENDER_DOMAINS.values())
            or external_id.startswith("url_")
            or canonical in seen
        ):
            continue
        seen.add(canonical)
        results.append((platform, external_id, canonical))
    return tuple(results)


def _german_number(value: str) -> float:
    return float(value.replace(".", "").replace(",", "."))


def _extract_numeric_facts(text: str) -> tuple[float | None, float | None, float | None]:
    warm = re.search(
        r"(?:Warmmiete|Gesamtmiete)[^\d€]{0,30}([\d.]+(?:,\d{1,2})?)\s*€",
        text,
        flags=re.IGNORECASE,
    )
    rooms = re.search(r"([\d.,]+)\s*(?:Zimmer|Zi\.)", text, flags=re.IGNORECASE)
    area = re.search(r"([\d.,]+)\s*m²", text, flags=re.IGNORECASE)
    return (
        _german_number(warm.group(1)) if warm else None,
        _german_number(rooms.group(1)) if rooms else None,
        _german_number(area.group(1)) if area else None,
    )


def _looks_like_reply(subject: str, body: str) -> bool:
    haystack = f"{subject}\n{body[:1200]}".casefold()
    markers = (
        "neue nachricht",
        "neue antwort",
        "hat ihnen geantwortet",
        "hat dir geantwortet",
        "antwort auf ihre anfrage",
        "antwort auf deine anfrage",
    )
    return any(marker in haystack for marker in markers)


def initialize_mail_database(connection: sqlite3.Connection) -> None:
    initialize_pipeline_database(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_source_messages (
            message_key TEXT PRIMARY KEY,
            provider_message_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            received_at TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            classification TEXT NOT NULL,
            ingested_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_listing_links (
            message_key TEXT NOT NULL,
            listing_key TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            PRIMARY KEY(message_key, listing_key),
            FOREIGN KEY(message_key) REFERENCES mail_source_messages(message_key)
        )
        """
    )
    connection.commit()


def ingest_mail_messages(
    connection: sqlite3.Connection,
    messages: Iterable[MailMessage],
    *,
    account_scope: str = "primary",
) -> MailIngestResult:
    initialize_mail_database(connection)
    messages_new = links_new = replies_new = ignored = 0
    for message in messages:
        platform_from_sender = _authenticated_official_platform(message)
        if not platform_from_sender:
            ignored += 1
            continue
        provider_message_id = _safe_message_id(message)
        message_key = hashlib.sha256(
            f"{account_scope}\n{provider_message_id}".encode("utf-8")
        ).hexdigest()
        all_links = extract_listing_links(message.body)
        links = tuple(link for link in all_links if link[0] == platform_from_sender)
        classification = "reply_alert" if _looks_like_reply(message.subject, message.body) else "search_alert"
        with connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO mail_source_messages("
                "message_key, provider_message_id, sender, subject, received_at, "
                "body_hash, classification, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_key,
                    provider_message_id,
                    message.sender[:300],
                    message.subject[:500],
                    message.received_at,
                    hashlib.sha256(message.body.encode("utf-8", "replace")).hexdigest(),
                    classification,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )
        if cursor.rowcount != 1:
            continue
        messages_new += 1
        if not links:
            ignored += 1
            continue
        numeric_facts = (
            _extract_numeric_facts(message.body)
            if len(all_links) == 1
            else (None, None, None)
        )
        for platform, external_id, url in links:
            warm, rooms, area = numeric_facts
            listing = SourceListing(
                platform=platform,
                account_scope=account_scope,
                external_listing_id=external_id,
                title=message.subject[:300] or f"{platform} listing {external_id}",
                raw_text=message.body[:20000],
                url=url,
                source_label="official_saved_search_email",
                warm_rent_eur=warm,
                area_m2=area,
                rooms=rooms,
                warm_rent_verified=warm is not None,
            )
            ingest = ingest_source_listing(connection, listing)
            with connection:
                link_cursor = connection.execute(
                    "INSERT OR IGNORE INTO mail_listing_links("
                    "message_key, listing_key, canonical_url) VALUES (?, ?, ?)",
                    (message_key, ingest.listing_key, url),
                )
            links_new += int(link_cursor.rowcount == 1)
            if classification != "reply_alert":
                continue
            application = connection.execute(
                "SELECT status FROM contact_applications WHERE listing_key = ?",
                (ingest.listing_key,),
            ).fetchone()
            if application is None or str(application[0]) not in {"sent", "replied"}:
                continue
            try:
                replies_new += int(
                    record_landlord_reply(
                        connection,
                        listing_key=ingest.listing_key,
                        provider_message_id=f"email:{provider_message_id}",
                        sender_label=message.sender[:180],
                        body=(
                            "[Untrusted external email alert]\n"
                            + message.body[:8000]
                        ),
                        received_at=message.received_at,
                    )
                )
            except ContactWorkflowError:
                # Keep the alert for audit, but never guess a conversation binding.
                pass
    return MailIngestResult(messages_new, links_new, replies_new, ignored)
