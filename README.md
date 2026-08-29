# Germany Housing Monitor

[中文说明](README.zh-CN.md)

A privacy-first, local housing discovery monitor for Germany. It scans configured ImmoScout search pages, can ingest authenticated saved-search alerts from selected platforms, deduplicates listings in SQLite, and queues Telegram or email notifications. An optional contact workflow creates immutable drafts, but keeps every landlord contact behind per-listing review and approval.

## What is safe by default

- Telegram, email, official-mail ingestion, model personalization, and contact drafting are disabled in the example configuration.
- No real landlord transport is included. The included contact transport is synthetic and cannot submit a message to a housing platform.
- Applicant profiles, credentials, browser cookies, logs, backups, and SQLite files are excluded from Git.
- Official saved-search mail is trusted only when sender domain, Gmail DMARC authentication, platform identity, and listing-link platform agree.
- Listing text and landlord replies are untrusted data. They are displayed or summarized, never executed as instructions.
- Browser debugging is loopback-only and an existing debugging endpoint is rejected rather than reused.

## Current scope

| Capability | Status |
| --- | --- |
| ImmoScout result discovery | Implemented through a dedicated Chrome session |
| WG-Gesucht / Immowelt saved-search mail | Implemented, disabled until filters are verified |
| Cross-platform identity and snapshot history | Implemented |
| Telegram and email notifications | Implemented, disabled by default |
| Approval-gated contact drafts | Implemented, disabled by default |
| Real landlord submission | Not included |
| Kleinanzeigen ingestion | Fail-closed / not integrated |

Search-page structures and platform rules can change. Re-verify a provider before relying on it, and respect the provider's terms and rate limits.

## Requirements

- Python 3.11 or newer
- Node.js with npm
- Google Chrome for Testing, or a compatible executable selected with `HOUSING_MONITOR_CHROME_PATH`
- macOS for Apple Mail and launchd integration; SMTP notification code is otherwise platform-independent

The Python runtime uses only the standard library. The browser CLI is installed locally from `package.json`.

## Quick start

```bash
git clone YOUR_REPOSITORY_URL
cd germany-housing-monitor
npm install
cp examples/config.example.json config.json
mkdir -p state
cp examples/contact_profile.example.json state/contact_profile.json
cp .env.example .env
python3 -m unittest discover -v
python3 scripts/preflight.py
python3 housing_workflow.py simulate
```

Edit `config.json` and `state/contact_profile.json` locally. Both are ignored by Git. The example profile uses fictional people and must not be used for a real application.

Before the first live discovery run, establish a silent baseline so existing listings do not flood notifications:

```bash
python3 monitor.py --baseline-only --no-jitter
```

If catch-up scanning is enabled, initialize its deeper boundary once:

```bash
python3 monitor.py --initialize-catch-up-baseline
```

## Configuration

Important environment overrides:

| Variable | Purpose |
| --- | --- |
| `HOUSING_MONITOR_CONFIG` | Path to the private runtime config |
| `HOUSING_MONITOR_STATE_DIR` | SQLite, outbox, logs, and locks |
| `HOUSING_MONITOR_BROWSER_PROFILE_DIR` | Dedicated browser profile |
| `HOUSING_MONITOR_CONTACT_PROFILE` | Private applicant profile |
| `HOUSING_MONITOR_ENV_FILE` | Ignored dotenv file |
| `HOUSING_MONITOR_CHROME_PATH` | Chrome executable |
| `AGENT_BROWSER_PATH` | `agent-browser` executable override |

Telegram credentials may be provided as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the process environment or ignored `.env`. Optional DeepSeek personalization uses `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, and `DEEPSEEK_BASE_URL`. Public listing text may be sent to that model only when personalization is explicitly enabled; applicant data is not included.

Email supports an existing Apple Mail OAuth account or SMTP with an app password stored in macOS Keychain. Do not put mail passwords in JSON or `.env`.

## Contact safety model

The workflow separates discovery, drafting, approval, and delivery:

1. A listing must pass the coarse room and verified warm-rent filter.
2. A local draft is hashed and stored as an immutable revision.
3. The operator must verify the configured layout, move-in, commute, nearby-amenity, and total-rent requirements.
4. Approval binds the listing ID, draft hash, evidence, approval message, approver, and expiry.
5. A revision revokes earlier approval.
6. Ambiguous delivery outcomes freeze instead of retrying automatically.

Even if configuration is changed to live mode, this repository still has no real provider transport. Adding one requires a separate security review and acceptance test.

## launchd

The checked-in plist files contain placeholders only. Render local copies without editing the templates:

```bash
python3 scripts/render_launchd.py \
  --python "$(command -v python3)" \
  --project-dir "$PWD"
```

Inspect the generated `.local.plist` files before loading them. The renderer never calls `launchctl`.

## Verification

```bash
python3 -m compileall -q .
python3 -m unittest discover -v
python3 scripts/preflight.py
```

The privacy preflight fails on private runtime directories, local configuration, personal email addresses, macOS user paths, common secret formats, databases, logs, large files, and unexpected binary files. Addresses under reserved example domains and explicitly allowlisted housing-platform service domains remain allowed in tests.

## Repository policy

Run the privacy preflight immediately before every public push. Never commit `state/`, `browser-profile/`, `backups/`, `.env`, `config.json`, exported emails, screenshots of authenticated pages, or application documents.

No open-source license has been selected yet. Until the repository owner adds one, normal copyright rules apply.
