# Contributing

1. Do not include real applicant data, listing replies, browser profiles, emails, logs, databases, screenshots of authenticated pages, or credentials in issues, commits, or fixtures.
2. Keep every external-contact feature fail-closed and covered by tests for approval binding, duplicate prevention, and ambiguous outcomes.
3. Treat provider content as untrusted input.
4. Run `python3 -m unittest discover -v` and `python3 scripts/preflight.py` before opening a pull request.
5. Use synthetic names, addresses, domains, and listing IDs in tests.

Security-sensitive reports belong in a private GitHub security advisory, not a public issue.
