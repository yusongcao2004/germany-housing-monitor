from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import application_workflow as workflow
from apple_mail_source import APPLE_MAIL_READ_SCRIPT, parse_mail_rows
from contact_delivery import FakeContactTransport, dispatch_one_contact
from contact_pipeline import (
    approve_after_user_review,
    current_draft_preview,
    prepare_contact_drafts,
)
from housing_pipeline import (
    ListingPipelineError,
    SourceListing,
    canonicalize_url,
    ingest_source_listing,
    listing_identity_from_url,
)
from mail_sources import (
    MailMessage,
    extract_listing_links,
    ingest_mail_messages,
    promote_mail_listings_to_seen,
)
from monitor import (
    Listing,
    initialize_database,
    load_config,
    queue_unnotified_listings,
    upsert_listings,
)


def google_dmarc_pass(domain: str = "immowelt.de") -> str:
    return (
        "Authentication-Results: mx.google.com;\n"
        f" dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from={domain}\n"
    )


class WorkflowV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_profile_path = workflow.CONTACT_PROFILE_PATH
        workflow.CONTACT_PROFILE_PATH = (
            Path(__file__).parent / "examples" / "contact_profile.example.json"
        )
        self.database = sqlite3.connect(":memory:")
        initialize_database(self.database)
        self.config = load_config(
            Path(__file__).parent / "examples" / "config.example.json"
        )

    def tearDown(self) -> None:
        self.database.close()
        workflow.CONTACT_PROFILE_PATH = self.original_profile_path

    def listing(
        self,
        listing_id: str = "100",
        *,
        platform: str = "immoscout24",
        address: str = "Musterstraße 1, 14467 Potsdam",
    ) -> Listing:
        return Listing(
            source="Test region",
            platform=platform,
            listing_id=listing_id,
            title="Ruhige 3-Zimmer-Wohnung mit Balkon",
            raw_text=(
                "Ruhige 3-Zimmer-Wohnung mit Balkon, Einbauküche, "
                f"1.950 € Warmmiete, 92 m², {address}"
            ),
            url=(
                f"https://www.immobilienscout24.de/expose/{listing_id}"
                if platform == "immoscout24"
                else f"https://www.immowelt.de/expose/{listing_id}"
            ),
            warm_rent_eur=1950,
            area_m2=92,
            rooms=3,
            address=address,
            warm_rent_verified=True,
        )

    def prepare(self, listing: Listing) -> dict:
        upsert_listings(self.database, [listing], baseline=False)
        result = prepare_contact_drafts(
            self.database,
            self.config,
            listing_keys=[listing.key],
            only_unnotified=False,
            use_deepseek=False,
        )
        self.assertEqual(result.created, 1)
        return current_draft_preview(self.database, listing.key)

    def approve(self, listing: Listing, preview: dict, message_id: str = "approval-1"):
        return approve_after_user_review(
            self.database,
            config=self.config,
            listing_key=listing.key,
            expected_draft_hash=preview["draft_hash_prefix"],
            approved_by="Example Operator",
            approval_source="test",
            approval_message_id=message_id,
            review_evidence={
                "layout_requirement_met": True,
                "move_in_date": "2027-01-15",
                "commute_requirement_met": True,
                "amenity_requirement_met": True,
                "warm_rent_eur": 1950,
                "warm_rent_verified": True,
                "layout_evidence": "detail: layout matches configured requirement",
                "move_in_date_evidence": "detail: available 2027-01-15",
                "commute_evidence": "route checked",
                "amenity_evidence": "nearby store checked",
                "warm_rent_evidence": "detail: 1950 warm rent",
            },
        )

    def test_source_identity_and_snapshots_are_stable(self) -> None:
        platform, listing_id, canonical = listing_identity_from_url(
            "https://www.immobilienscout24.de/expose/169837894?utm_source=x"
        )
        self.assertEqual((platform, listing_id), ("immoscout24", "169837894"))
        self.assertNotIn("utm_source", canonical)
        source = SourceListing(
            platform="ImmoScout24",
            account_scope="primary",
            external_listing_id="169837894",
            title="Test",
            raw_text="Test listing",
            url=canonical,
        )
        first = ingest_source_listing(self.database, source)
        second = ingest_source_listing(self.database, source)
        self.assertTrue(first.snapshot_created)
        self.assertFalse(second.snapshot_created)
        self.assertEqual(
            self.database.execute("SELECT COUNT(*) FROM source_listings").fetchone()[0],
            1,
        )

    def test_cross_site_match_is_review_candidate_not_auto_merge(self) -> None:
        first = self.listing("100", platform="immoscout24")
        second = self.listing("200", platform="immowelt")
        upsert_listings(self.database, [first, second], baseline=False)
        property_count = self.database.execute(
            "SELECT COUNT(*) FROM properties"
        ).fetchone()[0]
        candidate_count = self.database.execute(
            "SELECT COUNT(*) FROM dedupe_candidates"
        ).fetchone()[0]
        self.assertEqual(property_count, 2)
        self.assertEqual(candidate_count, 1)

    def test_approval_binds_hash_and_new_revision_revokes_old_approval(self) -> None:
        listing = self.listing()
        preview = self.prepare(listing)
        self.approve(listing, preview)
        profile = workflow.load_contact_profile()
        subject, body = workflow.render_initial_contact(
            profile,
            listing_title="Ruhige 3-Zimmer-Wohnung mit Balkon",
            listing_text=listing.raw_text,
            location=listing.address,
            evidence_highlight="Einbauküche",
        )
        revised = workflow.create_contact_draft(
            self.database,
            listing_key=listing.key,
            channel="platform_message",
            profile_version=profile["profile_version"],
            subject=subject + " (aktualisiert)",
            body=body,
        )
        self.assertEqual(revised.revision, 2)
        self.assertEqual(
            self.database.execute(
                "SELECT status FROM contact_applications WHERE listing_key = ?",
                (listing.key,),
            ).fetchone()[0],
            "approval_pending",
        )
        self.assertIsNotNone(
            self.database.execute(
                "SELECT revoked_at FROM contact_approvals WHERE listing_key = ?",
                (listing.key,),
            ).fetchone()[0]
        )
        with self.assertRaises(workflow.ContactWorkflowError):
            workflow.approve_contact(
                self.database,
                listing.key,
                expected_draft_hash=preview["draft_hash_prefix"],
                approval_message_id="stale-draft-test",
            )

    def test_only_one_worker_claims_and_ambiguous_is_not_retried(self) -> None:
        listing = self.listing()
        preview = self.prepare(listing)
        self.approve(listing, preview)
        first_claim = workflow.claim_contact_send(self.database, listing.key)
        self.assertIsNotNone(first_claim)
        self.assertIsNone(workflow.claim_contact_send(self.database, listing.key))
        assert first_claim is not None
        workflow.mark_contact_send_ambiguous(
            self.database, first_claim, error_class="timeout_after_submit"
        )
        self.assertIsNone(workflow.claim_contact_send(self.database, listing.key))

    def test_approval_requires_every_hard_condition(self) -> None:
        listing = self.listing()
        preview = self.prepare(listing)
        with self.assertRaises(workflow.ContactWorkflowError):
            workflow.approve_contact(
                self.database,
                listing.key,
                expected_draft_hash=preview["draft_hash_prefix"],
                approval_message_id="bypass-attempt",
            )
        with self.assertRaises(workflow.ContactWorkflowError):
            approve_after_user_review(
                self.database,
                config=self.config,
                listing_key=listing.key,
                expected_draft_hash=preview["draft_hash_prefix"],
                approved_by="Example Operator",
                approval_source="test",
                approval_message_id="incomplete-review",
                review_evidence={"layout_requirement_met": True},
            )
        with self.assertRaises(workflow.ContactWorkflowError):
            approve_after_user_review(
                self.database,
                config=self.config,
                listing_key=listing.key,
                expected_draft_hash=preview["draft_hash_prefix"],
                approved_by="Example Operator",
                approval_source="test",
                approval_message_id="over-budget-review",
                review_evidence={
                    "layout_requirement_met": True,
                    "move_in_date": "2027-01-15",
                    "commute_requirement_met": True,
                    "amenity_requirement_met": True,
                    "warm_rent_eur": 2000.01,
                    "warm_rent_verified": True,
                    "layout_evidence": "detail: layout matches configured requirement",
                    "move_in_date_evidence": "detail: available 2027-01-15",
                    "commute_evidence": "route checked",
                    "amenity_evidence": "nearby store checked",
                    "warm_rent_evidence": "detail: 2000.01 warm rent",
                },
            )
        self.assertEqual(
            self.database.execute(
                "SELECT status FROM contact_applications WHERE listing_key = ?",
                (listing.key,),
            ).fetchone()[0],
            "approval_pending",
        )

    def test_expired_approval_is_never_claimed(self) -> None:
        listing = self.listing()
        preview = self.prepare(listing)
        self.approve(listing, preview)
        self.database.execute(
            "UPDATE contact_approvals SET expires_at = '2000-01-01T00:00:00+00:00'"
        )
        self.database.commit()
        self.assertIsNone(workflow.claim_contact_send(self.database, listing.key))
        self.assertEqual(
            self.database.execute(
                "SELECT status FROM contact_send_outbox WHERE listing_key = ?",
                (listing.key,),
            ).fetchone()[0],
            "blocked",
        )

    def test_shadow_config_is_a_second_real_send_gate(self) -> None:
        listing = self.listing()
        preview = self.prepare(listing)
        self.approve(listing, preview)
        transport = FakeContactTransport()
        transport.is_real = True
        result = dispatch_one_contact(
            self.database,
            transport,
            listing_key=listing.key,
            real_send_enabled=True,
        )
        self.assertEqual(result["reason"], "real_send_disabled_by_double_gate")
        self.assertEqual(
            self.database.execute(
                "SELECT status FROM contact_send_outbox WHERE listing_key = ?",
                (listing.key,),
            ).fetchone()[0],
            "queued",
        )

    def test_provider_reply_routes_by_conversation_and_queues_both_channels(self) -> None:
        listing = self.listing()
        preview = self.prepare(listing)
        self.approve(listing, preview)
        sent = dispatch_one_contact(
            self.database,
            FakeContactTransport(),
            listing_key=listing.key,
        )
        self.assertEqual(sent["outcome"], "sent")
        self.assertTrue(
            workflow.record_provider_reply(
                self.database,
                platform="immoscout24",
                account_scope="primary",
                provider_conversation_id=f"fake-conversation:{listing.key}",
                provider_message_id="reply-1",
                sender_label="Vermieter",
                body="Besichtigung möglich",
            )
        )
        self.assertFalse(
            workflow.record_provider_reply(
                self.database,
                platform="immoscout24",
                account_scope="primary",
                provider_conversation_id=f"fake-conversation:{listing.key}",
                provider_message_id="reply-1",
                sender_label="Vermieter",
                body="duplicate",
            )
        )
        channels = {
            row[0]
            for row in self.database.execute(
                "SELECT channel FROM reply_notification_deliveries"
            ).fetchall()
        }
        self.assertEqual(channels, {"telegram", "email"})
        with self.assertRaises(workflow.ContactWorkflowError):
            workflow.record_provider_reply(
                self.database,
                platform="immoscout24",
                account_scope="primary",
                provider_conversation_id="unknown",
                provider_message_id="reply-2",
                sender_label="Vermieter",
                body="wrong conversation",
            )

    def test_expired_reply_notification_lease_is_recovered(self) -> None:
        listing = self.listing()
        preview = self.prepare(listing)
        self.approve(listing, preview)
        dispatch_one_contact(
            self.database, FakeContactTransport(), listing_key=listing.key
        )
        workflow.record_provider_reply(
            self.database,
            platform="immoscout24",
            account_scope="primary",
            provider_conversation_id=f"fake-conversation:{listing.key}",
            provider_message_id="reply-expired-lease",
            sender_label="Vermieter",
            body="Besichtigung möglich",
        )
        self.database.execute(
            "UPDATE reply_notification_deliveries SET status = 'claimed', "
            "lease_token = 'dead-worker', "
            "lease_expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE channel = 'telegram'"
        )
        self.database.commit()
        claim = workflow.claim_reply_notification(self.database, "telegram")
        self.assertIsNotNone(claim)
        self.assertNotEqual(claim.lease_token, "dead-worker")

    def test_official_mail_alert_is_deduplicated(self) -> None:
        message = MailMessage(
            message_id="mail-1",
            sender="alerts@immowelt.de",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:00+00:00",
            body=(
                "Warmmiete 1.950 €, 4 Zimmer, 92 m² "
                "https://www.immowelt.de/expose/ABC_123"
            ),
            authentication_results=google_dmarc_pass(),
        )
        first = ingest_mail_messages(self.database, [message])
        second = ingest_mail_messages(self.database, [message])
        self.assertEqual(first.messages_new, 1)
        self.assertEqual(first.listing_links_new, 1)
        self.assertEqual(second.messages_new, 0)
        self.assertEqual(promote_mail_listings_to_seen(self.database), 1)
        self.assertEqual(promote_mail_listings_to_seen(self.database), 0)
        payload = self.database.execute(
            "SELECT payload_json FROM seen WHERE listing_key = 'immowelt:ABC_123'"
        ).fetchone()[0]
        self.assertIn('"discovery_method": "official_saved_search_email"', payload)
        self.assertEqual(
            extract_listing_links(message.body)[0][0:2], ("immowelt", "ABC_123")
        )

    def test_official_mail_only_accepts_real_listing_urls(self) -> None:
        links = extract_listing_links(
            " ".join(
                (
                    "https://www.immobilienscout24.de/expose/169837894",
                    "https://www.wg-gesucht.de/wohnungen-in-Berlin.12345678.html",
                    "https://www.immowelt.de/expose/ABC_123",
                    "https://www.kleinanzeigen.de/s-anzeige/wohnung/123456789-203-1234",
                    "https://www.immowelt.de/ratgeber",
                    "https://click.immowelt.de/tracking-token",
                    "https://www.wg-gesucht.de/hilfe",
                    "https://www.immobilienscout24.de/meinkonto",
                    "https://www.kleinanzeigen.de/s-hilfe.html",
                )
            )
        )
        self.assertEqual(
            {(platform, listing_id) for platform, listing_id, _url in links},
            {
                ("immoscout24", "169837894"),
                ("wggesucht", "12345678"),
                ("immowelt", "ABC_123"),
                ("kleinanzeigen", "123456789-203-1234"),
            },
        )
        self.assertFalse(
            any(listing_id.startswith("url_") for _platform, listing_id, _url in links)
        )

    def test_listing_urls_reject_userinfo_and_non_default_ports(self) -> None:
        authority_confusion_urls = (
            "https://www.immowelt.de:pw@evil.example/expose/ABC_123",
            "https://www.immobilienscout24.de:pw@evil.example/expose/169837894",
            "https://www.wg-gesucht.de:pw@evil.example/wohnung.13746727.html",
            "https://www.kleinanzeigen.de:pw@evil.example/s-anzeige/wohnung/123-456",
        )
        for url in authority_confusion_urls:
            with self.subTest(url=url):
                with self.assertRaises(ListingPipelineError):
                    listing_identity_from_url(url)
        self.assertEqual(extract_listing_links(" ".join(authority_confusion_urls)), ())
        with self.assertRaises(ListingPipelineError):
            canonicalize_url("https://www.immowelt.de:not-a-port/expose/ABC_123")
        with self.assertRaises(ListingPipelineError):
            canonicalize_url("https://www.immowelt.de:8443/expose/ABC_123")
        self.assertEqual(
            canonicalize_url("https://www.immowelt.de:443/expose/ABC_123"),
            "https://immowelt.de/expose/ABC_123",
        )

    def test_wggesucht_identity_requires_a_listing_sized_numeric_suffix(self) -> None:
        real_listing_urls = {
            "https://www.wg-gesucht.de/wohnungen-in-Berlin.7630635.html": "7630635",
            "https://www.wg-gesucht.de/beliebiger-titel.13746727.html": "13746727",
        }
        for url, expected_id in real_listing_urls.items():
            with self.subTest(url=url):
                platform, listing_id, _canonical = listing_identity_from_url(url)
                self.assertEqual((platform, listing_id), ("wggesucht", expected_id))

        search_urls = (
            "https://www.wg-gesucht.de/suche/2-zimmer/wohnungen-in-Berlin.90.2.1.2.html",
            "https://www.wg-gesucht.de/wg-zimmer-in-Berlin.90.0.1.3.html",
        )
        for url in search_urls:
            with self.subTest(url=url):
                platform, listing_id, _canonical = listing_identity_from_url(url)
                self.assertEqual(platform, "wggesucht")
                self.assertTrue(listing_id.startswith("url_"))
        self.assertEqual(extract_listing_links(" ".join(search_urls)), ())

    def test_official_mail_sender_requires_real_official_address_domain(self) -> None:
        valid = MailMessage(
            message_id="mail-valid-domain",
            sender="Immowelt Alerts <alerts@news.eu.immowelt.de>",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:00+00:00",
            body="https://www.immowelt.de/expose/VALID_1",
            authentication_results=google_dmarc_pass("news.eu.immowelt.de"),
        )
        spoofed = MailMessage(
            message_id="mail-spoofed-domain",
            sender="Immowelt.de Support <attacker@example.com>",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:01+00:00",
            body="https://www.immowelt.de/expose/SPOOFED_1",
            authentication_results=google_dmarc_pass("example.com"),
        )
        result = ingest_mail_messages(self.database, [valid, spoofed])
        self.assertEqual(result.messages_new, 1)
        self.assertEqual(result.listing_links_new, 1)
        self.assertEqual(result.ignored, 1)
        rows = self.database.execute(
            "SELECT listing_key FROM source_listings ORDER BY listing_key"
        ).fetchall()
        self.assertEqual(rows, [("immowelt:VALID_1",)])

    def test_official_mail_rejects_forged_missing_or_mismatched_authentication(
        self,
    ) -> None:
        forged = MailMessage(
            message_id="mail-forged-auth",
            sender="alerts@immowelt.de",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:00+00:00",
            body="https://www.immowelt.de/expose/FORGED_AUTH",
            authentication_results=(
                "Authentication-Results: attacker.example;\n"
                " dmarc=pass header.from=immowelt.de\n"
            ),
        )
        missing = MailMessage(
            message_id="mail-missing-auth",
            sender="alerts@immowelt.de",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:01+00:00",
            body="https://www.immowelt.de/expose/MISSING_AUTH",
        )
        mismatched = MailMessage(
            message_id="mail-mismatched-auth",
            sender="alerts@immowelt.de",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:02+00:00",
            body="https://www.immowelt.de/expose/MISMATCHED_AUTH",
            authentication_results=google_dmarc_pass("wg-gesucht.de"),
        )
        result = ingest_mail_messages(
            self.database, [forged, missing, mismatched]
        )
        self.assertEqual(result.messages_new, 0)
        self.assertEqual(result.listing_links_new, 0)
        self.assertEqual(result.ignored, 3)
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM mail_source_messages"
            ).fetchone()[0],
            0,
        )

    def test_official_mail_sender_platform_must_match_every_listing_link(self) -> None:
        message = MailMessage(
            message_id="mail-mixed-platform-links",
            sender="alerts@immowelt.de",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:00+00:00",
            body=(
                "https://www.immowelt.de/expose/VALID_PLATFORM "
                "https://www.immobilienscout24.de/expose/169837894"
            ),
            authentication_results=google_dmarc_pass(),
        )
        result = ingest_mail_messages(self.database, [message])
        self.assertEqual(result.messages_new, 1)
        self.assertEqual(result.listing_links_new, 1)
        rows = self.database.execute(
            "SELECT listing_key FROM source_listings ORDER BY listing_key"
        ).fetchall()
        self.assertEqual(rows, [("immowelt:VALID_PLATFORM",)])

    def test_multi_listing_mail_does_not_copy_global_numeric_facts(self) -> None:
        message = MailMessage(
            message_id="mail-multiple-listings",
            sender="alerts@immowelt.de",
            subject="Zwei neue Immobilien",
            received_at="2026-08-07T20:00:00+00:00",
            body=(
                "Warmmiete 1.800 €, 3 Zimmer, 80 m² "
                "https://www.immowelt.de/expose/MULTI_1 "
                "Warmmiete 2.500 €, 4 Zimmer, 95 m² "
                "https://www.immowelt.de/expose/MULTI_2"
            ),
            authentication_results=google_dmarc_pass(),
        )
        result = ingest_mail_messages(self.database, [message])
        self.assertEqual(result.listing_links_new, 2)
        payloads = [
            json.loads(row[0])
            for row in self.database.execute(
                "SELECT snapshot.payload_json FROM listing_snapshots AS snapshot "
                "JOIN source_listings AS source "
                "ON source.source_listing_id = snapshot.source_listing_id "
                "ORDER BY source.listing_key"
            ).fetchall()
        ]
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertIsNone(payload["warm_rent_eur"])
            self.assertIsNone(payload["rooms"])
            self.assertIsNone(payload["area_m2"])
            self.assertFalse(payload["warm_rent_verified"])

    def test_later_mail_snapshot_refreshes_seen_without_duplicate_notification(
        self,
    ) -> None:
        unverified_config = json.loads(json.dumps(self.config))
        unverified_config["official_mail_sources"][
            "verified_saved_search_platforms"
        ] = []
        listing_url = "https://www.immowelt.de/expose/REFRESH_1"
        first = MailMessage(
            message_id="mail-refresh-1",
            sender="alerts@immowelt.de",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:00+00:00",
            body=listing_url,
            authentication_results=google_dmarc_pass(),
        )
        ingest_mail_messages(self.database, [first])
        self.assertEqual(promote_mail_listings_to_seen(self.database), 1)
        self.assertEqual(queue_unnotified_listings(self.database, unverified_config), 0)

        second = MailMessage(
            message_id="mail-refresh-2",
            sender="alerts@immowelt.de",
            subject="Aktualisierte Immobilie",
            received_at="2026-08-07T20:05:00+00:00",
            body=f"Warmmiete 1.950 €, 3 Zimmer, 82 m² {listing_url}",
            authentication_results=google_dmarc_pass(),
        )
        ingest_mail_messages(self.database, [second])
        self.assertEqual(promote_mail_listings_to_seen(self.database), 0)
        self.assertEqual(queue_unnotified_listings(self.database, unverified_config), 1)

        third = MailMessage(
            message_id="mail-refresh-3",
            sender="alerts@immowelt.de",
            subject="Nochmals aktualisierte Immobilie",
            received_at="2026-08-07T20:10:00+00:00",
            body=f"Warmmiete 2.050 €, 3 Zimmer, 85 m² {listing_url}",
            authentication_results=google_dmarc_pass(),
        )
        ingest_mail_messages(self.database, [third])
        self.assertEqual(promote_mail_listings_to_seen(self.database), 0)
        self.assertEqual(queue_unnotified_listings(self.database, unverified_config), 0)
        notified, payload_json = self.database.execute(
            "SELECT notified, payload_json FROM seen "
            "WHERE listing_key = 'immowelt:REFRESH_1'"
        ).fetchone()
        payload = json.loads(payload_json)
        self.assertEqual(notified, 1)
        self.assertEqual(payload["warm_rent_eur"], 2050.0)
        self.assertEqual(payload["area_m2"], 85.0)
        self.assertEqual(
            self.database.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
            1,
        )

    def test_empty_message_ids_use_stable_message_content_identity(self) -> None:
        first = MailMessage(
            message_id="",
            sender="alerts@immowelt.de",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:00+00:00",
            body="https://www.immowelt.de/expose/EMPTY_ID_1",
            authentication_results=google_dmarc_pass(),
        )
        second = MailMessage(
            message_id="",
            sender="alerts@immowelt.de",
            subject="Neue Immobilie",
            received_at="2026-08-07T20:00:00+00:00",
            body="https://www.immowelt.de/expose/EMPTY_ID_2",
            authentication_results=google_dmarc_pass(),
        )
        initial = ingest_mail_messages(self.database, [first, second])
        duplicate = ingest_mail_messages(self.database, [first])
        self.assertEqual(initial.messages_new, 2)
        self.assertEqual(initial.listing_links_new, 2)
        self.assertEqual(duplicate.messages_new, 0)
        provider_ids = {
            row[0]
            for row in self.database.execute(
                "SELECT provider_message_id FROM mail_source_messages"
            ).fetchall()
        }
        self.assertEqual(len(provider_ids), 2)
        self.assertTrue(
            all(value.startswith("fallback-sha256:") for value in provider_ids)
        )

    def test_apple_mail_rows_are_base64_decoded_without_delimiter_injection(self) -> None:
        fields = [
            "message-id",
            "alerts@wg-gesucht.de",
            "Neue Wohnung",
            "2026-08-07",
            "line one\nline\ttwo",
            google_dmarc_pass("wg-gesucht.de"),
        ]
        encoded = "\t".join(
            base64.b64encode(item.encode()).decode() for item in fields
        )
        parsed = parse_mail_rows(encoded)
        self.assertEqual(parsed.raw_rows, 1)
        self.assertEqual(parsed.messages[0].body, fields[4])
        self.assertEqual(parsed.messages[0].authentication_results, fields[5])

    def test_apple_mail_cutoff_is_resolved_as_a_script_variable(self) -> None:
        self.assertIn(
            "whose date received is greater than my cutoffDate",
            APPLE_MAIL_READ_SCRIPT,
        )
        self.assertIn("count of my outputLines", APPLE_MAIL_READ_SCRIPT)
        self.assertIn("set end of my outputLines", APPLE_MAIL_READ_SCRIPT)
        self.assertIn("/usr/bin/printf %s", APPLE_MAIL_READ_SCRIPT)
        self.assertNotIn(
            'set commandText to "/bin/printf %s ',
            APPLE_MAIL_READ_SCRIPT,
        )


if __name__ == "__main__":
    unittest.main()
