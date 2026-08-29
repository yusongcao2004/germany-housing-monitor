# Release checklist

Verified on 2026-08-29 with Python 3.11.

## Completed

- [x] Built this release tree from source files only; no runtime state was copied.
- [x] Removed hard-coded applicant names, personal statements, account identifiers, and user-home paths.
- [x] Replaced the original city, institution, budget, move-in window, and layout profile with fictional, generic examples.
- [x] Replaced the local applicant profile and runtime configuration with fictional examples.
- [x] Disabled Telegram, email, official-mail ingestion, model personalization, and contact drafting by default.
- [x] Kept real landlord submission out of the public release.
- [x] Added Git exclusions for secrets, browser profiles, databases, logs, backups, and generated launchd files.
- [x] Added a privacy preflight and a GitHub Actions check.
- [x] Generated `package-lock.json`; `npm` reported zero known vulnerabilities.
- [x] Compiled all Python files without warnings promoted to errors.
- [x] Passed all 90 unit tests.
- [x] Passed the isolated workflow simulation without external writes.
- [x] Parsed both checked-in and rendered launchd plists successfully.
- [x] Passed the final privacy scan with zero findings.
- [x] Compared 40 sensitive values from the private source configuration/profile against the exact staged Git tree; zero overlaps remained.

## Repository-owner choices

- [x] Repository name: `germany-housing-monitor`.
- [x] Visibility: public, only after the private CI gate passes.
- [x] License: MIT, with only the public GitHub handle in the copyright notice.

Only the files covered by this checklist belong in the first public release.
