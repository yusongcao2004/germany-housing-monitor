# Privacy model

This repository contains code and synthetic examples only. A real installation creates or reads private local data:

- browser cookies and challenge state in `browser-profile/`;
- listing history, drafts, approvals, and replies in `state/`;
- applicant statements in `state/contact_profile.json`;
- notification credentials in environment variables, `.env`, or macOS Keychain;
- optional Apple Mail metadata when official saved-search ingestion is enabled.

All of these paths are ignored by Git. Run `python scripts/preflight.py` immediately before every public push. The preflight is deliberately strict and rejects personal email addresses, macOS home-directory paths, common token formats, runtime directories, databases, logs, and unexpected binary files. Reserved example domains and explicitly allowlisted housing-platform service domains are allowed for tests.

The project does not require applicants' identity documents, financial records, or browser profiles to be copied into the source tree. Keep those materials outside the repository.
