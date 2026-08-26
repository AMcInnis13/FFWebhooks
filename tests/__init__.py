"""Test package for the ESPN Fantasy Football -> Discord notifier.

Tests use the standard library's unittest so the runtime dependency list stays
at exactly espn_api and requests.

Run the full suite:      python -m unittest discover -s tests -v
Run a single module:     python -m unittest tests.test_state -v

Fixtures must use obviously-fake credentials and ids -- never a real league id,
cookie, or Discord webhook URL. See CLAUDE.md section 6.
"""
