#!/usr/bin/env python3
"""Poll German housing search pages and send approval-gated notifications.

The monitor deliberately does not store ImmoScout credentials. A dedicated
browser profile retains only browser cookies after the user completes any
interactive challenge. Telegram and optional model secrets are read from the
process environment or an ignored local ``.env`` file. SMTP app passwords are
read from macOS Keychain. No secret is stored in the repository.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from notifiers import EmailDeliveryError, build_email_notifier
from application_workflow import canonical_listing_key, initialize_contact_database
from housing_pipeline import SourceListing, ingest_source_listing, initialize_pipeline_database
from contact_pipeline import DraftBatchResult, prepare_contact_drafts
from apple_mail_source import AppleMailReadError, read_recent_housing_mail
from contact_delivery import deliver_one_reply_notification
from mail_sources import ingest_mail_messages, promote_mail_listings_to_seen


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("HOUSING_MONITOR_CONFIG", str(ROOT / "config.json"))
).expanduser()
STATE_DIR = Path(
    os.environ.get("HOUSING_MONITOR_STATE_DIR", str(ROOT / "state"))
).expanduser()
PROFILE_DIR = Path(
    os.environ.get(
        "HOUSING_MONITOR_BROWSER_PROFILE_DIR", str(ROOT / "browser-profile")
    )
).expanduser()
DB_PATH = STATE_DIR / "seen.sqlite3"
LOCK_PATH = STATE_DIR / "monitor.lock"
LAST_RUN_PATH = STATE_DIR / "last_run.json"
BROWSER_OUT_LOG = STATE_DIR / "browser.out.log"
BROWSER_ERR_LOG = STATE_DIR / "browser.err.log"
SECRETS_ENV = Path(
    os.environ.get("HOUSING_MONITOR_ENV_FILE", str(ROOT / ".env"))
).expanduser()
AGENT_BROWSER_OVERRIDE = os.environ.get("AGENT_BROWSER_PATH", "").strip()
GOOGLE_CHROME = Path(
    os.environ.get(
        "HOUSING_MONITOR_CHROME_PATH",
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    )
).expanduser()
SESSION_NAME = "housing-cdp-live"
CDP_PORT = 9229
BERLIN = ZoneInfo("Europe/Berlin")
RETRY_DELAYS_MINUTES = (15, 30, 60, 180, 360)
SEARCH_RESULT_SCOPE_VERSION = 2
OFFICIAL_MAIL_PLATFORMS = frozenset(
    {"immoscout24", "wggesucht", "immowelt", "kleinanzeigen"}
)
OFFICIAL_MAIL_LAST_SUCCESS_META_KEY = "official_mail_last_success_at"
OFFICIAL_MAIL_MAX_LOOKBACK_DAYS = 90
OFFICIAL_MAIL_MAX_MESSAGES = 2000


class MonitorError(RuntimeError):
    pass


class BotChallenge(MonitorError):
    pass


@dataclass(frozen=True)
class Listing:
    source: str
    listing_id: str
    title: str
    raw_text: str
    url: str
    platform: str = "immoscout24"
    image_url: str = ""
    warm_rent_eur: float | None = None
    cold_rent_eur: float | None = None
    area_m2: float | None = None
    rooms: float | None = None
    address: str = ""
    property_kind: str = "apartment"
    priority: int = 0
    warm_rent_verified: bool = True
    discovery_method: str = "web_search"
    source_account_scope: str = "primary"

    @property
    def key(self) -> str:
        return canonical_listing_key(self.platform, self.listing_id)


@dataclass(frozen=True)
class SearchPage:
    listings: list[Listing]
    requested_page: int
    final_url: str
    total_results: int | None = None


@dataclass(frozen=True)
class SearchScanResult:
    listings_fetched: int
    pages_fetched: int
    new_items: list[Listing]
    complete: bool
    error: str = ""


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on incomplete or unsafe runtime configuration."""

    if not isinstance(config, dict):
        raise MonitorError("Configuration root must be a JSON object")
    try:
        rooms_min = float(config["rooms_min"])
        rooms_max = float(config["rooms_max"])
        rent_cap = float(config["warm_rent_target_eur"])
        move_in_from = dt.date.fromisoformat(str(config["move_in_from"]))
        move_in_to = dt.date.fromisoformat(str(config["move_in_to"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise MonitorError(
            "Configuration needs numeric room/rent limits and ISO move-in dates"
        ) from exc
    if rooms_min <= 0 or rooms_max < rooms_min:
        raise MonitorError("Room limits are invalid")
    if rent_cap <= 0:
        raise MonitorError("Warm-rent limit must be positive")
    if move_in_to < move_in_from:
        raise MonitorError("Move-in end date precedes the start date")
    if not str(config.get("commute_destination") or "").strip():
        raise MonitorError("A commute destination is required")
    try:
        max_commute_minutes = int(config["max_commute_minutes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MonitorError("A positive maximum commute time is required") from exc
    if max_commute_minutes <= 0:
        raise MonitorError("Maximum commute time must be positive")
    if not str(config.get("amenity_query") or "").strip():
        raise MonitorError("An amenity query is required")
    searches = config.get("searches")
    if not isinstance(searches, list) or not searches:
        raise MonitorError("At least one search region is required")
    for item in searches:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise MonitorError("Every search region needs a name")
        slug = str(item.get("slug") or "")
        if not re.fullmatch(r"[a-z0-9-]+", slug):
            raise MonitorError("Search slugs may contain only a-z, 0-9 and hyphens")
    contacts = dict(config.get("contacts") or {})
    if contacts.get("approval_mode", "per_listing_id") != "per_listing_id":
        raise MonitorError("Contacts must use per-listing approval")
    if contacts.get("real_send_enabled") is True and contacts.get("mode") != "live":
        raise MonitorError("Real sends require contacts.mode=live as a second gate")
    email = dict(config.get("email") or {})
    if email.get("enabled") is True and not (
        str(email.get("sender") or "").strip()
        and str(email.get("recipient") or "").strip()
    ):
        raise MonitorError("Enabled email notifications need sender and recipient")
    return config


def load_config(path: Path | None = None) -> dict[str, Any]:
    selected = path or CONFIG_PATH
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(
            "Missing config.json; copy examples/config.example.json first"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError("Configuration could not be read as JSON") from exc
    return validate_config(payload)


def configured_searches(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand each region into the configured property searches, highest priority first."""

    property_searches = list(config.get("property_searches") or [])
    if not property_searches:
        property_searches = [
            {
                "property_kind": "apartment",
                "property_label": "公寓",
                "listing_path": "wohnung-mieten",
                "priority": 0,
            }
        ]
    ordered_properties = sorted(
        property_searches,
        key=lambda item: int(item.get("priority", 0)),
        reverse=True,
    )
    expanded: list[dict[str, Any]] = []
    for property_search in ordered_properties:
        for region in config.get("searches") or []:
            expanded.append(
                {
                    "name": str(region["name"]),
                    "slug": str(region["slug"]),
                    "property_kind": str(
                        property_search.get("property_kind") or "apartment"
                    ),
                    "property_label": str(
                        property_search.get("property_label") or "公寓"
                    ),
                    "listing_path": str(
                        property_search.get("listing_path") or "wohnung-mieten"
                    ),
                    "price_type": str(
                        property_search.get("price_type")
                        or "calculatedtotalrent"
                    ),
                    "priority": int(property_search.get("priority", 0)),
                }
            )
    return expanded


def search_identity(
    config: dict[str, Any],
    slug: str,
    listing_path: str = "wohnung-mieten",
    price_type: str = "calculatedtotalrent",
) -> str:
    payload = {
        "provider": "immoscout24",
        "slug": slug,
        "listing_path": listing_path,
        "price_type": price_type,
        "rooms_min": config.get("rooms_min"),
        "rooms_max": config.get("rooms_max"),
        "warm_rent_target_eur": config.get("warm_rent_target_eur"),
        "sorting": "newest",
        "result_scope_version": SEARCH_RESULT_SCOPE_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"{slug}:{listing_path}:{price_type}:{digest}"


def catch_up_baseline_fingerprint(config: dict[str, Any]) -> str:
    identities = [
        search_identity(
            config,
            str(item["slug"]),
            str(item["listing_path"]),
            str(item["price_type"]),
        )
        for item in configured_searches(config)
    ]
    return hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()


def read_dotenv_value(path: Path, wanted: str) -> str:
    environment_value = os.environ.get(wanted, "").strip()
    if environment_value:
        return environment_value
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MonitorError(
            f"Missing {wanted}; set it in the environment or ignored .env file"
        ) from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value:
            break
        return value
    raise MonitorError(
        f"Missing {wanted}; set it in the environment or ignored .env file"
    )


def agent_browser_path() -> Path:
    candidates = []
    if AGENT_BROWSER_OVERRIDE:
        candidates.append(Path(AGENT_BROWSER_OVERRIDE).expanduser())
    candidates.append(ROOT / "node_modules" / ".bin" / "agent-browser")
    discovered = shutil.which("agent-browser")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise MonitorError(
        "agent-browser is unavailable; run npm install or set AGENT_BROWSER_PATH"
    )


def search_url(
    slug: str,
    max_warm_rent: float = 2000,
    page_number: int = 1,
    *,
    rooms_min: float = 3.0,
    rooms_max: float = 4.0,
    listing_path: str = "wohnung-mieten",
    price_type: str = "calculatedtotalrent",
) -> str:
    allowed_paths = {"wohnung-mieten", "haus-mieten", "einfamilienhaus-mieten"}
    if listing_path not in allowed_paths:
        raise MonitorError(f"Unsupported ImmoScout listing path: {listing_path}")
    parameters = {
        "numberofrooms": f"{float(rooms_min):.1f}-{float(rooms_max):.1f}",
        "price": f"-{float(max_warm_rent):.1f}",
        "pricetype": price_type,
        "sorting": "2",
    }
    if page_number > 1:
        parameters["pagenumber"] = str(page_number)
    query = urllib.parse.urlencode(parameters)
    return (
        "https://www.immobilienscout24.de/Suche/de/bayern/"
        f"{slug}/{listing_path}?{query}"
    )


def browser_is_ready(timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=timeout
        ) as response:
            return response.status == 200
    except Exception:
        return False


def wait_for_browser_ready(timeout_seconds: float = 120) -> None:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        if browser_is_ready():
            return
        if time.monotonic() >= deadline:
            raise MonitorError(
                f"Dedicated housing Chrome is not available on 127.0.0.1:{CDP_PORT}"
            )
        time.sleep(2)


def stop_dedicated_browser(
    process: subprocess.Popen[bytes], timeout_seconds: float = 10
) -> None:
    """Stop only the browser process group started by this monitor run."""
    process_group = process.pid
    with contextlib.suppress(ProcessLookupError):
        process_group = os.getpgid(process.pid)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
    try:
        process.wait(timeout=max(1.0, timeout_seconds))
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)

    group_is_alive = False
    try:
        os.killpg(process_group, 0)
        group_is_alive = True
    except ProcessLookupError:
        pass
    if group_is_alive:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)

    deadline = time.monotonic() + 5
    while browser_is_ready(timeout=0.25) and time.monotonic() < deadline:
        time.sleep(0.25)
    if browser_is_ready(timeout=0.25):
        raise MonitorError(
            f"Dedicated browser did not release 127.0.0.1:{CDP_PORT}"
        )


def start_dedicated_browser(
    timeout_seconds: float = 120,
) -> subprocess.Popen[bytes]:
    """Start one isolated browser, refusing to reuse any existing CDP port."""
    if browser_is_ready():
        raise MonitorError(
            f"Refusing to reuse an existing browser on 127.0.0.1:{CDP_PORT}"
        )
    if not GOOGLE_CHROME.exists():
        raise MonitorError(f"Dedicated Chrome is missing: {GOOGLE_CHROME}")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [
        str(GOOGLE_CHROME),
        f"--user-data-dir={PROFILE_DIR}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--start-minimized",
        "about:blank",
    ]
    with BROWSER_OUT_LOG.open("ab") as stdout_handle, BROWSER_ERR_LOG.open(
        "ab"
    ) as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            close_fds=True,
        )
    try:
        wait_for_browser_ready(timeout_seconds)
    except Exception:
        stop_dedicated_browser(process)
        raise
    return process


def trim_browser_tabs() -> int:
    """Keep only the active automation tab so restored pages cannot accumulate."""
    payload = browser_command(["tab", "list"], timeout=20)
    tabs = payload.get("tabs") if isinstance(payload, dict) else None
    if not isinstance(tabs, list) or len(tabs) <= 1:
        return 0
    keep = next(
        (item for item in tabs if isinstance(item, dict) and item.get("active")),
        tabs[0],
    )
    keep_id = str(keep.get("tabId") or "") if isinstance(keep, dict) else ""
    closed = 0
    for item in tabs:
        if not isinstance(item, dict):
            continue
        tab_id = str(item.get("tabId") or "")
        if tab_id == keep_id or not re.fullmatch(r"t\d+", tab_id):
            continue
        browser_command(["tab", "close", tab_id], timeout=20)
        closed += 1
    return closed


@contextlib.contextmanager
def managed_dedicated_browser(config: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Run the isolated browser only for the duration of one polling cycle."""
    settings = dict(config.get("catch_up") or {})
    timeout_seconds = float(settings.get("browser_startup_wait_seconds", 120))
    process = start_dedicated_browser(timeout_seconds)
    details = {"started_here": True, "tabs_closed": 0}
    try:
        wait_for_browser_ready(timeout_seconds)
        details["tabs_closed"] = trim_browser_tabs()
        yield details
    finally:
        stop_dedicated_browser(process)


def browser_command(args: list[str], timeout: int = 45) -> Any:
    executable = agent_browser_path()
    if not browser_is_ready():
        raise MonitorError(
            f"Dedicated housing Chrome is not available on 127.0.0.1:{CDP_PORT}"
        )
    command = [
        str(executable),
        "--cdp",
        str(CDP_PORT),
        "--session",
        SESSION_NAME,
    ]
    command.extend(["--json", *args])
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env={
            **os.environ,
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "AGENT_BROWSER_DEFAULT_TIMEOUT": "35000",
        },
    )
    output = (completed.stdout or completed.stderr).strip()
    if not output:
        raise MonitorError(f"Empty browser response (exit {completed.returncode})")
    try:
        payload = json.loads(output.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise MonitorError(f"Invalid browser response: {output[-300:]}") from exc
    if not payload.get("success"):
        raise MonitorError(str(payload.get("error") or "browser command failed"))
    return payload.get("data")


EXTRACT_JS = r"""(() => {
  const records = {};
  const resultRoot = document.querySelector('.HybridViewListViewContainer')
    || document.querySelector('[data-testid="result-list"]');
  for (const a of resultRoot ? resultRoot.querySelectorAll('a[href*="/expose/"]') : []) {
    const href = a.getAttribute('href') || '';
    const match = href.match(/\/expose\/(\d+)/);
    if (!match) continue;
    const id = match[1];
    const text = (a.innerText || '').replace(/\s+/g, ' ').trim();
    const heading = a.querySelector('h2');
    const img = a.querySelector('img');
    if (!records[id]) records[id] = {id, href, title: '', text: '', image: ''};
    if (text.length > records[id].text.length) records[id].text = text;
    if (heading && heading.innerText) records[id].title = heading.innerText.trim();
    if (img && (img.currentSrc || img.src)) records[id].image = img.currentSrc || img.src;
  }
  const heading = Array.from(document.querySelectorAll('h1'))
    .map(node => (node.innerText || '').replace(/\s+/g, ' ').trim())
    .find(text => /(Mietwohnungen|Häuser|Einfamilienhäuser)/i.test(text)) || '';
  const totalMatch = heading.match(/([\d.]+)/);
  const total = totalMatch ? Number(totalMatch[1].replace(/\./g, '')) : null;
  return JSON.stringify({
    rows: Object.values(records),
    total,
    url: location.href,
    resultRootFound: Boolean(resultRoot),
    bodyText: (document.body.innerText || '').slice(0, 2000)
  });
})()"""


def extract_result_value(data: Any) -> Any:
    if isinstance(data, dict):
        for key in ("result", "value", "data"):
            if key in data:
                return extract_result_value(data[key])
    if isinstance(data, str):
        candidate = data.strip()
        if candidate.startswith(("[", "{", '"')):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    return data


def parse_german_number(value: str) -> float:
    cleaned = value.replace(".", "").replace(",", ".")
    return float(cleaned)


def parse_listing(
    source: str,
    row: dict[str, Any],
    *,
    property_kind: str = "apartment",
    priority: int = 0,
    price_type: str = "calculatedtotalrent",
) -> Listing:
    raw = " ".join(str(row.get("text") or "").split())
    title = " ".join(str(row.get("title") or "").split())
    if not title:
        title = raw.split(" €", 1)[0].strip() or f"ImmoScout 房源 {row['id']}"

    price_match = re.search(r"~?\s*([\d.]+(?:,\d{1,2})?)\s*€", raw)
    area_match = re.search(r"([\d.]+(?:,\d+)?)\s*m²", raw)
    room_match = re.search(r"([\d.,]+)\s*Zi\.", raw)
    address = " ".join(str(row.get("address") or "").split())
    if not address and room_match and property_kind != "detached_house":
        address = raw[room_match.end() :].replace("Zum Merkzettel hinzufügen", "").strip()

    href = str(row.get("href") or f"/expose/{row['id']}")
    if href.startswith("/"):
        href = "https://www.immobilienscout24.de" + href
    displayed_rent = (
        parse_german_number(price_match.group(1)) if price_match else None
    )
    is_warm_rent = price_type == "calculatedtotalrent"
    return Listing(
        source=source,
        listing_id=str(row["id"]),
        title=title,
        raw_text=raw,
        url=href,
        image_url=str(row.get("image") or ""),
        warm_rent_eur=displayed_rent if is_warm_rent else None,
        cold_rent_eur=displayed_rent if not is_warm_rent else None,
        area_m2=parse_german_number(area_match.group(1)) if area_match else None,
        rooms=parse_german_number(room_match.group(1)) if room_match else None,
        address=address,
        property_kind=property_kind,
        priority=priority,
        warm_rent_verified=is_warm_rent and displayed_rent is not None,
    )


def page_number_from_url(url: str) -> int:
    values = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get(
        "pagenumber", ["1"]
    )
    try:
        return max(1, int(values[0]))
    except (TypeError, ValueError):
        return 1


def search_scope_is_preserved(
    url: str,
    *,
    listing_path: str,
    price_type: str,
    max_rent: float,
    rooms_min: float,
    rooms_max: float,
) -> bool:
    parsed = urllib.parse.urlparse(url)
    parameters = urllib.parse.parse_qs(parsed.query)
    return (
        parsed.path.rstrip("/").endswith(f"/{listing_path}")
        and parameters.get("pricetype") == [price_type]
        and parameters.get("price") == [f"-{float(max_rent):.1f}"]
        and parameters.get("numberofrooms")
        == [f"{float(rooms_min):.1f}-{float(rooms_max):.1f}"]
    )


def constrain_listings_to_result_total(
    listings: list[Listing], total: int | None, page_number: int
) -> list[Listing]:
    if page_number != 1 or total is None:
        return listings
    if total <= 0:
        return []
    return listings[:total]


def fetch_search_page(
    name: str,
    slug: str,
    page_number: int = 1,
    *,
    max_warm_rent: float = 2000,
    rooms_min: float = 3.0,
    rooms_max: float = 4.0,
    listing_path: str = "wohnung-mieten",
    price_type: str = "calculatedtotalrent",
    property_kind: str = "apartment",
    priority: int = 0,
) -> SearchPage:
    opened = browser_command(
        [
            "open",
            search_url(
                slug,
                max_warm_rent,
                page_number=page_number,
                rooms_min=rooms_min,
                rooms_max=rooms_max,
                listing_path=listing_path,
                price_type=price_type,
            ),
        ]
    )
    title = (
        str((opened or {}).get("title", ""))
        if isinstance(opened, dict)
        else str(opened)
    )
    lowered_title = title.lower()
    if (
        "kein roboter" in lowered_title
        or "captcha" in lowered_title
        or "gleich geht" in lowered_title
    ):
        raise BotChallenge("ImmoScout requires an interactive anti-bot check")
    browser_command(["wait", "h1"], timeout=40)
    evaluated = browser_command(["eval", EXTRACT_JS])
    payload = extract_result_value(evaluated)
    if not isinstance(payload, dict):
        raise MonitorError(
            f"Unexpected search payload for {name}: {type(payload).__name__}"
        )
    body_text = str(payload.get("bodyText") or "")
    lowered_body = body_text.lower()
    if (
        "kein roboter" in lowered_body
        or "anfrage blockiert" in lowered_body
        or "schädliche software" in lowered_body
    ):
        raise BotChallenge("ImmoScout requires an interactive anti-bot check")
    total_raw = payload.get("total")
    total = int(total_raw) if isinstance(total_raw, (int, float)) else None
    if not bool(payload.get("resultRootFound")) and total != 0:
        raise MonitorError(f"Result list container not found for {name}")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise MonitorError(
            f"Unexpected listing payload for {name}: {type(rows).__name__}"
        )
    listings = [
        parse_listing(
            name,
            row,
            property_kind=property_kind,
            priority=priority,
            price_type=price_type,
        )
        for row in rows
        if isinstance(row, dict) and row.get("id")
    ]
    # ImmoScout can render out-of-area recommendations below the genuine
    # result list. The h1 total is authoritative for page one.
    listings = constrain_listings_to_result_total(listings, total, page_number)
    if page_number == 1 and not listings and total != 0:
        raise MonitorError(
            f"No listings extracted for {name}; page layout or access may have changed"
        )
    final_url = str(payload.get("url") or "")
    if not search_scope_is_preserved(
        final_url,
        listing_path=listing_path,
        price_type=price_type,
        max_rent=max_warm_rent,
        rooms_min=rooms_min,
        rooms_max=rooms_max,
    ):
        raise MonitorError(f"Search filters were not preserved for {name}")
    actual_page = page_number_from_url(final_url)
    if actual_page != page_number:
        raise MonitorError(
            f"Pagination mismatch for {name}: requested {page_number}, "
            f"landed on {actual_page}"
        )
    return SearchPage(
        listings=listings,
        requested_page=page_number,
        final_url=final_url,
        total_results=total,
    )


def fetch_listings(name: str, slug: str, **search_options: Any) -> list[Listing]:
    return fetch_search_page(name, slug, **search_options).listings


def fetch_search_page_with_retry(
    name: str,
    slug: str,
    page_number: int = 1,
    attempts: int = 2,
    **search_options: Any,
) -> SearchPage:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetch_search_page(
                name, slug, page_number, **search_options
            )
        except BotChallenge:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                if not browser_is_ready():
                    wait_for_browser_ready(30)
                time.sleep(2)
    assert last_error is not None
    raise last_error


def fetch_listings_with_retry(
    name: str, slug: str, attempts: int = 2, **search_options: Any
) -> list[Listing]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetch_listings(name, slug, **search_options)
        except BotChallenge:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2)
    assert last_error is not None
    raise last_error


def open_database() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    initialize_database(connection)
    connection.commit()
    with contextlib.suppress(OSError):
        DB_PATH.chmod(0o600)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS seen (
            listing_key TEXT PRIMARY KEY,
            listing_id TEXT NOT NULL,
            source TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS search_seen (
            search_key TEXT NOT NULL,
            listing_key TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY(search_key, listing_key),
            FOREIGN KEY(listing_key) REFERENCES seen(listing_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_key TEXT NOT NULL UNIQUE,
            canonical_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(listing_key) REFERENCES seen(listing_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_deliveries (
            notification_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            sent_at TEXT,
            provider_message_id TEXT,
            last_error_class TEXT,
            FOREIGN KEY(notification_id) REFERENCES notifications(notification_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS email_batches (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_label TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            rfc_message_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            last_error_class TEXT,
            last_smtp_code INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS email_batch_items (
            batch_id INTEGER NOT NULL,
            notification_id INTEGER NOT NULL UNIQUE,
            PRIMARY KEY(batch_id, notification_id),
            FOREIGN KEY(batch_id) REFERENCES email_batches(batch_id),
            FOREIGN KEY(notification_id) REFERENCES notifications(notification_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_due "
        "ON telegram_deliveries(status, next_attempt_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_due "
        "ON email_batches(status, next_attempt_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_seen_listing "
        "ON search_seen(listing_key)"
    )
    initialize_contact_database(connection)
    initialize_pipeline_database(connection)


def get_meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def parse_utc_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def previous_full_success_at(
    db: sqlite3.Connection, config: dict[str, Any]
) -> dt.datetime | None:
    stored = parse_utc_timestamp(get_meta(db, "last_full_success_at"))
    if stored is not None:
        return stored
    try:
        payload = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("failures"):
        return None
    if int(payload.get("searches_ok", -1)) != len(configured_searches(config)):
        return None
    return parse_utc_timestamp(str(payload.get("finished_at") or ""))


def catch_up_baseline_is_current(
    db: sqlite3.Connection, config: dict[str, Any]
) -> bool:
    return (
        get_meta(db, "catch_up_baseline_complete") == "1"
        and get_meta(db, "catch_up_baseline_fingerprint")
        == catch_up_baseline_fingerprint(config)
    )


def catch_up_status(
    db: sqlite3.Connection,
    config: dict[str, Any],
    now: dt.datetime,
) -> tuple[bool, int | None]:
    settings = dict(config.get("catch_up") or {})
    if not bool(settings.get("enabled", True)):
        return False, None
    if not catch_up_baseline_is_current(db, config):
        return False, None
    previous = previous_full_success_at(db, config)
    if previous is None:
        return True, None
    gap_seconds = max(0, int((now - previous).total_seconds()))
    threshold = max(
        int(config.get("poll_interval_seconds", 900)) + 60,
        int(settings.get("trigger_gap_seconds", 1800)),
    )
    return gap_seconds >= threshold, gap_seconds


def upsert_listings(
    db: sqlite3.Connection, listings: Iterable[Listing], baseline: bool
) -> list[Listing]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    new_items: list[Listing] = []
    for listing in listings:
        ingest_source_listing(
            db,
            SourceListing(
                platform=listing.platform,
                account_scope="primary",
                external_listing_id=listing.listing_id,
                title=listing.title,
                raw_text=listing.raw_text,
                url=listing.url,
                source_label=listing.source,
                image_url=listing.image_url,
                address=listing.address,
                warm_rent_eur=listing.warm_rent_eur,
                cold_rent_eur=listing.cold_rent_eur,
                area_m2=listing.area_m2,
                rooms=listing.rooms,
                property_kind=listing.property_kind,
                warm_rent_verified=listing.warm_rent_verified,
            ),
        )
        existing = db.execute(
            "SELECT notified FROM seen WHERE listing_key = ?", (listing.key,)
        ).fetchone()
        payload = json.dumps(listing.__dict__, ensure_ascii=False, sort_keys=True)
        if existing is None:
            db.execute(
                "INSERT INTO seen VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    listing.key,
                    listing.listing_id,
                    listing.source,
                    now,
                    now,
                    1 if baseline else 0,
                    payload,
                ),
            )
            if not baseline:
                new_items.append(listing)
        else:
            db.execute(
                "UPDATE seen SET last_seen = ?, payload_json = ? WHERE listing_key = ?",
                (now, payload, listing.key),
            )
    db.commit()
    return new_items


def record_search_memberships(
    db: sqlite3.Connection, search_key: str, listings: Iterable[Listing]
) -> None:
    now = utc_now().isoformat()
    rows = [(search_key, listing.key, now, now) for listing in listings]
    if not rows:
        return
    db.executemany(
        "INSERT INTO search_seen(search_key, listing_key, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(search_key, listing_key) DO UPDATE SET "
        "last_seen = excluded.last_seen",
        rows,
    )
    db.commit()


def warm_rent_within_cap(listing: Listing, max_warm_rent: float) -> bool:
    return (
        listing.warm_rent_verified
        and listing.warm_rent_eur is not None
        and listing.warm_rent_eur <= max_warm_rent
    )


def verified_saved_search_platforms(config: dict[str, Any]) -> frozenset[str]:
    """Return platforms whose saved-search filters were verified by a human.

    The platform list is authoritative when present.  Older configurations that
    only contain ``saved_search_filters_verified=true`` retain their previous
    all-platform behavior, while missing, malformed, or false settings fail
    closed.
    """

    settings = dict(config.get("official_mail_sources") or {})
    if "verified_saved_search_platforms" in settings:
        configured = settings.get("verified_saved_search_platforms")
        if not isinstance(configured, list):
            return frozenset()
        return frozenset(
            str(platform).strip().casefold()
            for platform in configured
            if str(platform).strip().casefold() in OFFICIAL_MAIL_PLATFORMS
        )
    if settings.get("saved_search_filters_verified") is True:
        return OFFICIAL_MAIL_PLATFORMS
    return frozenset()


def listing_is_notification_eligible(
    listing: Listing, config: dict[str, Any]
) -> bool:
    """Fail closed before a listing can enter either notification channel."""

    verified_official_mail = (
        listing.discovery_method == "official_saved_search_email"
        and listing.platform.casefold() in verified_saved_search_platforms(config)
    )
    rooms = listing.rooms
    if rooms is None:
        if not verified_official_mail:
            return False
    elif not float(config["rooms_min"]) <= rooms <= float(config["rooms_max"]):
        return False
    max_warm_rent = float(config["warm_rent_target_eur"])
    if listing.warm_rent_eur is not None and listing.warm_rent_eur > max_warm_rent:
        return False
    if warm_rent_within_cap(listing, max_warm_rent):
        return True
    return verified_official_mail


def listing_passes_coarse_filter(
    listing: Listing,
    *,
    max_warm_rent: float,
    rooms_min: float,
    rooms_max: float,
) -> bool:
    """Filter list cards for internal collection; notification is stricter."""

    if listing.rooms is None or not rooms_min <= listing.rooms <= rooms_max:
        return False
    if listing.warm_rent_verified:
        return warm_rent_within_cap(listing, max_warm_rent)
    if listing.property_kind == "detached_house":
        # ImmoScout's public detached-house search only exposes Kaltmiete.
        # Kaltmiete <= the warm cap is a necessary precondition, not proof.
        return (
            listing.cold_rent_eur is not None
            and listing.cold_rent_eur <= max_warm_rent
        )
    return False


def scan_search(
    db: sqlite3.Connection,
    name: str,
    slug: str,
    *,
    search_key: str,
    deep_scan: bool,
    baseline: bool,
    max_pages: int,
    minimum_pages: int,
    known_boundary_pages: int,
    page_delay_seconds: float,
    max_warm_rent: float = 2000,
    rooms_min: float = 3.0,
    rooms_max: float = 4.0,
    listing_path: str = "wohnung-mieten",
    price_type: str = "calculatedtotalrent",
    property_kind: str = "apartment",
    priority: int = 0,
) -> SearchScanResult:
    fetched = 0
    pages_fetched = 0
    new_items: list[Listing] = []
    signatures: set[tuple[str, ...]] = set()
    collected_by_key: dict[str, Listing] = {}
    first_page_size: int | None = None
    total_pages: int | None = None
    historical_keys = {
        str(row[0])
        for row in db.execute(
            "SELECT listing_key FROM search_seen WHERE search_key = ?",
            (search_key,),
        ).fetchall()
    }
    consecutive_known_pages = 0

    def finish(complete: bool, error: str = "") -> SearchScanResult:
        collected = list(collected_by_key.values())
        if complete:
            if baseline:
                new_items.extend(upsert_listings(db, collected, baseline=True))
            record_search_memberships(db, search_key, collected)
        return SearchScanResult(
            listings_fetched=fetched,
            pages_fetched=pages_fetched,
            new_items=new_items,
            complete=complete,
            error=error,
        )

    for page_number in range(1, max(1, max_pages) + 1):
        try:
            page = fetch_search_page_with_retry(
                name,
                slug,
                page_number,
                max_warm_rent=max_warm_rent,
                rooms_min=rooms_min,
                rooms_max=rooms_max,
                listing_path=listing_path,
                price_type=price_type,
                property_kind=property_kind,
                priority=priority,
            )
        except BotChallenge:
            raise
        except Exception as exc:
            if page_number == 1:
                raise
            return finish(
                False, f"page {page_number}: {type(exc).__name__}: {exc}"
            )

        pages_fetched += 1
        fetched += len(page.listings)
        if not page.listings and page_number == 1 and page.total_results == 0:
            return finish(True)
        if not page.listings:
            return finish(False, f"page {page_number}: unexpected empty page")

        signature = tuple(sorted(listing.key for listing in page.listings))
        if signature in signatures:
            return finish(False, f"page {page_number}: repeated result page")
        signatures.add(signature)
        eligible_listings = [
            listing
            for listing in page.listings
            if listing_passes_coarse_filter(
                listing,
                max_warm_rent=max_warm_rent,
                rooms_min=rooms_min,
                rooms_max=rooms_max,
            )
        ]
        for listing in eligible_listings:
            collected_by_key[listing.key] = listing

        if first_page_size is None:
            first_page_size = len(page.listings)
            if page.total_results is not None and first_page_size:
                total_pages = max(1, math.ceil(page.total_results / first_page_size))

        known_before = {
            listing.key
            for listing in eligible_listings
            if listing.key in historical_keys
        }
        if not baseline:
            new_items.extend(upsert_listings(db, eligible_listings, baseline=False))

        if eligible_listings and len(known_before) == len(eligible_listings):
            consecutive_known_pages += 1
        else:
            consecutive_known_pages = 0

        if not deep_scan:
            return finish(True)
        if total_pages is not None and page_number >= total_pages:
            return finish(True)
        if baseline:
            if (
                total_pages is None
                and first_page_size is not None
                and len(page.listings) < first_page_size
            ):
                return finish(True)
        elif (
            page_number >= max(1, minimum_pages)
            and consecutive_known_pages >= max(1, known_boundary_pages)
        ):
            return finish(True)

        if page_number >= max(1, max_pages):
            return finish(
                False,
                f"reached safety limit of {max_pages} pages before a known boundary",
            )
        if page_delay_seconds > 0:
            time.sleep(page_delay_seconds)

    raise AssertionError("unreachable search scan state")


def telegram_request(method: str, payload: dict[str, str]) -> dict[str, Any]:
    token = read_dotenv_value(SECRETS_ENV, "TELEGRAM_BOT_TOKEN")
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise MonitorError(f"Telegram API failed: {data.get('description', 'unknown error')}")
    return data


def send_telegram(text: str) -> dict[str, Any]:
    chat_id = read_dotenv_value(SECRETS_ENV, "TELEGRAM_CHAT_ID")
    return telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "false",
        },
    )


def maps_link(origin: str, destination: str) -> str:
    params = urllib.parse.urlencode(
        {
            "api": "1",
            "origin": origin,
            "destination": destination,
            "travelmode": "transit",
        }
    )
    return f"https://www.google.com/maps/dir/?{params}"


def amenity_link(address: str, amenity_query: str) -> str:
    query = f"{amenity_query} near {address}" if address else amenity_query
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode(
        {"api": "1", "query": query}
    )


def format_listing(listing: Listing, config: dict[str, Any]) -> str:
    def value(number: float | None, suffix: str) -> str:
        if number is None:
            return "未提取"
        rendered = f"{number:.2f}".rstrip("0").rstrip(".")
        return rendered + suffix

    area = value(listing.area_m2, " m²")
    rooms = value(listing.rooms, " Zimmer")
    origin = listing.address or listing.source
    commute = maps_link(origin, str(config["commute_destination"]))
    amenity = amenity_link(listing.address, str(config["amenity_query"]))
    hard_cap = config["warm_rent_target_eur"]
    platform_label = {
        "immoscout24": "ImmoScout",
        "wggesucht": "WG-Gesucht",
        "immowelt": "Immowelt",
        "kleinanzeigen": "Kleinanzeigen",
    }.get(listing.platform, listing.platform)
    if listing.property_kind == "detached_house" and listing.warm_rent_verified:
        heading = f"🏡 {platform_label} 独栋候选（优先）"
        rent_line = f"暖租：{value(listing.warm_rent_eur, ' €')}"
        budget_note = f"暖租硬上限：≤{hard_cap} €"
    elif listing.property_kind == "detached_house":
        heading = f"🏡 {platform_label} 独栋预选（暖租待核实）"
        rent_line = f"冷租预筛：{value(listing.cold_rent_eur, ' €')}"
        budget_note = (
            f"暖租状态：未知；只有确认 Warmmiete≤{hard_cap} € 才算合格"
        )
    elif listing.warm_rent_verified:
        heading = f"🏠 {platform_label} 新公寓候选"
        rent_line = f"暖租：{value(listing.warm_rent_eur, ' €')}"
        budget_note = f"暖租硬上限：≤{hard_cap} €"
    else:
        heading = f"🔎 {platform_label} 官方提醒候选（暖租待核实）"
        rent_line = "暖租：邮件中未可靠提取"
        budget_note = (
            f"已由该站保存搜索预筛；联系前仍必须确认 Warmmiete≤{hard_cap} €"
        )
    contact_note = (
        "系统会先在本地准备草稿；"
        f"只有你明确批准房源ID {listing.key} 后才可进入发送流程。"
        if listing.warm_rent_verified
        else "暖租核实合格后才会生成联系草稿；未知暖租绝不会直接联系。"
    )
    return (
        f"{heading}\n"
        f"{listing.title}\n"
        f"区域：{listing.source}\n"
        f"{rent_line}｜面积：{area}｜房间：{rooms}\n"
        f"{budget_note}\n"
        f"地址：{listing.address or '列表页未完整显示'}\n"
        f"入住目标：{config['move_in_from']}–{config['move_in_to']}（详情需核实）\n"
        f"房源ID：{listing.key}\n"
        f"房源：{listing.url}\n"
        f"通勤路线：{commute}\n"
        f"周边设施（{config['amenity_query']}）：{amenity}\n"
        f"⚠️ 需核实：户型需求、真实入住日、"
        f"通勤≤{config['max_commute_minutes']} 分钟、指定周边设施，"
        f"以及所有强制费用后的真实暖租。{contact_note}"
    )


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def retry_at(attempts_after_failure: int) -> str:
    index = min(max(attempts_after_failure - 1, 0), len(RETRY_DELAYS_MINUTES) - 1)
    return (utc_now() + dt.timedelta(minutes=RETRY_DELAYS_MINUTES[index])).isoformat()


def queue_unnotified_listings(
    db: sqlite3.Connection, config: dict[str, Any]
) -> int:
    """Atomically turn every unqueued seen row into a durable notification."""

    rows = db.execute(
        "SELECT listing_key, payload_json, first_seen FROM seen WHERE notified = 0"
    ).fetchall()
    prepared_rows: list[tuple[str, dict[str, Any], str]] = []
    for listing_key, payload_json, first_seen in rows:
        payload = json.loads(str(payload_json))
        listing = Listing(**payload)
        if not listing_is_notification_eligible(listing, config):
            # Detached-house result cards expose Kaltmiete only. Keep these
            # internally for deduplication, but never put a Warmmiete-unknown
            # listing into Telegram or email candidate flows.
            continue
        prepared_rows.append((str(listing_key), payload, str(first_seen)))
    prepared_rows.sort(
        key=lambda row: (
            -(
                int(row[1].get("priority", 0))
                if bool(row[1].get("warm_rent_verified", True))
                else -1
            ),
            row[2],
            row[0],
        )
    )
    now = utc_now().isoformat()
    queued = 0
    with db:
        for listing_key, payload, _first_seen in prepared_rows:
            listing = Listing(**payload)
            canonical_text = format_listing(listing, config)
            db.execute(
                "INSERT OR IGNORE INTO notifications"
                "(listing_key, canonical_text, created_at) VALUES (?, ?, ?)",
                (listing_key, canonical_text, now),
            )
            notification_row = db.execute(
                "SELECT notification_id FROM notifications WHERE listing_key = ?",
                (listing_key,),
            ).fetchone()
            if notification_row is None:
                raise MonitorError("Failed to create durable notification")
            notification_id = int(notification_row[0])
            db.execute(
                "INSERT OR IGNORE INTO telegram_deliveries"
                "(notification_id, status, attempts, next_attempt_at) "
                "VALUES (?, 'pending', 0, ?)",
                (notification_id, now),
            )
            db.execute(
                "UPDATE seen SET notified = 1 WHERE listing_key = ?",
                (listing_key,),
            )
            queued += 1
    return queued


def email_body(canonical_texts: list[str], config: dict[str, Any]) -> str:
    count = len(canonical_texts)
    sections = [
        f"找房监控发现 {count} 条新房源。",
        (
            f"筛选：{config['rooms_min']:g}–{config['rooms_max']:g} Zimmer；"
            f"暖租硬上限≤{config['warm_rent_target_eur']} €；"
            f"入住目标 {config['move_in_from']}–{config['move_in_to']}。"
        ),
    ]
    for index, text in enumerate(canonical_texts, start=1):
        sections.append(f"[{index}/{count}]\n{text}")
    sections.append(
        "这是本机找房监控自动发送的只读提醒；机器人不会联系房东、提交申请或购买会员。"
    )
    return "\n\n----------------------------------------\n\n".join(sections)


def create_email_batches(db: sqlite3.Connection, config: dict[str, Any]) -> int:
    """Freeze unsent notifications into immutable, retryable email batches."""

    email_config = dict(config.get("email") or {})
    recipient = str(email_config.get("recipient") or "").strip()
    if not recipient:
        return 0
    batch_size = max(1, int(email_config.get("max_listings_per_email", 15)))
    created = 0
    while True:
        rows = db.execute(
            """
            SELECT n.notification_id, n.canonical_text
            FROM notifications AS n
            LEFT JOIN email_batch_items AS i
              ON i.notification_id = n.notification_id
            WHERE i.notification_id IS NULL
            ORDER BY n.notification_id
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()
        if not rows:
            break
        notification_ids = [int(row[0]) for row in rows]
        texts = [str(row[1]) for row in rows]
        now = utc_now()
        local_time = now.astimezone(BERLIN)
        prefix = str(email_config.get("subject_prefix") or "找房监控")
        subject = (
            f"{prefix}｜{len(rows)}条新房源｜"
            f"{local_time.strftime('%Y-%m-%d %H:%M')}"
        )
        body = email_body(texts, config)
        digest_source = ",".join(str(item) for item in notification_ids) + recipient
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:32]
        message_id = f"<housing-{digest}@housing-monitor.local>"
        with db:
            cursor = db.execute(
                """
                INSERT INTO email_batches(
                    recipient_label, subject, body, rfc_message_id,
                    status, attempts, next_attempt_at, created_at
                ) VALUES ('roommate', ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (subject, body, message_id, now.isoformat(), now.isoformat()),
            )
            batch_id = int(cursor.lastrowid)
            db.executemany(
                "INSERT INTO email_batch_items(batch_id, notification_id) VALUES (?, ?)",
                [(batch_id, notification_id) for notification_id in notification_ids],
            )
        created += 1
    return created


def queue_current_email_backfill(
    db: sqlite3.Connection,
    config: dict[str, Any],
    *,
    active_window_minutes: int = 10,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Queue current, never-notified baseline rows for email-only backfill.

    Normal new listings still go to both channels.  Historical backfill is
    deliberately email-only so hundreds of old rows cannot flood Telegram.
    A single Telegram completion summary is sent by the CLI after delivery.
    """

    latest_raw = db.execute("SELECT MAX(last_seen) FROM seen").fetchone()[0]
    if not latest_raw:
        return {"selected": 0, "batches_created": 0, "cutoff": ""}
    latest = dt.datetime.fromisoformat(str(latest_raw))
    cutoff = latest - dt.timedelta(minutes=max(1, active_window_minutes))
    rows = db.execute(
        """
        SELECT s.listing_key, s.payload_json
        FROM seen AS s
        LEFT JOIN notifications AS n ON n.listing_key = s.listing_key
        WHERE n.listing_key IS NULL AND s.last_seen >= ?
        ORDER BY s.first_seen, s.listing_key
        """,
        (cutoff.isoformat(),),
    ).fetchall()
    now = utc_now().isoformat()
    with db:
        for listing_key, payload_json in rows:
            listing = Listing(**json.loads(str(payload_json)))
            cursor = db.execute(
                """
                INSERT INTO notifications(listing_key, canonical_text, created_at)
                VALUES (?, ?, ?)
                """,
                (listing_key, format_listing(listing, config), now),
            )
            db.execute(
                """
                INSERT INTO telegram_deliveries(
                    notification_id, status, attempts, next_attempt_at,
                    last_error_class
                ) VALUES (?, 'skipped', 0, ?, 'backfill_email_only')
                """,
                (int(cursor.lastrowid), now),
            )

    backfill_config = {
        **config,
        "email": {
            **dict(config.get("email") or {}),
            "max_listings_per_email": max(1, batch_size),
            "subject_prefix": "找房监控历史补发",
        },
    }
    batches_created = create_email_batches(db, backfill_config)
    return {
        "selected": len(rows),
        "batches_created": batches_created,
        "cutoff": cutoff.isoformat(),
    }


def telegram_is_enabled(config: dict[str, Any]) -> bool:
    return bool(dict(config.get("telegram") or {}).get("enabled", False))


def drain_telegram_outbox(
    db: sqlite3.Connection,
    *,
    limit: int,
    config: dict[str, Any] | None = None,
) -> dict[str, int]:
    if config is not None and not telegram_is_enabled(config):
        return {"sent": 0, "retry": 0}
    rows = db.execute(
        """
        SELECT d.notification_id, d.attempts, n.canonical_text
        FROM telegram_deliveries AS d
        JOIN notifications AS n USING(notification_id)
        WHERE d.status IN ('pending', 'retry') AND d.next_attempt_at <= ?
        ORDER BY d.notification_id
        LIMIT ?
        """,
        (utc_now().isoformat(), max(1, limit)),
    ).fetchall()
    sent = 0
    retried = 0
    for notification_id, attempts, text in rows:
        next_attempt = int(attempts) + 1
        try:
            result = send_telegram(str(text))
            provider_id = str((result.get("result") or {}).get("message_id") or "")
        except Exception as exc:
            db.execute(
                """
                UPDATE telegram_deliveries
                SET status = 'retry', attempts = ?, next_attempt_at = ?,
                    last_error_class = ?
                WHERE notification_id = ?
                """,
                (
                    next_attempt,
                    retry_at(next_attempt),
                    type(exc).__name__,
                    notification_id,
                ),
            )
            db.commit()
            retried += 1
            continue
        db.execute(
            """
            UPDATE telegram_deliveries
            SET status = 'sent', attempts = ?, sent_at = ?,
                provider_message_id = ?, last_error_class = NULL
            WHERE notification_id = ?
            """,
            (next_attempt, utc_now().isoformat(), provider_id, notification_id),
        )
        db.commit()
        sent += 1
    return {"sent": sent, "retry": retried}


def drain_email_outbox(
    db: sqlite3.Connection, config: dict[str, Any]
) -> dict[str, Any]:
    email_config = dict(config.get("email") or {})
    if not bool(email_config.get("enabled", False)):
        return {"sent": 0, "retry": 0, "blocked": 0, "error": "disabled"}
    try:
        notifier = build_email_notifier(email_config)
    except EmailDeliveryError as exc:
        return {"sent": 0, "retry": 0, "blocked": 0, "error": exc.category}

    rows = db.execute(
        """
        SELECT batch_id, subject, body, rfc_message_id, attempts, created_at
        FROM email_batches
        WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?
        ORDER BY batch_id
        LIMIT ?
        """,
        (
            utc_now().isoformat(),
            max(1, int(email_config.get("max_batches_per_run", 1))),
        ),
    ).fetchall()
    sent = 0
    retried = 0
    blocked = 0
    alert = ""
    max_age_hours = int(email_config.get("max_age_hours", 24))
    max_age = (
        dt.timedelta(hours=max_age_hours) if max_age_hours > 0 else None
    )
    for batch_id, subject, body, message_id, attempts, created_at in rows:
        created = dt.datetime.fromisoformat(str(created_at))
        if max_age is not None and utc_now() - created > max_age:
            db.execute(
                "UPDATE email_batches SET status = 'blocked', "
                "last_error_class = 'expired' WHERE batch_id = ?",
                (batch_id,),
            )
            db.commit()
            blocked += 1
            alert = alert or "expired"
            continue
        next_attempt = int(attempts) + 1
        try:
            notifier.send(
                subject=str(subject), body=str(body), message_id=str(message_id)
            )
        except EmailDeliveryError as exc:
            status = "retry" if exc.retryable else "blocked"
            next_time = retry_at(next_attempt) if exc.retryable else utc_now().isoformat()
            db.execute(
                """
                UPDATE email_batches
                SET status = ?, attempts = ?, next_attempt_at = ?,
                    last_error_class = ?, last_smtp_code = ?
                WHERE batch_id = ?
                """,
                (
                    status,
                    next_attempt,
                    next_time,
                    exc.category,
                    exc.smtp_code,
                    batch_id,
                ),
            )
            db.commit()
            if exc.retryable:
                retried += 1
            else:
                blocked += 1
            alert = alert or exc.category
            continue
        db.execute(
            """
            UPDATE email_batches
            SET status = 'sent', attempts = ?, sent_at = ?,
                last_error_class = NULL, last_smtp_code = NULL
            WHERE batch_id = ?
            """,
            (next_attempt, utc_now().isoformat(), batch_id),
        )
        db.commit()
        sent += 1
    return {
        "sent": sent,
        "retry": retried,
        "blocked": blocked,
        "error": alert,
    }


def official_mail_read_limits(
    db: sqlite3.Connection,
    config: dict[str, Any],
    *,
    now: dt.datetime | None = None,
) -> tuple[int, int]:
    """Choose a bounded Apple Mail catch-up window without replaying on first use."""

    settings = dict(config.get("official_mail_sources") or {})
    base_lookback_days = min(
        OFFICIAL_MAIL_MAX_LOOKBACK_DAYS,
        max(1, int(settings.get("lookback_days", 7))),
    )
    base_max_messages = min(
        OFFICIAL_MAIL_MAX_MESSAGES,
        max(1, int(settings.get("max_messages", 200))),
    )
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    previous = parse_utc_timestamp(
        get_meta(db, OFFICIAL_MAIL_LAST_SUCCESS_META_KEY)
    )
    if previous is None:
        return base_lookback_days, base_max_messages

    gap_seconds = max(0.0, (current - previous).total_seconds())
    gap_days = int(math.ceil(gap_seconds / 86400.0))
    lookback_days = min(
        OFFICIAL_MAIL_MAX_LOOKBACK_DAYS,
        max(base_lookback_days, gap_days),
    )
    scaled_messages = int(
        math.ceil(base_max_messages * lookback_days / base_lookback_days)
    )
    return lookback_days, min(
        OFFICIAL_MAIL_MAX_MESSAGES,
        max(base_max_messages, scaled_messages),
    )


def ingest_official_mail_source(
    db: sqlite3.Connection, config: dict[str, Any]
) -> dict[str, int | str]:
    settings = dict(config.get("official_mail_sources") or {})
    if not bool(settings.get("enabled", False)):
        return {
            "messages_new": 0,
            "listing_links_new": 0,
            "replies_new": 0,
            "promoted": 0,
            "ignored": 0,
            "error": "disabled",
        }
    read_started_at = utc_now()
    lookback_days, max_messages = official_mail_read_limits(
        db, config, now=read_started_at
    )
    try:
        read_result = read_recent_housing_mail(
            lookback_days=lookback_days,
            max_messages=max_messages,
            timeout_seconds=int(settings.get("timeout_seconds", 60)),
        )
        ingested = ingest_mail_messages(
            db,
            read_result.messages,
            account_scope=str(settings.get("account_scope", "primary")),
        )
        promoted = promote_mail_listings_to_seen(db)
    except AppleMailReadError as exc:
        return {
            "messages_new": 0,
            "listing_links_new": 0,
            "replies_new": 0,
            "promoted": 0,
            "ignored": 0,
            "error": str(exc),
        }
    set_meta(db, OFFICIAL_MAIL_LAST_SUCCESS_META_KEY, read_started_at.isoformat())
    return {
        "messages_new": ingested.messages_new,
        "listing_links_new": ingested.listing_links_new,
        "replies_new": ingested.replies_new,
        "promoted": promoted,
        "ignored": ingested.ignored,
        "error": "",
    }


def drain_reply_notification_outboxes(
    db: sqlite3.Connection, config: dict[str, Any], *, limit_per_channel: int = 10
) -> dict[str, int]:
    counts = {
        "telegram_sent": 0,
        "telegram_ambiguous": 0,
        "email_sent": 0,
        "email_ambiguous": 0,
        "email_unavailable": 0,
    }

    def telegram_sender(_subject: str, body: str, _event_key: str) -> str:
        payload = send_telegram("📩 " + body)
        result = payload.get("result") if isinstance(payload, dict) else None
        return str(result.get("message_id") or "") if isinstance(result, dict) else ""

    def drain_channel(channel: str, sender: Any) -> None:
        for _ in range(max(0, int(limit_per_channel))):
            outcome = deliver_one_reply_notification(db, channel, sender)
            if not bool(outcome.get("processed")):
                break
            status = str(outcome.get("outcome") or "")
            if status == "sent":
                counts[f"{channel}_sent"] += 1
            elif status == "ambiguous":
                counts[f"{channel}_ambiguous"] += 1

    # A broken email account must never stop the Telegram side of the contract.
    if telegram_is_enabled(config):
        drain_channel("telegram", telegram_sender)

    email_config = dict(config.get("email") or {})
    if not bool(email_config.get("enabled", False)):
        counts["email_unavailable"] = 1
        return counts
    try:
        email_notifier = build_email_notifier(email_config)
    except EmailDeliveryError:
        counts["email_unavailable"] = 1
        return counts

    def email_sender(subject: str, body: str, event_key: str) -> str:
        message_id = f"<housing-reply-{event_key}@local.housing-monitor>"
        return email_notifier.send(
            subject=subject, body=body, message_id=message_id
        ).message_id

    drain_channel("email", email_sender)
    return counts


def should_send_error(db: sqlite3.Connection, category: str, hours: int = 6) -> bool:
    key = f"last_error_notice:{category}"
    raw = get_meta(db, key)
    now = dt.datetime.now(dt.timezone.utc)
    if raw:
        with contextlib.suppress(ValueError):
            previous = dt.datetime.fromisoformat(raw)
            if now - previous < dt.timedelta(hours=hours):
                return False
    set_meta(db, key, now.isoformat())
    return True


@contextlib.contextmanager
def exclusive_lock() -> Iterable[None]:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with LOCK_PATH.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MonitorError("Another housing monitor run is active") from exc
        yield


def run_once(
    *,
    force_baseline: bool = False,
    no_jitter: bool = False,
    lock_held: bool = False,
) -> dict[str, Any]:
    config = load_config()
    if not no_jitter:
        time.sleep(random.randint(0, 45))
    lock_context = contextlib.nullcontext() if lock_held else exclusive_lock()
    with lock_context, open_database() as db:
        catch_settings = dict(config.get("catch_up") or {})
        contact_settings = dict(config.get("contacts") or {})
        active_searches = configured_searches(config)
        limit = int(config.get("max_notifications_per_run", 15))
        official_mail = ingest_official_mail_source(db, config)
        reply_deliveries = drain_reply_notification_outboxes(
            db,
            config,
            limit_per_channel=int(contact_settings.get("reply_notifications_per_run", 10)),
        )
        contacts_enabled = bool(contact_settings.get("enabled", False))
        if contacts_enabled:
            drafts_before_scan = prepare_contact_drafts(
                db,
                config,
                only_unnotified=True,
                use_deepseek=bool(
                    contact_settings.get("deepseek_personalization", False)
                ),
            )
        else:
            drafts_before_scan = DraftBatchResult(0, 0, 0, 0, 0)
        queued_before_scan = queue_unnotified_listings(db, config)
        batches_before_scan = create_email_batches(db, config)
        telegram_before_scan = drain_telegram_outbox(db, limit=limit, config=config)
        email_before_scan = drain_email_outbox(db, config)
        first_run = get_meta(db, "baseline_complete") != "1"
        baseline = first_run or force_baseline
        if force_baseline:
            set_meta(db, "baseline_complete", "0")
        if (
            not baseline
            and bool(catch_settings.get("enabled", True))
            and not catch_up_baseline_is_current(db, config)
        ):
            raise MonitorError(
                "Catch-up baseline is missing or stale; "
                "run --initialize-catch-up-baseline before polling"
            )
        wait_for_browser_ready(
            float(catch_settings.get("browser_startup_wait_seconds", 120))
        )
        run_started_at = utc_now()
        catch_up, gap_seconds = catch_up_status(db, config, run_started_at)
        if baseline:
            catch_up = False
        all_new: list[Listing] = []
        fetched = 0
        pages_fetched = 0
        successful_searches = 0
        failures: list[str] = []
        challenge_seen = False
        for item in active_searches:
            search_name = f"{item['property_label']} · {item['name']}"
            try:
                scan = scan_search(
                    db,
                    search_name,
                    str(item["slug"]),
                    search_key=search_identity(
                        config,
                        str(item["slug"]),
                        str(item["listing_path"]),
                        str(item["price_type"]),
                    ),
                    deep_scan=catch_up,
                    baseline=baseline,
                    max_pages=int(catch_settings.get("max_pages_per_search", 40)),
                    minimum_pages=int(catch_settings.get("minimum_pages", 2)),
                    known_boundary_pages=int(
                        catch_settings.get("known_boundary_pages", 2)
                    ),
                    page_delay_seconds=float(
                        catch_settings.get("page_delay_seconds", 0.8)
                    ),
                    max_warm_rent=float(config["warm_rent_target_eur"]),
                    rooms_min=float(config["rooms_min"]),
                    rooms_max=float(config["rooms_max"]),
                    listing_path=str(item["listing_path"]),
                    price_type=str(item["price_type"]),
                    property_kind=str(item["property_kind"]),
                    priority=int(item["priority"]),
                )
                fetched += scan.listings_fetched
                pages_fetched += scan.pages_fetched
                all_new.extend(scan.new_items)
                if scan.complete:
                    successful_searches += 1
                else:
                    failures.append(f"{search_name}: {scan.error}")
            except BotChallenge as exc:
                challenge_seen = True
                failures.append(f"{search_name}: {exc}")
                if should_send_error(db, "captcha"):
                    if telegram_is_enabled(config):
                        send_telegram(
                            "⚠️ 找房监控暂停：ImmoScout 要求人工验证码\n"
                            "请在专用浏览器中由本人完成一次验证。"
                            "其他已配置的官方搜索提醒仍然有效。"
                        )
                break
            except Exception as exc:
                failures.append(f"{search_name}: {type(exc).__name__}: {exc}")

        if baseline and successful_searches == len(active_searches) and not failures:
            set_meta(db, "baseline_complete", "1")
            if telegram_is_enabled(config):
                send_telegram(
                    "✅ 找房监控基线已建立\n"
                    f"已记录 {fetched} 条当前列表结果；以后只推送新出现的房源，"
                    "避免首次刷屏。\n"
                    f"房间：{config['rooms_min']:g}–{config['rooms_max']:g} Zimmer；"
                    f"暖租硬上限≤{config['warm_rent_target_eur']} €。"
                )

        if successful_searches == len(active_searches) and not failures:
            set_meta(db, "last_full_success_at", utc_now().isoformat())

        if contacts_enabled:
            drafts_after_scan = prepare_contact_drafts(
                db,
                config,
                only_unnotified=True,
                use_deepseek=bool(
                    contact_settings.get("deepseek_personalization", False)
                ),
            )
        else:
            drafts_after_scan = DraftBatchResult(0, 0, 0, 0, 0)
        queued_after_scan = queue_unnotified_listings(db, config)
        batches_after_scan = create_email_batches(db, config)
        telegram_after_scan = drain_telegram_outbox(db, limit=limit, config=config)
        email_after_scan = drain_email_outbox(db, config)
        queued = queued_before_scan + queued_after_scan
        email_batches_created = batches_before_scan + batches_after_scan
        telegram_delivery = {
            "sent": int(telegram_before_scan["sent"])
            + int(telegram_after_scan["sent"]),
            "retry": int(telegram_before_scan["retry"])
            + int(telegram_after_scan["retry"]),
        }
        email_delivery = {
            "sent": int(email_before_scan["sent"])
            + int(email_after_scan["sent"]),
            "retry": int(email_before_scan["retry"])
            + int(email_after_scan["retry"]),
            "blocked": int(email_before_scan["blocked"])
            + int(email_after_scan["blocked"]),
            "error": str(email_after_scan.get("error") or "")
            or str(email_before_scan.get("error") or ""),
        }

        pending_telegram = int(
            db.execute(
                "SELECT COUNT(*) FROM telegram_deliveries "
                "WHERE status IN ('pending', 'retry')"
            ).fetchone()[0]
        )
        if pending_telegram and queued > limit:
            if telegram_is_enabled(config):
                send_telegram(
                    f"ℹ️ 本轮新增较多，已有 {pending_telegram} 条进入可靠队列，"
                    "后续检查会继续发送，不会丢失。"
                )
        if failures and not challenge_seen and should_send_error(db, "generic"):
            safe = "\n".join(line[:240] for line in failures[:5])
            if telegram_is_enabled(config):
                send_telegram(
                    "⚠️ 找房监控部分来源读取失败\n"
                    f"{safe}\n其他已配置的官方搜索提醒不受影响。"
                )
        email_error = str(email_delivery.get("error") or "")
        if email_error and email_error != "disabled":
            if should_send_error(db, f"email:{email_error}"):
                if telegram_is_enabled(config):
                    send_telegram(
                        "⚠️ 邮件通知暂未成功\n"
                        f"错误类别：{email_error}。Telegram 房源通知不受影响。"
                    )
        official_mail_error = str(official_mail.get("error") or "")
        if official_mail_error and official_mail_error != "disabled":
            if should_send_error(db, "official_mail_source"):
                if telegram_is_enabled(config):
                    send_telegram(
                        "⚠️ 官方保存搜索邮件暂未读取成功\n"
                        f"错误：{official_mail_error[:180]}。网页扫描仍继续运行。"
                    )
        result = {
            "finished_at": utc_now().isoformat(),
            "baseline": baseline,
            "catch_up": catch_up,
            "catch_up_gap_seconds": gap_seconds,
            "catch_up_complete": bool(catch_up and not failures),
            "fetched": fetched,
            "pages_fetched": pages_fetched,
            "searches_ok": successful_searches,
            "searches_total": len(active_searches),
            "new": len(all_new),
            "contact_drafts_created": int(drafts_before_scan.created)
            + int(drafts_after_scan.created),
            "contact_drafts_deepseek": int(drafts_before_scan.deepseek_used)
            + int(drafts_after_scan.deepseek_used),
            "contact_drafts_fallback": int(drafts_before_scan.fallback_used)
            + int(drafts_after_scan.fallback_used),
            "official_mail_messages_new": int(official_mail["messages_new"]),
            "official_mail_listing_links_new": int(
                official_mail["listing_links_new"]
            ),
            "official_mail_listings_promoted": int(official_mail["promoted"]),
            "landlord_replies_new": int(official_mail["replies_new"]),
            "reply_telegram_sent": int(reply_deliveries["telegram_sent"]),
            "reply_email_sent": int(reply_deliveries["email_sent"]),
            "reply_telegram_ambiguous": int(
                reply_deliveries["telegram_ambiguous"]
            ),
            "reply_email_ambiguous": int(reply_deliveries["email_ambiguous"]),
            "queued": queued,
            "telegram_sent": int(telegram_delivery["sent"]),
            "telegram_retry": int(telegram_delivery["retry"]),
            "email_batches_created": email_batches_created,
            "email_batches_sent": int(email_delivery["sent"]),
            "email_batches_retry": int(email_delivery["retry"]),
            "email_batches_blocked": int(email_delivery["blocked"]),
            "failures": failures,
        }
        LAST_RUN_PATH.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with contextlib.suppress(OSError):
            LAST_RUN_PATH.chmod(0o600)
        return result


def initialize_catch_up_baseline(*, lock_held: bool = False) -> None:
    config = load_config()
    settings = dict(config.get("catch_up") or {})
    active_searches = configured_searches(config)
    fingerprint = catch_up_baseline_fingerprint(config)
    lock_context = contextlib.nullcontext() if lock_held else exclusive_lock()
    with lock_context, open_database() as db:
        if (
            get_meta(db, "catch_up_baseline_complete") == "1"
            and get_meta(db, "catch_up_baseline_fingerprint") == fingerprint
        ):
            print(
                json.dumps(
                    {"already_complete": True, "searches_total": len(active_searches)},
                    ensure_ascii=False,
                )
            )
            return
        set_meta(db, "catch_up_baseline_complete", "0")
        wait_for_browser_ready(
            float(settings.get("browser_startup_wait_seconds", 120))
        )
        fetched = 0
        pages_fetched = 0
        searches_complete = 0
        searches_skipped = 0
        failures: list[str] = []
        for item in active_searches:
            search_name = f"{item['property_label']} · {item['name']}"
            identity = search_identity(
                config,
                str(item["slug"]),
                str(item["listing_path"]),
                str(item["price_type"]),
            )
            marker = f"catch_up_baseline_search:{identity}"
            if get_meta(db, marker) == "1":
                searches_complete += 1
                searches_skipped += 1
                continue
            try:
                scan = scan_search(
                    db,
                    search_name,
                    str(item["slug"]),
                    search_key=identity,
                    deep_scan=True,
                    baseline=True,
                    max_pages=int(settings.get("max_pages_per_search", 40)),
                    minimum_pages=int(settings.get("minimum_pages", 2)),
                    known_boundary_pages=int(
                        settings.get("known_boundary_pages", 2)
                    ),
                    page_delay_seconds=float(
                        settings.get("page_delay_seconds", 0.8)
                    ),
                    max_warm_rent=float(config["warm_rent_target_eur"]),
                    rooms_min=float(config["rooms_min"]),
                    rooms_max=float(config["rooms_max"]),
                    listing_path=str(item["listing_path"]),
                    price_type=str(item["price_type"]),
                    property_kind=str(item["property_kind"]),
                    priority=int(item["priority"]),
                )
                fetched += scan.listings_fetched
                pages_fetched += scan.pages_fetched
                if scan.complete:
                    searches_complete += 1
                    set_meta(db, marker, "1")
                else:
                    failures.append(f"{search_name}: {scan.error}")
            except Exception as exc:
                failures.append(
                    f"{search_name}: {type(exc).__name__}: {exc}"
                )

        result = {
            "already_complete": False,
            "fetched": fetched,
            "pages_fetched": pages_fetched,
            "searches_complete": searches_complete,
            "searches_skipped": searches_skipped,
            "searches_total": len(active_searches),
            "failures": failures,
        }
        if failures or searches_complete != len(active_searches):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise MonitorError("Catch-up baseline did not complete")
        completed_at = utc_now().isoformat()
        set_meta(db, "catch_up_baseline_completed_at", completed_at)
        set_meta(db, "catch_up_baseline_fingerprint", fingerprint)
        set_meta(db, "catch_up_baseline_complete", "1")
        set_meta(db, "last_full_success_at", completed_at)
        print(json.dumps(result, ensure_ascii=False, indent=2))


def telegram_test() -> None:
    result = send_telegram(
        "✅ 找房监控 Telegram 通道测试成功\n"
        "个人私聊通知；不会自动联系房东、提交申请或购买会员。"
    )
    safe = {
        "ok": bool(result.get("ok")),
        "message_id_present": bool((result.get("result") or {}).get("message_id")),
    }
    print(json.dumps(safe, ensure_ascii=False))


def email_test() -> None:
    config = load_config()
    email_config = dict(config.get("email") or {})
    now = utc_now()
    nonce = hashlib.sha256(now.isoformat().encode("utf-8")).hexdigest()[:10]
    message_id = f"<housing-test-{nonce}@housing-monitor.local>"
    notifier = build_email_notifier(email_config)
    result = notifier.send(
        subject=f"找房监控｜邮件通道测试｜{nonce}",
        body=(
            "✅ 找房监控邮件通知通道测试。\n\n"
            f"验收码：{nonce}\n"
            "此测试不会联系房东、提交申请或购买会员。"
        ),
        message_id=message_id,
    )
    print(
        json.dumps(
            {
                "accepted_by_transport": True,
                "transport": str(email_config.get("transport") or "smtp"),
                "message_id_present": bool(result.message_id),
                "verification_nonce": nonce,
            },
            ensure_ascii=False,
        )
    )


def backfill_current_email() -> None:
    config = load_config()
    with exclusive_lock(), open_database() as db:
        queued = queue_current_email_backfill(db, config)
        pending_before = int(
            db.execute(
                "SELECT COUNT(*) FROM email_batches "
                "WHERE status IN ('pending', 'retry')"
            ).fetchone()[0]
        )
        send_config = {
            **config,
            "email": {
                **dict(config.get("email") or {}),
                "max_batches_per_run": max(1, pending_before),
            },
        }
        delivery = drain_email_outbox(db, send_config)
        pending_after = int(
            db.execute(
                "SELECT COUNT(*) FROM email_batches "
                "WHERE status IN ('pending', 'retry')"
            ).fetchone()[0]
        )
        if int(queued["selected"]):
            if pending_after == 0 and int(delivery["blocked"]) == 0:
                if telegram_is_enabled(config):
                    send_telegram(
                        "✅ 历史房源补发完成\n"
                        f"已将 {queued['selected']} 条当前仍在线的未发送房源，"
                        f"整理为 {delivery['sent']} 封邮件。\n"
                        "已发过的房源没有重复；Telegram 未逐条刷屏。"
                    )
            else:
                if telegram_is_enabled(config):
                    send_telegram(
                        "⚠️ 历史房源补发尚未完全完成\n"
                        f"目标 {queued['selected']} 条；成功邮件 {delivery['sent']} 封；"
                        f"待重试 {pending_after} 封；阻塞 {delivery['blocked']} 封。"
                    )
        print(
            json.dumps(
                {
                    "selected": int(queued["selected"]),
                    "batches_created": int(queued["batches_created"]),
                    "email_batches_sent": int(delivery["sent"]),
                    "email_batches_retry": int(delivery["retry"]),
                    "email_batches_blocked": int(delivery["blocked"]),
                    "pending_after": pending_after,
                },
                ensure_ascii=False,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--telegram-test", action="store_true")
    parser.add_argument("--email-test", action="store_true")
    parser.add_argument("--backfill-current-email", action="store_true")
    parser.add_argument("--initialize-catch-up-baseline", action="store_true")
    parser.add_argument("--no-jitter", action="store_true")
    args = parser.parse_args()
    if args.telegram_test:
        telegram_test()
        return
    if args.email_test:
        email_test()
        return
    if args.backfill_current_email:
        backfill_current_email()
        return
    if args.initialize_catch_up_baseline:
        config = load_config()
        with exclusive_lock():
            with managed_dedicated_browser(config):
                initialize_catch_up_baseline(lock_held=True)
        return
    if not args.no_jitter:
        time.sleep(random.randint(0, 45))
    config = load_config()
    with exclusive_lock():
        with managed_dedicated_browser(config):
            result = run_once(
                force_baseline=args.baseline_only,
                no_jitter=True,
                lock_held=True,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def terminate_gracefully(signum: int, _frame: Any) -> None:
    """Turn launchd termination into stack unwinding so browser cleanup runs."""
    signal.signal(signum, signal.SIG_IGN)
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate_gracefully)
    signal.signal(signal.SIGHUP, terminate_gracefully)
    try:
        main()
    except Exception as exc:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        failure = {
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "fatal_error": f"{type(exc).__name__}: {exc}",
        }
        LAST_RUN_PATH.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"housing monitor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
