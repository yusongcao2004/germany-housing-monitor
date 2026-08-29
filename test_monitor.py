from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import plistlib
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("monitor.py")
SPEC = importlib.util.spec_from_file_location("housing_monitor", MODULE_PATH)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    @staticmethod
    def sample_listing(index: int = 1) -> monitor.Listing:
        return monitor.Listing(
            source="Berlin Landkreis",
            listing_id=str(900000000 + index),
            title=f"Testwohnung {index}",
            raw_text="",
            url=f"https://example.invalid/expose/{900000000 + index}",
            warm_rent_eur=1900.0,
            area_m2=82.0,
            rooms=3.0,
            address="Teststraße 1, Berlin",
        )

    @staticmethod
    def config(*, email_enabled: bool = False, batch_size: int = 15):
        return {
            "rooms_min": 3.0,
            "rooms_max": 4.0,
            "warm_rent_target_eur": 2000,
            "move_in_from": "2027-01-01",
            "move_in_to": "2027-01-31",
            "commute_destination": "Berlin Hauptbahnhof",
            "max_commute_minutes": 60,
            "amenity_query": "Supermarkt",
            "telegram": {"enabled": True},
            "email": {
                "enabled": email_enabled,
                "transport": "smtp",
                "recipient": "roommate@example.invalid",
                "sender": "sender@example.invalid",
                "smtp_username": "sender@example.invalid",
                "smtp_host": "smtp.example.invalid",
                "smtp_port": 465,
                "smtp_security": "ssl",
                "keychain_service": "test.housing.smtp",
                "max_listings_per_email": batch_size,
                "max_batches_per_run": 1,
                "max_age_hours": 24,
            },
        }

    @staticmethod
    def database() -> sqlite3.Connection:
        database = sqlite3.connect(":memory:")
        monitor.initialize_database(database)
        return database

    def test_example_config_loads_with_external_actions_disabled(self) -> None:
        config = monitor.load_config(
            MODULE_PATH.parent / "examples" / "config.example.json"
        )
        self.assertFalse(monitor.telegram_is_enabled(config))
        self.assertFalse(config["email"]["enabled"])
        self.assertFalse(config["contacts"]["enabled"])
        self.assertFalse(config["official_mail_sources"]["enabled"])

    def test_config_rejects_reversed_move_in_window(self) -> None:
        config = monitor.load_config(
            MODULE_PATH.parent / "examples" / "config.example.json"
        )
        config["move_in_from"] = "2027-03-01"
        config["move_in_to"] = "2027-01-01"
        with self.assertRaisesRegex(monitor.MonitorError, "precedes"):
            monitor.validate_config(config)

    def test_main_requires_an_explicit_action_before_any_side_effect(self) -> None:
        with (
            mock.patch.object(monitor.time, "sleep") as sleep,
            mock.patch.object(monitor, "load_config") as load_config,
            mock.patch.object(monitor, "exclusive_lock") as exclusive_lock,
            mock.patch.object(monitor, "managed_dedicated_browser") as browser,
            mock.patch.object(monitor, "run_once") as run_once,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as caught:
                monitor.main([])
        self.assertEqual(caught.exception.code, 2)
        sleep.assert_not_called()
        load_config.assert_not_called()
        exclusive_lock.assert_not_called()
        browser.assert_not_called()
        run_once.assert_not_called()

    def test_no_jitter_alone_is_not_an_action(self) -> None:
        with (
            mock.patch.object(monitor.time, "sleep") as sleep,
            mock.patch.object(monitor, "load_config") as load_config,
            mock.patch.object(monitor, "run_once") as run_once,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as caught:
                monitor.main(["--no-jitter"])
        self.assertEqual(caught.exception.code, 2)
        sleep.assert_not_called()
        load_config.assert_not_called()
        run_once.assert_not_called()

    def test_main_run_once_dispatches_exactly_one_scan(self) -> None:
        config = {"test": True}
        result = {"failures": []}
        with (
            mock.patch.object(monitor.time, "sleep") as sleep,
            mock.patch.object(monitor, "load_config", return_value=config),
            mock.patch.object(
                monitor, "exclusive_lock", return_value=contextlib.nullcontext()
            ),
            mock.patch.object(
                monitor,
                "managed_dedicated_browser",
                return_value=contextlib.nullcontext(),
            ) as browser,
            mock.patch.object(monitor, "run_once", return_value=result) as run_once,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            monitor.main(["--run-once", "--no-jitter"])
        sleep.assert_not_called()
        browser.assert_called_once_with(config)
        run_once.assert_called_once_with(
            force_baseline=False,
            no_jitter=True,
            lock_held=True,
        )

    def test_main_baseline_only_dispatches_a_forced_baseline(self) -> None:
        config = {"test": True}
        with (
            mock.patch.object(monitor, "load_config", return_value=config),
            mock.patch.object(
                monitor, "exclusive_lock", return_value=contextlib.nullcontext()
            ),
            mock.patch.object(
                monitor,
                "managed_dedicated_browser",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                monitor, "run_once", return_value={"failures": []}
            ) as run_once,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            monitor.main(["--baseline-only", "--no-jitter"])
        run_once.assert_called_once_with(
            force_baseline=True,
            no_jitter=True,
            lock_held=True,
        )

    def test_main_rejects_conflicting_actions(self) -> None:
        with (
            mock.patch.object(monitor, "telegram_test") as telegram_test,
            mock.patch.object(monitor, "run_once") as run_once,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as caught:
                monitor.main(["--run-once", "--telegram-test"])
        self.assertEqual(caught.exception.code, 2)
        telegram_test.assert_not_called()
        run_once.assert_not_called()

    def test_search_url_uses_warm_rent(self) -> None:
        url = monitor.search_url("berlin", 2000)
        self.assertIn("pricetype=calculatedtotalrent", url)
        self.assertIn("price=-2000.0", url)
        self.assertIn("numberofrooms=3.0-4.0", url)
        self.assertNotIn("pagenumber", url)

    def test_search_url_supports_real_second_page(self) -> None:
        url = monitor.search_url("berlin", 2000, page_number=2)
        self.assertIn("pagenumber=2", url)
        self.assertEqual(monitor.page_number_from_url(url), 2)

    def test_detached_house_url_uses_cold_rent_prefilter(self) -> None:
        url = monitor.search_url(
            "potsdam-mittelmark-kreis",
            2000,
            listing_path="einfamilienhaus-mieten",
            price_type="rentpermonth",
        )
        self.assertIn("/einfamilienhaus-mieten?", url)
        self.assertIn("pricetype=rentpermonth", url)
        self.assertNotIn("calculatedtotalrent", url)
        self.assertTrue(
            monitor.search_scope_is_preserved(
                url,
                listing_path="einfamilienhaus-mieten",
                price_type="rentpermonth",
                max_rent=2000,
                rooms_min=3.0,
                rooms_max=4.0,
            )
        )

    def test_property_searches_are_distinct_and_detached_is_scanned_first(self) -> None:
        config = self.config()
        config["searches"] = [
            {"name": "Potsdam-Mittelmark", "slug": "potsdam-mittelmark-kreis"}
        ]
        config["property_searches"] = [
            {
                "property_kind": "apartment",
                "property_label": "公寓",
                "listing_path": "wohnung-mieten",
                "price_type": "calculatedtotalrent",
                "priority": 0,
            },
            {
                "property_kind": "detached_house",
                "property_label": "独栋预选",
                "listing_path": "einfamilienhaus-mieten",
                "price_type": "rentpermonth",
                "priority": 100,
            },
        ]
        searches = monitor.configured_searches(config)
        self.assertEqual(searches[0]["property_kind"], "detached_house")
        identities = {
            monitor.search_identity(
                config,
                item["slug"],
                item["listing_path"],
                item["price_type"],
            )
            for item in searches
        }
        self.assertEqual(len(identities), 2)

    def test_first_page_is_limited_to_declared_result_total(self) -> None:
        listings = [self.sample_listing(1), self.sample_listing(2)]
        self.assertEqual(
            monitor.constrain_listings_to_result_total(listings, 0, 1), []
        )
        self.assertEqual(
            monitor.constrain_listings_to_result_total(listings, 1, 1),
            listings[:1],
        )
        self.assertEqual(
            monitor.constrain_listings_to_result_total(listings, 0, 2), listings
        )

    def test_parse_listing(self) -> None:
        item = monitor.parse_listing(
            "Berlin Stadt",
            {
                "id": "169832586",
                "href": "/expose/169832586",
                "title": "Wohnen mit Charakter",
                "text": "Wohnen mit Charakter 1.550 € 86 m² 3 Zi. Schwemmstraße 12, Berlin Zum Merkzettel hinzufügen",
                "image": "https://example.invalid/a.jpg",
            },
        )
        self.assertEqual(item.warm_rent_eur, 1550.0)
        self.assertEqual(item.area_m2, 86.0)
        self.assertEqual(item.rooms, 3.0)
        self.assertEqual(item.address, "Schwemmstraße 12, Berlin")
        self.assertTrue(item.url.endswith("/expose/169832586"))

    def test_detached_list_price_is_never_labeled_as_warm_rent(self) -> None:
        item = monitor.parse_listing(
            "独栋预选 · Potsdam-Mittelmark",
            {
                "id": "161495304",
                "href": "/expose/161495304",
                "title": "Preiswertes Einfamilienhaus",
                "text": "Preiswertes Einfamilienhaus 1.890 € 122 m² 4 Zi. 980 m² Erdweg",
            },
            property_kind="detached_house",
            priority=100,
            price_type="rentpermonth",
        )
        self.assertIsNone(item.warm_rent_eur)
        self.assertEqual(item.cold_rent_eur, 1890.0)
        self.assertFalse(item.warm_rent_verified)
        self.assertEqual(item.address, "")
        self.assertFalse(monitor.warm_rent_within_cap(item, 2000))
        self.assertTrue(
            monitor.listing_passes_coarse_filter(
                item, max_warm_rent=2000, rooms_min=3.0, rooms_max=4.0
            )
        )

    def test_warm_rent_hard_cap_has_no_over_budget_tolerance(self) -> None:
        sample = self.sample_listing(1)
        at_cap = monitor.Listing(
            **{**sample.__dict__, "warm_rent_eur": 2000.0}
        )
        over_cap = monitor.Listing(
            **{**at_cap.__dict__, "listing_id": "2", "warm_rent_eur": 2000.01}
        )
        self.assertTrue(monitor.warm_rent_within_cap(at_cap, 2000))
        self.assertFalse(monitor.warm_rent_within_cap(over_cap, 2000))

    def test_official_mail_known_over_cap_is_always_rejected(self) -> None:
        sample = self.sample_listing(1)
        listing = monitor.Listing(
            **{
                **sample.__dict__,
                "platform": "immowelt",
                "warm_rent_eur": 2000.01,
                "warm_rent_verified": False,
                "discovery_method": "official_saved_search_email",
            }
        )
        config = self.config()
        config["official_mail_sources"] = {
            "verified_saved_search_platforms": ["immowelt"]
        }
        self.assertFalse(monitor.listing_is_notification_eligible(listing, config))

    def test_official_mail_trust_is_scoped_to_verified_platforms(self) -> None:
        sample = self.sample_listing(1)
        listing = monitor.Listing(
            **{
                **sample.__dict__,
                "platform": "immowelt",
                "warm_rent_eur": None,
                "warm_rent_verified": False,
                "discovery_method": "official_saved_search_email",
            }
        )
        config = self.config()
        config["official_mail_sources"] = {
            "verified_saved_search_platforms": ["immowelt"],
            "saved_search_filters_verified": True,
        }
        self.assertTrue(monitor.listing_is_notification_eligible(listing, config))
        other_platform = monitor.Listing(
            **{**listing.__dict__, "platform": "wggesucht", "listing_id": "2"}
        )
        self.assertFalse(
            monitor.listing_is_notification_eligible(other_platform, config)
        )

    def test_verified_saved_search_can_supply_missing_rooms_but_not_override_known_mismatch(self) -> None:
        sample = self.sample_listing(1)
        listing = monitor.Listing(
            **{
                **sample.__dict__,
                "platform": "immowelt",
                "rooms": None,
                "warm_rent_eur": None,
                "warm_rent_verified": False,
                "discovery_method": "official_saved_search_email",
            }
        )
        config = self.config()
        config["official_mail_sources"] = {
            "verified_saved_search_platforms": ["immowelt"]
        }
        self.assertTrue(monitor.listing_is_notification_eligible(listing, config))
        known_mismatch = monitor.Listing(**{**listing.__dict__, "rooms": 2.0})
        self.assertFalse(
            monitor.listing_is_notification_eligible(known_mismatch, config)
        )

    def test_official_mail_legacy_boolean_is_compatible_but_defaults_closed(self) -> None:
        sample = self.sample_listing(1)
        listing = monitor.Listing(
            **{
                **sample.__dict__,
                "platform": "wggesucht",
                "warm_rent_eur": None,
                "warm_rent_verified": False,
                "discovery_method": "official_saved_search_email",
            }
        )
        default_config = self.config()
        self.assertFalse(
            monitor.listing_is_notification_eligible(listing, default_config)
        )
        legacy_config = self.config()
        legacy_config["official_mail_sources"] = {
            "saved_search_filters_verified": True
        }
        self.assertTrue(
            monitor.listing_is_notification_eligible(listing, legacy_config)
        )
        malformed_new_config = self.config()
        malformed_new_config["official_mail_sources"] = {
            "verified_saved_search_platforms": "immowelt",
            "saved_search_filters_verified": True,
        }
        self.assertFalse(
            monitor.listing_is_notification_eligible(listing, malformed_new_config)
        )

    def test_extract_nested_eval_result(self) -> None:
        value = monitor.extract_result_value({"result": '[{"id":"1"}]'})
        self.assertEqual(value, [{"id": "1"}])

    def test_cdp_is_loopback_only(self) -> None:
        self.assertEqual(monitor.CDP_PORT, 9229)

    def test_launchd_node_path_is_explicit(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', source)

    def test_browser_launch_agent_is_not_persistent(self) -> None:
        path = MODULE_PATH.with_name("ai.housing-browser.plist")
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        self.assertFalse(payload["RunAtLoad"])
        self.assertFalse(payload["KeepAlive"])

    def test_monitor_allows_browser_cleanup_before_forced_exit(self) -> None:
        path = MODULE_PATH.with_name("ai.housing-monitor.plist")
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        self.assertGreaterEqual(payload["ExitTimeOut"], 15)
        self.assertEqual(payload["ProgramArguments"].count("--run-once"), 1)

    def test_browser_start_refuses_an_existing_cdp_endpoint(self) -> None:
        with (
            mock.patch.object(monitor, "browser_is_ready", return_value=True),
            mock.patch.object(monitor.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(monitor.MonitorError, "Refusing to reuse"):
                monitor.start_dedicated_browser()
        popen.assert_not_called()

    def test_trim_browser_tabs_keeps_active_tab(self) -> None:
        calls: list[list[str]] = []

        def fake_command(args: list[str], timeout: int = 45):
            del timeout
            calls.append(args)
            if args == ["tab", "list"]:
                return {
                    "tabs": [
                        {"tabId": "t1", "active": False},
                        {"tabId": "t2", "active": True},
                        {"tabId": "t3", "active": False},
                    ]
                }
            return {}

        with mock.patch.object(monitor, "browser_command", side_effect=fake_command):
            self.assertEqual(monitor.trim_browser_tabs(), 2)

        self.assertEqual(
            calls,
            [
                ["tab", "list"],
                ["tab", "close", "t1"],
                ["tab", "close", "t3"],
            ],
        )

    def test_managed_browser_always_stops_the_process_it_started(self) -> None:
        process = mock.Mock()
        config = {"catch_up": {"browser_startup_wait_seconds": 17}}
        with (
            mock.patch.object(monitor, "start_dedicated_browser", return_value=process),
            mock.patch.object(monitor, "wait_for_browser_ready") as wait_ready,
            mock.patch.object(monitor, "trim_browser_tabs", return_value=4) as trim,
            mock.patch.object(monitor, "stop_dedicated_browser") as stop,
        ):
            with self.assertRaisesRegex(RuntimeError, "scan failed"):
                with monitor.managed_dedicated_browser(config) as details:
                    self.assertEqual(
                        details, {"started_here": True, "tabs_closed": 4}
                    )
                    raise RuntimeError("scan failed")

        wait_ready.assert_called_once_with(17.0)
        trim.assert_called_once_with()
        stop.assert_called_once_with(process)

    def test_browser_stop_escalates_only_its_own_process_group(self) -> None:
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [
            monitor.subprocess.TimeoutExpired(cmd="chrome", timeout=1),
            None,
        ]

        def fake_killpg(group: int, requested_signal: int) -> None:
            self.assertEqual(group, 4242)
            if requested_signal == 0:
                raise ProcessLookupError

        with (
            mock.patch.object(monitor.os, "getpgid", return_value=4242),
            mock.patch.object(monitor.os, "killpg", side_effect=fake_killpg) as killpg,
            mock.patch.object(monitor, "browser_is_ready", return_value=False),
        ):
            monitor.stop_dedicated_browser(process, timeout_seconds=1)

        requested = [call.args[1] for call in killpg.call_args_list]
        self.assertEqual(requested, [monitor.signal.SIGTERM, monitor.signal.SIGKILL, 0])

    def test_termination_signal_unwinds_python(self) -> None:
        with mock.patch.object(monitor.signal, "signal") as register:
            with self.assertRaises(SystemExit) as raised:
                monitor.terminate_gracefully(monitor.signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 128 + monitor.signal.SIGTERM)
        register.assert_called_once_with(monitor.signal.SIGTERM, monitor.signal.SIG_IGN)

    def test_fetch_retry_recovers_once(self) -> None:
        original = monitor.fetch_listings
        original_sleep = monitor.time.sleep
        calls = []

        def flaky(name: str, slug: str):
            calls.append((name, slug))
            if len(calls) == 1:
                raise monitor.MonitorError("transient")
            return ["ok"]

        monitor.fetch_listings = flaky
        monitor.time.sleep = lambda _seconds: None
        try:
            self.assertEqual(
                monitor.fetch_listings_with_retry("Berlin", "berlin"), ["ok"]
            )
        finally:
            monitor.fetch_listings = original
            monitor.time.sleep = original_sleep
        self.assertEqual(len(calls), 2)

    def test_listing_text_is_plain_and_contains_all_links(self) -> None:
        text = monitor.format_listing(self.sample_listing(), self.config())
        self.assertNotIn("<b>", text)
        self.assertIn("房源：https://example.invalid/expose/900000001", text)
        self.assertIn("通勤路线：https://www.google.com/maps/dir/", text)
        self.assertIn(
            "周边设施（Supermarkt）：https://www.google.com/maps/search/", text
        )

    def test_new_listing_is_durably_queued_for_both_channels(self) -> None:
        database = self.database()
        try:
            items = monitor.upsert_listings(
                database, [self.sample_listing()], baseline=False
            )
            self.assertEqual(len(items), 1)
            self.assertEqual(
                monitor.queue_unnotified_listings(database, self.config()), 1
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
                1,
            )
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM telegram_deliveries"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(monitor.create_email_batches(database, self.config()), 1)
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM email_batch_items").fetchone()[0],
                1,
            )
            self.assertEqual(
                database.execute("SELECT notified FROM seen").fetchone()[0], 1
            )
        finally:
            database.close()

    def test_only_warm_verified_detached_house_receives_priority(self) -> None:
        database = self.database()
        apartment = self.sample_listing(1)
        verified_house = monitor.Listing(
            **{
                **self.sample_listing(2).__dict__,
                "property_kind": "detached_house",
                "priority": 100,
                "cold_rent_eur": 1750.0,
                "warm_rent_eur": 1950.0,
                "warm_rent_verified": True,
            }
        )
        unknown_house = monitor.Listing(
            **{
                **self.sample_listing(3).__dict__,
                "property_kind": "detached_house",
                "priority": 100,
                "cold_rent_eur": 1750.0,
                "warm_rent_eur": None,
                "warm_rent_verified": False,
            }
        )
        try:
            monitor.upsert_listings(
                database,
                [apartment, unknown_house, verified_house],
                baseline=False,
            )
            monitor.queue_unnotified_listings(database, self.config())
            texts = [
                str(row[0])
                for row in database.execute(
                    "SELECT canonical_text FROM notifications ORDER BY notification_id"
                ).fetchall()
            ]
            self.assertEqual(len(texts), 2)
            self.assertIn("独栋候选（优先）", texts[0])
            self.assertIn("新公寓候选", texts[1])
            unknown_status = database.execute(
                "SELECT notified FROM seen WHERE listing_key = ?",
                (unknown_house.key,),
            ).fetchone()
            self.assertEqual(unknown_status, (0,))
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM notifications WHERE listing_key = ?",
                    (unknown_house.key,),
                ).fetchone()[0],
                0,
            )
        finally:
            database.close()

    def test_reboot_catch_up_walks_until_known_search_boundary(self) -> None:
        database = self.database()
        config = self.config()
        search_key = monitor.search_identity(config, "berlin")
        try:
            old_items = [
                self.sample_listing(90),
                self.sample_listing(91),
                self.sample_listing(92),
                self.sample_listing(93),
            ]
            monitor.upsert_listings(database, old_items, baseline=True)
            monitor.record_search_memberships(database, search_key, old_items)
            pages = {
                1: [self.sample_listing(1), self.sample_listing(2)],
                2: [self.sample_listing(3), self.sample_listing(4)],
                3: old_items[:2],
                4: old_items[2:],
            }

            def fake_page(_name: str, _slug: str, page_number: int = 1, attempts: int = 2, **_options):
                del attempts
                return monitor.SearchPage(
                    listings=pages[page_number],
                    requested_page=page_number,
                    final_url=monitor.search_url("berlin", page_number=page_number),
                    total_results=20,
                )

            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=fake_page
            ):
                result = monitor.scan_search(
                    database,
                    "Berlin Stadt",
                    "berlin",
                    search_key=search_key,
                    deep_scan=True,
                    baseline=False,
                    max_pages=5,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                )

            self.assertTrue(result.complete)
            self.assertEqual(result.pages_fetched, 4)
            self.assertEqual(len(result.new_items), 4)
            self.assertEqual(
                monitor.queue_unnotified_listings(database, self.config()), 4
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
                4,
            )
        finally:
            database.close()

    def test_reboot_catch_up_keeps_checkpoint_open_after_page_failure(self) -> None:
        database = self.database()
        config = self.config()
        try:
            def fake_page(_name: str, _slug: str, page_number: int = 1, attempts: int = 2, **_options):
                del attempts
                if page_number == 2:
                    raise monitor.MonitorError("temporary page failure")
                return monitor.SearchPage(
                    listings=[self.sample_listing(1)],
                    requested_page=page_number,
                    final_url=monitor.search_url("berlin", page_number=page_number),
                    total_results=40,
                )

            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=fake_page
            ):
                result = monitor.scan_search(
                    database,
                    "Berlin Stadt",
                    "berlin",
                    search_key=monitor.search_identity(config, "berlin"),
                    deep_scan=True,
                    baseline=False,
                    max_pages=5,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                )

            self.assertFalse(result.complete)
            self.assertIn("page 2", result.error)
            self.assertEqual(len(result.new_items), 1)
        finally:
            database.close()

    def test_current_scan_rows_cannot_fake_historical_boundary(self) -> None:
        database = self.database()
        config = self.config()
        search_key = monitor.search_identity(config, "berlin")
        try:
            historical = [
                self.sample_listing(90),
                self.sample_listing(91),
                self.sample_listing(92),
                self.sample_listing(93),
            ]
            monitor.upsert_listings(database, historical, baseline=True)
            monitor.record_search_memberships(database, search_key, historical)
            pages = {
                1: [self.sample_listing(1), self.sample_listing(2)],
                2: [self.sample_listing(1)],
                3: historical[:2],
                4: historical[2:],
            }

            def fake_page(_name: str, _slug: str, page_number: int = 1, attempts: int = 2, **_options):
                del attempts
                return monitor.SearchPage(
                    listings=pages[page_number],
                    requested_page=page_number,
                    final_url=monitor.search_url("berlin", page_number=page_number),
                    total_results=20,
                )

            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=fake_page
            ):
                result = monitor.scan_search(
                    database,
                    "Berlin Stadt",
                    "berlin",
                    search_key=search_key,
                    deep_scan=True,
                    baseline=False,
                    max_pages=5,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                )

            self.assertTrue(result.complete)
            self.assertEqual(result.pages_fetched, 4)
            self.assertEqual(len(result.new_items), 2)
        finally:
            database.close()

    def test_failed_scan_membership_does_not_pollute_restart_boundary(self) -> None:
        database = self.database()
        config = self.config()
        search_key = monitor.search_identity(config, "berlin")
        historical = [
            self.sample_listing(90),
            self.sample_listing(91),
            self.sample_listing(92),
            self.sample_listing(93),
        ]
        try:
            monitor.upsert_listings(database, historical, baseline=True)
            monitor.record_search_memberships(database, search_key, historical)

            def failing_page(_name: str, _slug: str, page_number: int = 1, attempts: int = 2, **_options):
                del attempts
                if page_number == 3:
                    raise monitor.MonitorError("page three failed")
                listings = {
                    1: [self.sample_listing(1), self.sample_listing(2)],
                    2: [self.sample_listing(3), self.sample_listing(4)],
                }[page_number]
                return monitor.SearchPage(
                    listings=listings,
                    requested_page=page_number,
                    final_url=monitor.search_url("berlin", page_number=page_number),
                    total_results=20,
                )

            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=failing_page
            ):
                first = monitor.scan_search(
                    database,
                    "Berlin Stadt",
                    "berlin",
                    search_key=search_key,
                    deep_scan=True,
                    baseline=False,
                    max_pages=8,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                )
            self.assertFalse(first.complete)
            stored_after_failure = database.execute(
                "SELECT listing_key FROM search_seen WHERE search_key = ?",
                (search_key,),
            ).fetchall()
            self.assertEqual(len(stored_after_failure), 4)

            pages = {
                1: [self.sample_listing(1), self.sample_listing(2)],
                2: [self.sample_listing(3), self.sample_listing(4)],
                3: [self.sample_listing(5), self.sample_listing(6)],
                4: historical[:2],
                5: historical[2:],
            }

            def recovered_page(_name: str, _slug: str, page_number: int = 1, attempts: int = 2, **_options):
                del attempts
                return monitor.SearchPage(
                    listings=pages[page_number],
                    requested_page=page_number,
                    final_url=monitor.search_url("berlin", page_number=page_number),
                    total_results=20,
                )

            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=recovered_page
            ):
                second = monitor.scan_search(
                    database,
                    "Berlin Stadt",
                    "berlin",
                    search_key=search_key,
                    deep_scan=True,
                    baseline=False,
                    max_pages=8,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                )
            self.assertTrue(second.complete)
            self.assertEqual(second.pages_fetched, 5)
            self.assertTrue(
                database.execute(
                    "SELECT 1 FROM search_seen WHERE search_key = ? AND listing_key = ?",
                    (search_key, self.sample_listing(6).key),
                ).fetchone()
            )
        finally:
            database.close()

    def test_unexpected_empty_followup_page_is_incomplete(self) -> None:
        database = self.database()
        config = self.config()
        try:
            def fake_page(_name: str, _slug: str, page_number: int = 1, attempts: int = 2, **_options):
                del attempts
                listings = [self.sample_listing(1), self.sample_listing(2)] if page_number == 1 else []
                return monitor.SearchPage(
                    listings=listings,
                    requested_page=page_number,
                    final_url=monitor.search_url("berlin", page_number=page_number),
                    total_results=10,
                )

            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=fake_page
            ):
                result = monitor.scan_search(
                    database,
                    "Berlin Stadt",
                    "berlin",
                    search_key=monitor.search_identity(config, "berlin"),
                    deep_scan=True,
                    baseline=False,
                    max_pages=5,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                )
            self.assertFalse(result.complete)
            self.assertIn("unexpected empty page", result.error)
        finally:
            database.close()

    def test_explicit_zero_result_search_is_complete(self) -> None:
        database = self.database()
        config = self.config()

        def zero_page(
            _name: str,
            _slug: str,
            page_number: int = 1,
            attempts: int = 2,
            **_options,
        ):
            del attempts
            return monitor.SearchPage(
                listings=[],
                requested_page=page_number,
                final_url=monitor.search_url(
                    "berlin",
                    listing_path="einfamilienhaus-mieten",
                    price_type="rentpermonth",
                ),
                total_results=0,
            )

        try:
            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=zero_page
            ):
                result = monitor.scan_search(
                    database,
                    "独栋预选 · Berlin Stadt",
                    "berlin",
                    search_key=monitor.search_identity(
                        config,
                        "berlin",
                        "einfamilienhaus-mieten",
                        "rentpermonth",
                    ),
                    deep_scan=True,
                    baseline=True,
                    max_pages=5,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                    listing_path="einfamilienhaus-mieten",
                    price_type="rentpermonth",
                    property_kind="detached_house",
                    priority=100,
                )
            self.assertTrue(result.complete)
            self.assertEqual(result.pages_fetched, 1)
            self.assertEqual(result.listings_fetched, 0)
        finally:
            database.close()

    def test_failed_silent_baseline_writes_nothing(self) -> None:
        database = self.database()
        config = self.config()
        try:
            def fake_page(_name: str, _slug: str, page_number: int = 1, attempts: int = 2, **_options):
                del attempts
                if page_number == 2:
                    raise monitor.MonitorError("baseline page failed")
                return monitor.SearchPage(
                    listings=[self.sample_listing(1), self.sample_listing(2)],
                    requested_page=page_number,
                    final_url=monitor.search_url("berlin", page_number=page_number),
                    total_results=10,
                )

            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=fake_page
            ):
                result = monitor.scan_search(
                    database,
                    "Berlin Stadt",
                    "berlin",
                    search_key=monitor.search_identity(config, "berlin"),
                    deep_scan=True,
                    baseline=True,
                    max_pages=5,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                )
            self.assertFalse(result.complete)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 0)
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM search_seen").fetchone()[0],
                0,
            )
        finally:
            database.close()

    def test_short_baseline_page_cannot_override_known_total(self) -> None:
        database = self.database()
        config = self.config()
        try:
            first_page = [self.sample_listing(index) for index in range(1, 21)]
            short_page = [self.sample_listing(index) for index in range(21, 31)]
            pages = {1: first_page, 2: short_page}

            def fake_page(_name: str, _slug: str, page_number: int = 1, attempts: int = 2, **_options):
                del attempts
                return monitor.SearchPage(
                    listings=pages[page_number],
                    requested_page=page_number,
                    final_url=monitor.search_url("berlin", page_number=page_number),
                    total_results=100,
                )

            with mock.patch.object(
                monitor, "fetch_search_page_with_retry", side_effect=fake_page
            ):
                result = monitor.scan_search(
                    database,
                    "Berlin Stadt",
                    "berlin",
                    search_key=monitor.search_identity(config, "berlin"),
                    deep_scan=True,
                    baseline=True,
                    max_pages=2,
                    minimum_pages=2,
                    known_boundary_pages=2,
                    page_delay_seconds=0,
                )
            self.assertFalse(result.complete)
            self.assertIn("safety limit", result.error)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 0)
        finally:
            database.close()

    def test_catch_up_triggers_only_after_baseline_and_gap(self) -> None:
        database = self.database()
        config = self.config()
        config["poll_interval_seconds"] = 900
        config["searches"] = [{"name": "Berlin", "slug": "berlin"}]
        config["catch_up"] = {"enabled": True, "trigger_gap_seconds": 1800}
        now = dt.datetime(2026, 8, 6, 20, 0, tzinfo=dt.timezone.utc)
        try:
            monitor.set_meta(database, "last_full_success_at", (now - dt.timedelta(hours=2)).isoformat())
            self.assertEqual(monitor.catch_up_status(database, config, now), (False, None))
            monitor.set_meta(database, "catch_up_baseline_complete", "1")
            monitor.set_meta(
                database,
                "catch_up_baseline_fingerprint",
                monitor.catch_up_baseline_fingerprint(config),
            )
            due, gap = monitor.catch_up_status(database, config, now)
            self.assertTrue(due)
            self.assertEqual(gap, 7200)
        finally:
            database.close()

    def test_search_change_invalidates_catch_up_baseline(self) -> None:
        database = self.database()
        config = self.config()
        config["searches"] = [{"name": "Berlin", "slug": "berlin"}]
        try:
            monitor.set_meta(database, "catch_up_baseline_complete", "1")
            monitor.set_meta(
                database,
                "catch_up_baseline_fingerprint",
                monitor.catch_up_baseline_fingerprint(config),
            )
            self.assertTrue(monitor.catch_up_baseline_is_current(database, config))
            changed = {**config, "warm_rent_target_eur": 1999}
            self.assertFalse(monitor.catch_up_baseline_is_current(database, changed))
        finally:
            database.close()

    def test_baseline_rows_are_never_backfilled_to_notifications(self) -> None:
        database = self.database()
        try:
            monitor.upsert_listings(database, [self.sample_listing()], baseline=True)
            self.assertEqual(
                monitor.queue_unnotified_listings(database, self.config()), 0
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
                0,
            )
        finally:
            database.close()

    def test_twenty_listings_create_immutable_fifteen_plus_five_batches(self) -> None:
        database = self.database()
        try:
            listings = [self.sample_listing(index) for index in range(1, 21)]
            monitor.upsert_listings(database, listings, baseline=False)
            self.assertEqual(
                monitor.queue_unnotified_listings(database, self.config()), 20
            )
            self.assertEqual(
                monitor.create_email_batches(database, self.config(batch_size=15)), 2
            )
            sizes = database.execute(
                "SELECT COUNT(*) FROM email_batch_items GROUP BY batch_id ORDER BY batch_id"
            ).fetchall()
            self.assertEqual(sizes, [(15,), (5,)])
            ids = database.execute(
                "SELECT rfc_message_id FROM email_batches ORDER BY batch_id"
            ).fetchall()
            self.assertEqual(len({row[0] for row in ids}), 2)
        finally:
            database.close()

    def test_email_failure_does_not_roll_back_telegram_success(self) -> None:
        database = self.database()
        config = self.config(email_enabled=True)
        try:
            monitor.upsert_listings(database, [self.sample_listing()], baseline=False)
            monitor.queue_unnotified_listings(database, config)
            monitor.create_email_batches(database, config)

            telegram_response = {"ok": True, "result": {"message_id": 42}}
            with mock.patch.object(
                monitor, "send_telegram", return_value=telegram_response
            ):
                telegram = monitor.drain_telegram_outbox(database, limit=15)

            class FailingNotifier:
                def send(self, **_kwargs):
                    raise monitor.EmailDeliveryError(
                        "smtp_connection_failed", retryable=True
                    )

            with mock.patch.object(
                monitor, "build_email_notifier", return_value=FailingNotifier()
            ):
                email = monitor.drain_email_outbox(database, config)

            self.assertEqual(telegram, {"sent": 1, "retry": 0})
            self.assertEqual(email["retry"], 1)
            self.assertEqual(
                database.execute(
                    "SELECT status FROM telegram_deliveries"
                ).fetchone()[0],
                "sent",
            )
            self.assertEqual(
                database.execute("SELECT status FROM email_batches").fetchone()[0],
                "retry",
            )
        finally:
            database.close()

    def test_disabled_telegram_keeps_delivery_pending(self) -> None:
        database = self.database()
        config = self.config()
        config["telegram"]["enabled"] = False
        try:
            monitor.upsert_listings(database, [self.sample_listing()], baseline=False)
            monitor.queue_unnotified_listings(database, config)
            with mock.patch.object(monitor, "send_telegram") as sender:
                result = monitor.drain_telegram_outbox(
                    database, limit=15, config=config
                )
            self.assertEqual(result, {"sent": 0, "retry": 0})
            sender.assert_not_called()
            self.assertEqual(
                database.execute(
                    "SELECT status FROM telegram_deliveries"
                ).fetchone()[0],
                "pending",
            )
        finally:
            database.close()

    def test_disabled_email_keeps_batch_pending(self) -> None:
        database = self.database()
        config = self.config(email_enabled=False)
        try:
            monitor.upsert_listings(database, [self.sample_listing()], baseline=False)
            monitor.queue_unnotified_listings(database, config)
            monitor.create_email_batches(database, config)
            result = monitor.drain_email_outbox(database, config)
            self.assertEqual(result["error"], "disabled")
            self.assertEqual(
                database.execute("SELECT status FROM email_batches").fetchone()[0],
                "pending",
            )
        finally:
            database.close()

    def test_email_queue_can_survive_long_computer_shutdown(self) -> None:
        database = self.database()
        config = self.config(email_enabled=True)
        config["email"]["max_age_hours"] = 0
        try:
            monitor.upsert_listings(database, [self.sample_listing()], baseline=False)
            monitor.queue_unnotified_listings(database, config)
            monitor.create_email_batches(database, config)
            database.execute(
                "UPDATE email_batches SET created_at = ?, next_attempt_at = ?",
                ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            database.commit()

            class SuccessfulNotifier:
                def send(self, **_kwargs):
                    return None

            with mock.patch.object(
                monitor, "build_email_notifier", return_value=SuccessfulNotifier()
            ):
                result = monitor.drain_email_outbox(database, config)
            self.assertEqual(result["sent"], 1)
            self.assertEqual(result["blocked"], 0)
        finally:
            database.close()

    def test_official_mail_normal_read_uses_configured_window_without_checkpoint(
        self,
    ) -> None:
        database = self.database()
        config = self.config()
        config["official_mail_sources"] = {
            "enabled": True,
            "lookback_days": 7,
            "max_messages": 200,
        }
        try:
            limits = monitor.official_mail_read_limits(
                database,
                config,
                now=dt.datetime(2026, 8, 7, 20, 0, tzinfo=dt.timezone.utc),
            )
            self.assertEqual(limits, (7, 200))
        finally:
            database.close()

    def test_official_mail_read_expands_after_offline_gap(self) -> None:
        database = self.database()
        config = self.config()
        config["official_mail_sources"] = {
            "enabled": True,
            "lookback_days": 7,
            "max_messages": 200,
        }
        now = dt.datetime(2026, 8, 28, 21, 0, tzinfo=dt.timezone.utc)
        try:
            monitor.set_meta(
                database,
                monitor.OFFICIAL_MAIL_LAST_SUCCESS_META_KEY,
                (now - dt.timedelta(days=20, hours=1)).isoformat(),
            )
            self.assertEqual(
                monitor.official_mail_read_limits(database, config, now=now),
                (21, 600),
            )
        finally:
            database.close()

    def test_official_mail_catch_up_is_bounded_to_ninety_days_and_two_thousand(
        self,
    ) -> None:
        database = self.database()
        config = self.config()
        config["official_mail_sources"] = {
            "enabled": True,
            "lookback_days": 7,
            "max_messages": 200,
        }
        now = dt.datetime(2026, 8, 28, 21, 0, tzinfo=dt.timezone.utc)
        try:
            monitor.set_meta(
                database,
                monitor.OFFICIAL_MAIL_LAST_SUCCESS_META_KEY,
                (now - dt.timedelta(days=365)).isoformat(),
            )
            self.assertEqual(
                monitor.official_mail_read_limits(database, config, now=now),
                (90, 2000),
            )
        finally:
            database.close()

    def test_official_mail_success_updates_checkpoint_and_uses_catch_up_limits(
        self,
    ) -> None:
        database = self.database()
        config = self.config()
        config["official_mail_sources"] = {
            "enabled": True,
            "account_scope": "primary",
            "lookback_days": 7,
            "max_messages": 200,
            "timeout_seconds": 60,
        }
        now = dt.datetime(2026, 8, 28, 21, 0, tzinfo=dt.timezone.utc)
        try:
            monitor.set_meta(
                database,
                monitor.OFFICIAL_MAIL_LAST_SUCCESS_META_KEY,
                (now - dt.timedelta(days=20, hours=1)).isoformat(),
            )
            read_result = mock.Mock(messages=())
            with (
                mock.patch.object(monitor, "utc_now", return_value=now),
                mock.patch.object(
                    monitor, "read_recent_housing_mail", return_value=read_result
                ) as reader,
            ):
                result = monitor.ingest_official_mail_source(database, config)
            self.assertEqual(result["error"], "")
            reader.assert_called_once_with(
                lookback_days=21, max_messages=600, timeout_seconds=60
            )
            self.assertEqual(
                monitor.get_meta(
                    database, monitor.OFFICIAL_MAIL_LAST_SUCCESS_META_KEY
                ),
                now.isoformat(),
            )
        finally:
            database.close()

    def test_official_mail_read_failure_does_not_advance_checkpoint(self) -> None:
        database = self.database()
        config = self.config()
        config["official_mail_sources"] = {
            "enabled": True,
            "account_scope": "primary",
            "lookback_days": 7,
            "max_messages": 200,
            "timeout_seconds": 60,
        }
        previous = "2026-08-01T20:00:00+00:00"
        now = dt.datetime(2026, 8, 28, 21, 0, tzinfo=dt.timezone.utc)
        try:
            monitor.set_meta(
                database,
                monitor.OFFICIAL_MAIL_LAST_SUCCESS_META_KEY,
                previous,
            )
            with (
                mock.patch.object(monitor, "utc_now", return_value=now),
                mock.patch.object(
                    monitor,
                    "read_recent_housing_mail",
                    side_effect=monitor.AppleMailReadError("simulated read failure"),
                ),
            ):
                result = monitor.ingest_official_mail_source(database, config)
            self.assertEqual(result["error"], "simulated read failure")
            self.assertEqual(
                monitor.get_meta(
                    database, monitor.OFFICIAL_MAIL_LAST_SUCCESS_META_KEY
                ),
                previous,
            )
        finally:
            database.close()

    def test_current_backfill_is_email_only_and_idempotent(self) -> None:
        database = self.database()
        config = self.config(email_enabled=True)
        try:
            listings = [self.sample_listing(index) for index in range(1, 21)]
            monitor.upsert_listings(database, listings, baseline=True)
            first = monitor.queue_current_email_backfill(
                database, config, batch_size=10
            )
            self.assertEqual(first["selected"], 20)
            self.assertEqual(first["batches_created"], 2)
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM telegram_deliveries "
                    "WHERE status = 'skipped' AND last_error_class = 'backfill_email_only'"
                ).fetchone()[0],
                20,
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM email_batch_items").fetchone()[0],
                20,
            )
            second = monitor.queue_current_email_backfill(
                database, config, batch_size=10
            )
            self.assertEqual(second["selected"], 0)
            self.assertEqual(second["batches_created"], 0)
        finally:
            database.close()

    def test_reply_email_setup_failure_does_not_block_telegram(self) -> None:
        database = self.database()
        config = self.config(email_enabled=True)
        outcomes = [
            {"processed": True, "outcome": "sent"},
            {"processed": False, "reason": "nothing_pending"},
        ]
        try:
            with (
                mock.patch.object(
                    monitor,
                    "deliver_one_reply_notification",
                    side_effect=outcomes,
                ) as deliver,
                mock.patch.object(
                    monitor,
                    "build_email_notifier",
                    side_effect=monitor.EmailDeliveryError(
                        "apple_mail_unavailable", retryable=True
                    ),
                ),
            ):
                result = monitor.drain_reply_notification_outboxes(
                    database, config, limit_per_channel=2
                )
            self.assertEqual(result["telegram_sent"], 1)
            self.assertEqual(result["email_unavailable"], 1)
            self.assertEqual(
                [call.args[1] for call in deliver.call_args_list],
                ["telegram", "telegram"],
            )
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
