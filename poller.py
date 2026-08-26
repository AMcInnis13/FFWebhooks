#!/usr/bin/env python3
"""ESPN Fantasy Football -> Discord notifier.

A one-shot script: run, post whatever is new, exit. All persistence lives in
state.json, which the GitHub Actions workflow commits back to the repo.

Every secret arrives through an environment variable and is never written to
disk or printed. See CLAUDE.md section 6 for the full security contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
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


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

STATE_PATH = "state.json"
MAX_FINGERPRINTS = 300
STATE_KEYS = ("last_activity_ms", "seen_fingerprints", "posted_weeks", "posted_reminders")


def warn(message: str) -> None:
    """Write a diagnostic to stderr. Never pass a secret to this."""
    print(f"[notifier] {message}", file=sys.stderr)


def default_state() -> dict:
    """A fresh state object. Built per call -- never share a mutable default."""
    return {
        "last_activity_ms": 0,
        "seen_fingerprints": [],
        "posted_weeks": [],
        "posted_reminders": [],
    }


def _as_int(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_int_list(value) -> list[int]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def team_name(team) -> str:
    """Team display name, tolerating the empty string the library can emit.

    espn_api sets ``team = ''`` when it cannot resolve one (activity.py:37),
    so this must never assume a Team object.
    """
    name = getattr(team, "team_name", None)
    if not name:
        return ""
    return str(name).strip()


def player_name(player) -> str:
    """Player display name, tolerating the raw-id fallback.

    When lookup fails espn_api substitutes the bare targetId (an int) or the
    string 'Unknown' (activity.py:29, 57), so a Player object is not
    guaranteed.
    """
    return str(getattr(player, "name", player)).strip()


def fingerprint(activity) -> str:
    """A stable content hash for one Activity.

    Deliberately not Python's ``hash()``: that is salted per process by
    PYTHONHASHSEED, so it would produce a different value on every Actions
    run and defeat the whole point of persisting fingerprints. Actions are
    sorted so that ordering differences between runs do not change the hash.
    """
    rows = []
    for action in getattr(activity, "actions", []) or []:
        row = tuple(action)
        team = row[0] if len(row) > 0 else ""
        verb = row[1] if len(row) > 1 else ""
        player = row[2] if len(row) > 2 else ""
        bid = _as_int(row[3] if len(row) > 3 else 0)
        rows.append(f"{team_name(team)}|{verb}|{player_name(player)}|{bid}")

    payload = str(_as_int(getattr(activity, "date", 0))) + "\n" + "\n".join(sorted(rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def needs_bootstrap(path: str = STATE_PATH) -> bool:
    """True when state.json has never been written by a real run.

    Checked against the file rather than the loaded dict on purpose. A loaded
    state legitimately equals the default during preseason -- no activity yet,
    no completed weeks -- and comparing to the default would re-send the
    "notifier is online" message on every run until the season started.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return True
    if not raw.strip():
        return True
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return True
    if not isinstance(data, dict):
        return True
    return not any(key in data for key in STATE_KEYS)


def load_state(path: str = STATE_PATH) -> dict:
    """Read state.json, degrading to a clean state rather than crashing.

    Every failure mode -- missing, empty, truncated, malformed, or the wrong
    JSON type -- returns the default shape. That routes into the bootstrap
    path, which posts a single message; the alternative, crashing, takes the
    notifier offline silently. Unknown keys are preserved so a future version
    that adds one does not lose it when an older copy runs.
    """
    state = default_state()

    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return state
    except OSError as exc:
        warn(f"could not read {path} ({exc}); starting from a clean state")
        return state

    if not raw.strip():
        return state

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        warn(f"{path} is not valid JSON ({exc}); starting from a clean state")
        return state

    if not isinstance(data, dict):
        warn(f"{path} holds {type(data).__name__}, expected an object; starting clean")
        return state

    state.update(data)
    state["last_activity_ms"] = _as_int(data.get("last_activity_ms"), 0)
    state["seen_fingerprints"] = _as_str_list(data.get("seen_fingerprints"))
    state["posted_weeks"] = _as_int_list(data.get("posted_weeks"))
    state["posted_reminders"] = _as_str_list(data.get("posted_reminders"))
    return state


def save_state(state: Mapping, path: str = STATE_PATH) -> dict:
    """Write state.json atomically, trimming fingerprints to the newest N.

    Written with sorted keys and LF endings so the bytes are identical on
    Windows and on the Linux runner. The workflow skips its commit when the
    file is unchanged, and that check is only meaningful if serialization is
    deterministic.

    The write goes to a temp file and is renamed into place: a crash partway
    through would otherwise leave truncated JSON, which the next run would
    treat as a clean state and re-bootstrap.
    """
    payload = dict(state)
    payload["seen_fingerprints"] = list(payload.get("seen_fingerprints") or [])[-MAX_FINGERPRINTS:]

    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle_fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    return payload
