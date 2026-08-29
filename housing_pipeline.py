#!/usr/bin/env python3
"""Provider-neutral listing ingestion and conservative cross-site deduplication.

Every source listing keeps its own stable identity.  A physical-property record
is created per listing initially; exact fingerprints across providers create a
review candidate only.  They are never merged automatically because two flats
in the same building can legitimately share an address, room count and rent.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any

from application_workflow import canonical_listing_key, property_fingerprint


SCHEMA_VERSION = 2


class ListingPipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceListing:
    platform: str
    account_scope: str
    external_listing_id: str
    title: str
    raw_text: str
    url: str
    source_label: str = ""
    image_url: str = ""
    address: str = ""
    warm_rent_eur: float | None = None
    cold_rent_eur: float | None = None
    area_m2: float | None = None
    rooms: float | None = None
    property_kind: str = "apartment"
    warm_rent_verified: bool = False
    published_at: str = ""

    @property
    def listing_key(self) -> str:
        return canonical_listing_key(self.platform, self.external_listing_id)

    @property
    def fingerprint(self) -> str:
        return property_fingerprint(
            address=self.address,
            warm_rent_eur=self.warm_rent_eur,
            area_m2=self.area_m2,
            rooms=self.rooms,
        )


@dataclass(frozen=True)
class IngestResult:
    listing_key: str
    property_id: str
    snapshot_created: bool
    duplicate_candidates: tuple[str, ...]


@dataclass(frozen=True)
class BackfillResult:
    scanned: int
    ingested: int
    skipped: int


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_platform(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", value.lower())
    aliases = {
        "immobilienscout24": "immoscout24",
        "immoscout": "immoscout24",
        "wg": "wggesucht",
        "wggesuchtde": "wggesucht",
        "immoweltde": "immowelt",
        "kleinanzeigende": "kleinanzeigen",
    }
    normalized = aliases.get(normalized, normalized)
    if not normalized:
        raise ListingPipelineError("Platform is missing")
    return normalized


def _stable_id(prefix: str, value: str, length: int = 24) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def canonicalize_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        raise ListingPipelineError("Listing URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ListingPipelineError("Listing URL must be HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ListingPipelineError("Listing URL must not contain user information")
    try:
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ListingPipelineError("Listing URL has an invalid port") from exc
    if not host:
        raise ListingPipelineError("Listing URL host is missing")
    default_port = 80 if parsed.scheme == "http" else 443
    if port is not None and port != default_port:
        raise ListingPipelineError("Listing URL must use the default HTTP(S) port")
    if host.startswith("www."):
        host = host[4:]
    authority = f"[{host}]" if ":" in host else host
    safe_query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in {
            "tracking",
            "ref",
            "referrer",
            "source",
            "campaign",
        }:
            continue
        safe_query.append((key, value))
    return urllib.parse.urlunsplit(
        (
            "https",
            authority,
            re.sub(r"/{2,}", "/", parsed.path).rstrip("/") or "/",
            urllib.parse.urlencode(sorted(safe_query)),
            "",
        )
    )


def listing_identity_from_url(url: str) -> tuple[str, str, str]:
    canonical = canonicalize_url(url)
    parsed = urllib.parse.urlsplit(canonical)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path
    patterns = (
        ("immoscout24", r"(?:^|/)expose/(\d+)(?:/|$)", ("immobilienscout24.de",)),
        (
            "wggesucht",
            r"(?:^|[/.])(\d{7,})\.html(?:$|/)",
            ("wg-gesucht.de",),
        ),
        ("immowelt", r"(?:^|/)expose/([A-Za-z0-9_-]+)(?:/|$)", ("immowelt.de",)),
        (
            "kleinanzeigen",
            r"/s-anzeige/[^/]+/(\d+(?:-\d+)*)$",
            ("kleinanzeigen.de",),
        ),
    )
    for platform, pattern, hosts in patterns:
        if any(host == item or host.endswith("." + item) for item in hosts):
            match = re.search(pattern, path)
            if match:
                return platform, match.group(1), canonical
            return platform, _stable_id("url", canonical), canonical
    return normalize_platform(host.split(".", 1)[0]), _stable_id("url", canonical), canonical


def initialize_pipeline_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            component TEXT NOT NULL,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY(component, version)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_listings (
            source_listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_key TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            external_listing_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            title TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            source_label TEXT NOT NULL DEFAULT '',
            property_fingerprint TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(platform, account_scope, external_listing_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS listing_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_listing_id INTEGER NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            UNIQUE(source_listing_id, payload_hash),
            FOREIGN KEY(source_listing_id) REFERENCES source_listings(source_listing_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS properties (
            property_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            merge_state TEXT NOT NULL DEFAULT 'single'
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS property_memberships (
            property_id TEXT NOT NULL,
            source_listing_id INTEGER NOT NULL UNIQUE,
            assigned_at TEXT NOT NULL,
            assignment_kind TEXT NOT NULL DEFAULT 'initial',
            PRIMARY KEY(property_id, source_listing_id),
            FOREIGN KEY(property_id) REFERENCES properties(property_id),
            FOREIGN KEY(source_listing_id) REFERENCES source_listings(source_listing_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS dedupe_candidates (
            left_source_listing_id INTEGER NOT NULL,
            right_source_listing_id INTEGER NOT NULL,
            confidence TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            PRIMARY KEY(left_source_listing_id, right_source_listing_id),
            CHECK(left_source_listing_id < right_source_listing_id),
            FOREIGN KEY(left_source_listing_id) REFERENCES source_listings(source_listing_id),
            FOREIGN KEY(right_source_listing_id) REFERENCES source_listings(source_listing_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS eligibility_evaluations (
            evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_key TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            evaluated_at TEXT NOT NULL,
            expires_at TEXT,
            evaluator TEXT NOT NULL,
            UNIQUE(listing_key, policy_version, evaluated_at)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_fingerprint "
        "ON source_listings(property_fingerprint, platform)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_eligibility_latest "
        "ON eligibility_evaluations(listing_key, evaluated_at DESC)"
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(component, version, applied_at) "
        "VALUES('housing_pipeline', ?, ?)",
        (SCHEMA_VERSION, utc_now()),
    )
    connection.commit()


def ingest_source_listing(
    connection: sqlite3.Connection, listing: SourceListing
) -> IngestResult:
    initialize_pipeline_database(connection)
    platform = normalize_platform(listing.platform)
    listing_key = canonical_listing_key(platform, listing.external_listing_id)
    canonical_url = canonicalize_url(listing.url)
    now = utc_now()
    payload = asdict(listing)
    payload["platform"] = platform
    payload["url"] = canonical_url
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    fingerprint = property_fingerprint(
        address=listing.address,
        warm_rent_eur=listing.warm_rent_eur,
        area_m2=listing.area_m2,
        rooms=listing.rooms,
    )
    with connection:
        connection.execute(
            """
            INSERT INTO source_listings(
                listing_key, platform, account_scope, external_listing_id,
                canonical_url, title, raw_text, source_label,
                property_fingerprint, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_key) DO UPDATE SET
                canonical_url = excluded.canonical_url,
                title = excluded.title,
                raw_text = excluded.raw_text,
                source_label = excluded.source_label,
                property_fingerprint = excluded.property_fingerprint,
                last_seen = excluded.last_seen,
                active = 1
            """,
            (
                listing_key,
                platform,
                listing.account_scope,
                listing.external_listing_id,
                canonical_url,
                listing.title,
                listing.raw_text,
                listing.source_label,
                fingerprint,
                now,
                now,
            ),
        )
        source_id = int(
            connection.execute(
                "SELECT source_listing_id FROM source_listings WHERE listing_key = ?",
                (listing_key,),
            ).fetchone()[0]
        )
        snapshot_cursor = connection.execute(
            "INSERT OR IGNORE INTO listing_snapshots("
            "source_listing_id, payload_hash, payload_json, captured_at) "
            "VALUES (?, ?, ?, ?)",
            (source_id, payload_hash, payload_json, now),
        )
        membership = connection.execute(
            "SELECT property_id FROM property_memberships WHERE source_listing_id = ?",
            (source_id,),
        ).fetchone()
        if membership is None:
            property_id = _stable_id("property", listing_key)
            connection.execute(
                "INSERT OR IGNORE INTO properties(property_id, created_at, updated_at) "
                "VALUES (?, ?, ?)",
                (property_id, now, now),
            )
            connection.execute(
                "INSERT INTO property_memberships("
                "property_id, source_listing_id, assigned_at) VALUES (?, ?, ?)",
                (property_id, source_id, now),
            )
        else:
            property_id = str(membership[0])

        candidates: list[str] = []
        if fingerprint:
            rows = connection.execute(
                "SELECT source_listing_id, listing_key FROM source_listings "
                "WHERE property_fingerprint = ? AND source_listing_id <> ? "
                "AND platform <> ? ORDER BY listing_key",
                (fingerprint, source_id, platform),
            ).fetchall()
            for other_id, other_key in rows:
                left, right = sorted((source_id, int(other_id)))
                connection.execute(
                    "INSERT OR IGNORE INTO dedupe_candidates("
                    "left_source_listing_id, right_source_listing_id, confidence, "
                    "reasons_json, created_at) VALUES (?, ?, 'high', ?, ?)",
                    (
                        left,
                        right,
                        json.dumps(
                            {
                                "exact_fingerprint": fingerprint,
                                "warning": "review_before_merge_or_second_contact",
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                candidates.append(str(other_key))
    return IngestResult(
        listing_key=listing_key,
        property_id=property_id,
        snapshot_created=snapshot_cursor.rowcount == 1,
        duplicate_candidates=tuple(candidates),
    )


def record_eligibility(
    connection: sqlite3.Connection,
    *,
    listing_key: str,
    policy_version: str,
    status: str,
    evidence: dict[str, Any],
    evaluator: str,
    expires_at: str | None = None,
) -> None:
    allowed = {"eligible", "rejected", "needs_review"}
    if status not in allowed:
        raise ListingPipelineError("Invalid eligibility status")
    initialize_pipeline_database(connection)
    connection.execute(
        "INSERT INTO eligibility_evaluations("
        "listing_key, policy_version, status, evidence_json, evaluated_at, "
        "expires_at, evaluator) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            listing_key,
            policy_version,
            status,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            utc_now(),
            expires_at,
            evaluator,
        ),
    )
    connection.commit()


def latest_eligibility(
    connection: sqlite3.Connection, listing_key: str
) -> sqlite3.Row | tuple[Any, ...] | None:
    return connection.execute(
        "SELECT status, policy_version, evidence_json, evaluated_at, expires_at, evaluator "
        "FROM eligibility_evaluations WHERE listing_key = ? "
        "ORDER BY evaluation_id DESC LIMIT 1",
        (listing_key,),
    ).fetchone()


def unresolved_duplicate_contacts(
    connection: sqlite3.Connection, listing_key: str
) -> tuple[str, ...]:
    row = connection.execute(
        "SELECT source_listing_id FROM source_listings WHERE listing_key = ?",
        (listing_key,),
    ).fetchone()
    if row is None:
        return ()
    source_id = int(row[0])
    rows = connection.execute(
        """
        SELECT other.listing_key
        FROM dedupe_candidates AS candidate
        JOIN source_listings AS other
          ON other.source_listing_id = CASE
             WHEN candidate.left_source_listing_id = ?
             THEN candidate.right_source_listing_id
             ELSE candidate.left_source_listing_id
          END
        JOIN contact_applications AS application
          ON application.listing_key = other.listing_key
        WHERE candidate.status = 'pending'
          AND (? IN (candidate.left_source_listing_id, candidate.right_source_listing_id))
          AND application.status IN ('approved', 'sent', 'replied')
        ORDER BY other.listing_key
        """,
        (source_id, source_id),
    ).fetchall()
    return tuple(str(item[0]) for item in rows)


def backfill_seen_listings(
    connection: sqlite3.Connection,
    *,
    account_scope: str = "primary",
) -> BackfillResult:
    """Idempotently mirror legacy ``seen`` rows into the provider-neutral model."""

    initialize_pipeline_database(connection)
    if not _table_exists(connection, "seen"):
        return BackfillResult(0, 0, 0)
    rows = connection.execute(
        "SELECT listing_key, payload_json FROM seen ORDER BY first_seen, listing_key"
    ).fetchall()
    ingested = skipped = 0
    for listing_key, payload_json in rows:
        try:
            payload = json.loads(str(payload_json))
            platform = str(payload.get("platform") or str(listing_key).split(":", 1)[0])
            external_id = str(
                payload.get("listing_id") or str(listing_key).split(":", 1)[1]
            )
            ingest_source_listing(
                connection,
                SourceListing(
                    platform=platform,
                    account_scope=account_scope,
                    external_listing_id=external_id,
                    title=str(payload.get("title") or f"Listing {external_id}"),
                    raw_text=str(payload.get("raw_text") or ""),
                    url=str(payload.get("url") or ""),
                    source_label=str(payload.get("source") or "legacy_seen"),
                    image_url=str(payload.get("image_url") or ""),
                    address=str(payload.get("address") or ""),
                    warm_rent_eur=payload.get("warm_rent_eur"),
                    cold_rent_eur=payload.get("cold_rent_eur"),
                    area_m2=payload.get("area_m2"),
                    rooms=payload.get("rooms"),
                    property_kind=str(payload.get("property_kind") or "apartment"),
                    warm_rent_verified=bool(
                        payload.get("warm_rent_verified", False)
                    ),
                ),
            )
        except (KeyError, IndexError, TypeError, ValueError, ListingPipelineError):
            skipped += 1
            continue
        ingested += 1
    return BackfillResult(len(rows), ingested, skipped)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )
