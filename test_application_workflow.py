from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

import application_workflow as workflow


class ApplicationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_profile_path = workflow.CONTACT_PROFILE_PATH
        workflow.CONTACT_PROFILE_PATH = (
            Path(__file__).parent / "examples" / "contact_profile.example.json"
        )
        self.database = sqlite3.connect(":memory:")
        self.database.execute(
            "CREATE TABLE seen (listing_key TEXT PRIMARY KEY)"
        )
        self.database.executemany(
            "INSERT INTO seen(listing_key) VALUES (?)",
            [("immoscout24:100",), ("immowelt:200",)],
        )
        workflow.initialize_contact_database(self.database)
        self.profile = workflow.load_contact_profile()

    def tearDown(self) -> None:
        self.database.close()
        workflow.CONTACT_PROFILE_PATH = self.original_profile_path

    def draft(self, listing_key: str, fingerprint: str = ""):
        subject, body = workflow.render_initial_contact(
            self.profile,
            listing_title="Ruhige 3-Zimmer-Wohnung",
            listing_text="Ruhige 3-Zimmer-Wohnung mit Balkon in Berlin-Mitte",
            location="Berlin-Mitte",
            evidence_highlight="mit Balkon",
        )
        return workflow.create_contact_draft(
            self.database,
            listing_key=listing_key,
            channel="platform_message",
            profile_version=self.profile["profile_version"],
            subject=subject,
            body=body,
            fingerprint=fingerprint,
        )

    def test_listing_key_is_platform_scoped(self) -> None:
        self.assertEqual(
            workflow.canonical_listing_key("ImmoScout24", "169837894"),
            "immoscout24:169837894",
        )
        with self.assertRaises(workflow.ContactWorkflowError):
            workflow.canonical_listing_key("ImmoScout24", "bad/id")

    def test_same_listing_can_only_have_one_contact_draft(self) -> None:
        self.assertTrue(self.draft("immoscout24:100").created)
        self.assertFalse(self.draft("immoscout24:100").created)
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM contact_applications"
            ).fetchone()[0],
            1,
        )

    def test_cross_platform_fingerprint_is_a_duplicate_hint(self) -> None:
        fingerprint = workflow.property_fingerprint(
            address="Musterstraße 1, Berlin",
            warm_rent_eur=1900,
            area_m2=80,
            rooms=3,
        )
        self.draft("immoscout24:100", fingerprint)
        second = self.draft("immowelt:200", fingerprint)
        self.assertEqual(
            second.possible_cross_platform_matches, ("immoscout24:100",)
        )

    def test_draft_uses_confirmed_facts_without_claiming_guarantee(self) -> None:
        _subject, body = workflow.render_initial_contact(
            self.profile,
            listing_title="Wohnung am Park",
            listing_text="Wohnung am Park mit großem Wohnzimmer",
            location="Potsdam",
            evidence_highlight="großem Wohnzimmer",
        )
        self.assertIn("Hauptmieter", body)
        self.assertIn("keine Haustiere", body)
        self.assertIn("finanziellen Nachweise", body)
        self.assertNotIn("Bürgschaft liegt vor", body)

    def test_ungrounded_personalization_is_rejected(self) -> None:
        with self.assertRaises(workflow.ContactWorkflowError):
            workflow.render_initial_contact(
                self.profile,
                listing_title="Wohnung am Park",
                listing_text="Wohnung am Park",
                location="Potsdam",
                evidence_highlight="mit Dachterrasse",
            )

    def test_send_requires_explicit_approval(self) -> None:
        draft = self.draft("immoscout24:100")
        with self.assertRaises(workflow.ContactWorkflowError):
            workflow.mark_contact_sent(
                self.database, "immoscout24:100", "conversation-1"
            )
        workflow.approve_contact(
            self.database,
            "immoscout24:100",
            expected_draft_hash=draft.draft_hash[:8],
            approval_message_id="test-approval-send",
        )
        workflow.mark_contact_sent(
            self.database, "immoscout24:100", "conversation-1"
        )
        self.assertEqual(
            self.database.execute(
                "SELECT status FROM contact_applications WHERE listing_key = ?",
                ("immoscout24:100",),
            ).fetchone()[0],
            "sent",
        )

    def test_reply_is_deduplicated_and_tracks_both_notifications(self) -> None:
        draft = self.draft("immoscout24:100")
        workflow.approve_contact(
            self.database,
            "immoscout24:100",
            expected_draft_hash=draft.draft_hash[:8],
            approval_message_id="test-approval-reply",
        )
        workflow.mark_contact_sent(
            self.database, "immoscout24:100", "conversation-1"
        )
        arguments = {
            "listing_key": "immoscout24:100",
            "provider_message_id": "reply-1",
            "sender_label": "Vermieter",
            "body": "Guten Tag, eine Besichtigung ist möglich.",
        }
        self.assertTrue(workflow.record_landlord_reply(self.database, **arguments))
        self.assertFalse(workflow.record_landlord_reply(self.database, **arguments))
        telegram = workflow.pending_reply_notifications(self.database, "telegram")
        email = workflow.pending_reply_notifications(self.database, "email")
        self.assertEqual(len(telegram), 1)
        self.assertEqual(len(email), 1)
        reply_key = str(telegram[0][0])
        workflow.mark_reply_notified(self.database, reply_key, "telegram")
        self.assertEqual(
            workflow.pending_reply_notifications(self.database, "telegram"), []
        )
        self.assertEqual(len(workflow.pending_reply_notifications(self.database, "email")), 1)


if __name__ == "__main__":
    unittest.main()
