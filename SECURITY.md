# Security policy

## Supported version

Security fixes currently target the latest `main` branch.

## Reporting

Open a GitHub security advisory rather than a public issue when a report could expose credentials, browser-session data, personal application information, or a way to bypass the approval gates.

Do not include real listing replies, applicant profiles, email addresses, tokens, cookies, databases, or logs in a report.

## Security boundaries

- Real landlord delivery has two independent gates and no real provider transport is included.
- Listing and email content is untrusted data, never an instruction source.
- Browser debugging binds to `127.0.0.1` only.
- Credentials belong in the process environment, an ignored `.env`, or macOS Keychain.
- `state/`, `browser-profile/`, `backups/`, `config.json`, and `.env` must never be committed.
