#!/usr/bin/env python3
"""Connect matching listings to immutable, approval-gated contact drafts."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from application_workflow import (
    ContactWorkflowError,
    approve_contact,
    create_contact_draft,
    load_contact_profile,
    property_fingerprint,
    render_initial_contact,
)
from housing_pipeline import record_eligibility
from personalizer import (
    PersonalizationError,
    deepseek_personalization,
    deterministic_personalization,
)


ELIGIBILITY_POLICY_VERSION = "generic-rental-v1"
REQUIRED_REVIEW_FLAGS = (
    "layout_requirement_met",
    "commute_requirement_met",
    "amenity_requirement_met",
    "warm_rent_verified",
)
REQUIRED_REVIEW_EVIDENCE = (
    "layout_evidence",
    "move_in_date_evidence",
    "commute_evidence",
    "amenity_evidence",
    "warm_rent_evidence",
)


@dataclass(frozen=True)
class DraftBatchResult:
    created: int
    already_present: int
    skipped: int
    deepseek_used: int
    fallback_used: int


def _payload_is_coarse_match(payload: dict[str, Any], config: dict[str, Any]) -> bool:
    rooms = payload.get("rooms")
    rent = payload.get("warm_rent_eur")
    if rooms is None or rent is None or not payload.get("warm_rent_verified", False):
        return False
    return (
        float(config["rooms_min"]) <= float(rooms) <= float(config["rooms_max"])
        and float(rent) <= float(config["warm_rent_target_eur"])
    )


def prepare_contact_drafts(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    *,
    listing_keys: Iterable[str] | None = None,
    only_unnotified: bool = True,
    use_deepseek: bool = False,
) -> DraftBatchResult:
    """Prepare drafts locally; never approves or sends them."""

    profile = load_contact_profile()
    clauses: list[str] = []
    parameters: list[Any] = []
    keys = tuple(dict.fromkeys(listing_keys or ()))
    if keys:
        placeholders = ",".join("?" for _ in keys)
        clauses.append(f"seen.listing_key IN ({placeholders})")
        parameters.extend(keys)
    if only_unnotified:
        clauses.append("seen.notified = 0")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        "SELECT seen.listing_key, seen.payload_json FROM seen" + where
        + " ORDER BY seen.first_seen, seen.listing_key",
        parameters,
    ).fetchall()
    created = already = skipped = deepseek_used = fallback_used = 0
    for listing_key, payload_json in rows:
        payload = json.loads(str(payload_json))
        if not _payload_is_coarse_match(payload, config):
            skipped += 1
            continue
        existing = connection.execute(
            "SELECT 1 FROM contact_applications WHERE listing_key = ?",
            (listing_key,),
        ).fetchone()
        if existing is not None:
            already += 1
            continue
        evidence = {
            "coarse_filter": {
                "rooms": payload.get("rooms"),
                "warm_rent_eur": payload.get("warm_rent_eur"),
                "warm_rent_verified": payload.get("warm_rent_verified"),
            },
            "still_requires_user_review": [
                "layout_requirement",
                "move_in_date",
                "commute_requirement",
                "nearby_amenity_requirement",
                "all_mandatory_costs_within_warm_rent_cap",
            ],
        }
        record_eligibility(
            connection,
            listing_key=str(listing_key),
            policy_version=ELIGIBILITY_POLICY_VERSION,
            status="needs_review",
            evidence=evidence,
            evaluator="coarse_filter",
        )
        listing_text = str(payload.get("raw_text") or payload.get("title") or "")
        provider_name = str(payload.get("provider_name") or "")
        personalization = deterministic_personalization(listing_text, provider_name)
        if use_deepseek:
            try:
                personalization = deepseek_personalization(
                    listing_text, provider_name=provider_name
                )
                deepseek_used += 1
            except PersonalizationError:
                fallback_used += 1
        subject, body = render_initial_contact(
            profile,
            listing_title=str(payload.get("title") or f"房源 {listing_key}"),
            listing_text=listing_text,
            location=str(payload.get("address") or payload.get("source") or "Deutschland"),
            landlord_salutation=personalization.salutation,
            evidence_highlight=personalization.evidence_highlight,
        )
        result = create_contact_draft(
            connection,
            listing_key=str(listing_key),
            channel="platform_message",
            profile_version=str(profile["profile_version"]),
            subject=subject,
            body=body,
            fingerprint=property_fingerprint(
                address=str(payload.get("address") or ""),
                warm_rent_eur=payload.get("warm_rent_eur"),
                area_m2=payload.get("area_m2"),
                rooms=payload.get("rooms"),
            ),
            generator=personalization.generator,
            evidence={
                "highlight": personalization.evidence_highlight,
                "salutation": personalization.salutation,
                "source": personalization.generator,
            },
        )
        created += int(result.created)
        already += int(not result.created)
    return DraftBatchResult(created, already, skipped, deepseek_used, fallback_used)


def approve_after_user_review(
    connection: sqlite3.Connection,
    *,
    config: dict[str, Any],
    listing_key: str,
    expected_draft_hash: str,
    approved_by: str,
    approval_source: str,
    approval_message_id: str | None = None,
    review_evidence: dict[str, Any] | None = None,
) -> str:
    """Bind the user's review and approval to the current immutable draft."""

    if not expected_draft_hash or len(expected_draft_hash) < 8:
        raise ContactWorkflowError("Approval must identify the current draft hash")
    if not approval_message_id:
        raise ContactWorkflowError("Approval must have a unique user message ID")
    evidence = dict(review_evidence or {})
    missing_flags = [key for key in REQUIRED_REVIEW_FLAGS if evidence.get(key) is not True]
    if missing_flags:
        raise ContactWorkflowError(
            "Required review confirmations are missing: " + ", ".join(missing_flags)
        )
    missing_evidence = []
    for key in REQUIRED_REVIEW_EVIDENCE:
        value = str(evidence.get(key) or "").strip()
        if not value or len(value) > 1000 or "\x00" in value:
            missing_evidence.append(key)
        else:
            evidence[key] = value
    if missing_evidence:
        raise ContactWorkflowError(
            "Required review evidence is missing: " + ", ".join(missing_evidence)
        )
    try:
        move_in = dt.date.fromisoformat(str(evidence["move_in_date"]))
        move_in_from = dt.date.fromisoformat(str(config["move_in_from"]))
        move_in_to = dt.date.fromisoformat(str(config["move_in_to"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContactWorkflowError("A verified ISO move-in date is required") from exc
    if not move_in_from <= move_in <= move_in_to:
        raise ContactWorkflowError("Verified move-in date is outside the accepted window")
    try:
        warm_rent = float(evidence["warm_rent_eur"])
        warm_rent_cap = float(config["warm_rent_target_eur"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ContactWorkflowError("A verified warm rent is required") from exc
    if isinstance(evidence.get("warm_rent_eur"), bool) or warm_rent <= 0:
        raise ContactWorkflowError("Verified warm rent must be a positive amount")
    if warm_rent > warm_rent_cap:
        raise ContactWorkflowError("Verified warm rent exceeds the hard budget cap")
    listing_row = connection.execute(
        "SELECT payload_json FROM seen WHERE listing_key = ?", (listing_key,)
    ).fetchone()
    if listing_row is None:
        raise ContactWorkflowError("Listing disappeared before approval")
    payload_json = str(listing_row[0])
    payload = json.loads(payload_json)
    current_warm_rent = payload.get("warm_rent_eur")
    if payload.get("warm_rent_verified") is not True or current_warm_rent is None:
        raise ContactWorkflowError("Current listing snapshot has no verified warm rent")
    if abs(float(current_warm_rent) - warm_rent) > 0.01:
        raise ContactWorkflowError("Reviewed warm rent no longer matches the listing snapshot")
    evidence["move_in_date"] = move_in.isoformat()
    evidence["warm_rent_eur"] = warm_rent
    evidence["listing_payload_sha256"] = hashlib.sha256(
        payload_json.encode("utf-8")
    ).hexdigest()
    evidence["listing_url"] = str(payload.get("url") or "")
    evidence["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    evidence["user_confirmed_interest_and_criteria_review"] = True
    approval_ttl_minutes = max(
        5, int(config.get("contacts", {}).get("approval_ttl_minutes", 60))
    )
    expires_at = (
        dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(minutes=approval_ttl_minutes)
    ).isoformat()
    record_eligibility(
        connection,
        listing_key=listing_key,
        policy_version=ELIGIBILITY_POLICY_VERSION,
        status="eligible",
        evidence=evidence,
        evaluator=f"user:{approved_by}",
        expires_at=expires_at,
    )
    try:
        return approve_contact(
            connection,
            listing_key,
            expected_draft_hash=expected_draft_hash,
            approved_by=approved_by,
            approval_source=approval_source,
            approval_message_id=approval_message_id,
            expires_at=expires_at,
        )
    except Exception:
        # Keep the eligibility audit record, but never turn a failed approval
        # into a queued send implicitly.
        raise


def current_draft_preview(
    connection: sqlite3.Connection, listing_key: str
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT application.status, draft.revision, draft.draft_hash,
               draft.subject, draft.body, draft.generator, draft.evidence_json
        FROM contact_applications AS application
        JOIN contact_draft_revisions AS draft
          ON draft.listing_key = application.listing_key
         AND draft.superseded_at IS NULL
        WHERE application.listing_key = ?
        """,
        (listing_key,),
    ).fetchone()
    if row is None:
        raise ContactWorkflowError("No current draft for this listing")
    return {
        "listing_key": listing_key,
        "status": str(row[0]),
        "revision": int(row[1]),
        "draft_hash": str(row[2]),
        "draft_hash_prefix": str(row[2])[:8],
        "subject": str(row[3]),
        "body": str(row[4]),
        "generator": str(row[5]),
        "evidence": json.loads(str(row[6])),
    }
