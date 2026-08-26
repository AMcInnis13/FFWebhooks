#!/usr/bin/env python3
"""ESPN Fantasy Football -> Discord notifier.

A one-shot script: run, post whatever is new, exit. All persistence lives in
state.json, which the GitHub Actions workflow commits back to the repo.

Every secret arrives through an environment variable and is never written to
disk or printed. See CLAUDE.md section 6 for the full security contract.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_TIMEZONE = "America/Chicago"

REQUIRED_VARS = (
    "LEAGUE_ID",
    "LEAGUE_YEAR",
    "ESPN_S2",
    "SWID",
    "DISCORD_WEBHOOK_URL",
)

_TRUE_VALUES = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "n", "off"})


class ConfigError(RuntimeError):
    """Environment configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration.

    Secret-bearing fields carry ``repr=False`` so that logging or a stray
    traceback can never print a cookie, a webhook URL, or the league id.
    GitHub Actions logs are public on a public repo, so the default dataclass
    repr would be a real leak. ``repr(Config)`` shows only the safe fields.
    """

    league_id: int = field(repr=False)
    league_year: int
    espn_s2: str = field(repr=False)
    swid: str = field(repr=False)
    webhook_url: str = field(repr=False)
    webhook_url_results: str = field(repr=False)
    timezone: str
    lineup_reminders: bool


def normalize_swid(raw: str) -> str:
    """Return SWID wrapped in braces, accepting input with or without them.

    ESPN's cookie is normally ``{AAAA-BBBB-...}`` but browsers and copy-paste
    routinely drop the braces, and the API rejects the bare form.
    """
    inner = raw.strip().strip("{}").strip()
    if not inner:
        raise ConfigError("SWID is empty; expected a value like {AAAA-BBBB-CCCC}")
    return "{" + inner + "}"


def parse_bool(raw: str | None, default: bool, *, var_name: str) -> bool:
    """Parse a boolean env var, raising on values that are neither.

    An unrecognised value is an error rather than a silent fallback: quietly
    treating LINEUP_REMINDERS="ture" as the default is exactly how a feature
    goes missing for a whole season without anyone noticing.
    """
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"{var_name} must be one of true/false/1/0/yes/no (got {raw!r})"
    )


def _require_int(value: str, var_name: str, *, redact: bool = False) -> int:
    """Coerce an env var to int, with an error that names the variable."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        shown = "<redacted>" if redact else repr(value)
        raise ConfigError(f"{var_name} must be an integer (got {shown})") from None


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a Config from the environment.

    ``env`` is injectable so tests never touch os.environ. Missing variables
    are reported together rather than one per run -- a five-round-trip
    debugging loop against a cron job is a miserable way to find a typo.

    TIMEZONE is deliberately NOT validated here. Resolving it needs the tz
    database, which is absent on bare Windows, and a failure at config time
    would take down transactions and results too. The reminders feature owns
    that check so it can fail alone (see T-008).
    """
    env = os.environ if env is None else env

    missing = [name for name in REQUIRED_VARS if not (env.get(name) or "").strip()]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set them as GitHub Actions repository secrets."
        )

    webhook_url = env["DISCORD_WEBHOOK_URL"].strip()
    results_url = (env.get("DISCORD_WEBHOOK_URL_RESULTS") or "").strip()

    return Config(
        league_id=_require_int(env["LEAGUE_ID"], "LEAGUE_ID", redact=True),
        league_year=_require_int(env["LEAGUE_YEAR"], "LEAGUE_YEAR"),
        espn_s2=env["ESPN_S2"].strip(),
        swid=normalize_swid(env["SWID"]),
        webhook_url=webhook_url,
        webhook_url_results=results_url or webhook_url,
        timezone=(env.get("TIMEZONE") or "").strip() or DEFAULT_TIMEZONE,
        lineup_reminders=parse_bool(
            env.get("LINEUP_REMINDERS"), True, var_name="LINEUP_REMINDERS"
        ),
    )
