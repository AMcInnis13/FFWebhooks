#!/usr/bin/env python3
"""ESPN Fantasy Football -> Discord notifier.

A one-shot script: run, post whatever is new, exit. All persistence lives in
state.json, which the GitHub Actions workflow commits back to the repo.

Every secret arrives through an environment variable and is never written to
disk or printed. See CLAUDE.md section 6 for the full security contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

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

# Message categories, each routable to its own Discord channel.
CATEGORY_MAIN = "main"
CATEGORY_TRADES = "trades"
CATEGORY_ROSTER = "roster"
CATEGORY_RESULTS = "results"

# Lineup reminders, the bootstrap confirmation, and every error notice stay on
# CATEGORY_MAIN. Errors especially: a failure routed to a quiet side channel
# is barely better than no failure message at all.
CATEGORIES = (CATEGORY_MAIN, CATEGORY_TRADES, CATEGORY_ROSTER, CATEGORY_RESULTS)

CATEGORY_LABELS = {
    CATEGORY_MAIN: "main",
    CATEGORY_TRADES: "trades",
    CATEGORY_ROSTER: "roster moves",
    CATEGORY_RESULTS: "results",
}


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
    # Optional per-category destinations. Each falls back to webhook_url, so
    # an unconfigured install behaves exactly as it did before routing existed.
    webhook_url_trades: str = field(repr=False, default="")
    webhook_url_roster: str = field(repr=False, default="")

    def webhook_for(self, category: str) -> str:
        """The webhook URL a message category should be posted to.

        Unknown categories fall back to the main webhook rather than raising:
        a routing mistake should misfile a message, never drop it.
        """
        return {
            CATEGORY_TRADES: self.webhook_url_trades or self.webhook_url,
            CATEGORY_ROSTER: self.webhook_url_roster or self.webhook_url,
            CATEGORY_RESULTS: self.webhook_url_results or self.webhook_url,
        }.get(category, self.webhook_url)


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
    trades_url = (env.get("DISCORD_WEBHOOK_URL_TRADES") or "").strip()
    roster_url = (env.get("DISCORD_WEBHOOK_URL_ROSTER") or "").strip()

    return Config(
        league_id=_require_int(env["LEAGUE_ID"], "LEAGUE_ID", redact=True),
        league_year=_require_int(env["LEAGUE_YEAR"], "LEAGUE_YEAR"),
        espn_s2=env["ESPN_S2"].strip(),
        swid=normalize_swid(env["SWID"]),
        webhook_url=webhook_url,
        webhook_url_results=results_url or webhook_url,
        webhook_url_trades=trades_url or webhook_url,
        webhook_url_roster=roster_url or webhook_url,
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


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

DISCORD_CONTENT_LIMIT = 2000
DEFAULT_USERNAME = "Fantasy Notifier"
POST_DELAY_SECONDS = 0.75
REQUEST_TIMEOUT_SECONDS = 15
MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_RETRY_AFTER_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 60.0


class DiscordError(RuntimeError):
    """A Discord post failed.

    Messages raised from here must never contain the webhook URL -- it is a
    credential, and Actions logs are public on a public repo.
    """


def split_content(text: str, limit: int = DISCORD_CONTENT_LIMIT) -> list[str]:
    """Split text into chunks that fit Discord's content cap.

    Breaks on line boundaries so a trade block or a scoreboard never tears
    mid-line. A single line longer than the limit is hard-split, since there
    is nowhere better to cut. Trailing blank lines are dropped; interior ones
    are preserved because they carry formatting.
    """
    text = (text or "").rstrip("\n")
    if not text.strip():
        return []

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line

    if current:
        chunks.append(current)
    return chunks


class Discord:
    """Posts messages to one Discord webhook.

    ``session`` and ``sleep`` are injectable so tests never open a socket or
    actually wait. ``label`` only tags dry-run output so the transactions and
    results webhooks are distinguishable.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        username: str = DEFAULT_USERNAME,
        dry_run: bool = False,
        session=None,
        sleep=time.sleep,
        label: str = "",
    ):
        self.webhook_url = webhook_url
        self.username = username
        self.dry_run = dry_run
        self.label = label
        self._session = session
        self._sleep = sleep
        self._posted_any = False

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def post(self, content: str) -> int:
        """Post content, splitting if needed. Returns the number of POSTs sent."""
        chunks = split_content(content)
        for index, chunk in enumerate(chunks):
            self._post_chunk(chunk, index + 1, len(chunks))
        return len(chunks)

    def _post_chunk(self, content: str, index: int, total: int) -> None:
        payload = {
            "username": self.username,
            "content": content,
            # Without this a team called "@everyone", or a player name that
            # happens to match a role, would ping the whole server.
            "allowed_mentions": {"parse": []},
        }

        if self.dry_run:
            tag = f" {self.label}" if self.label else ""
            print(f"--- would POST to Discord{tag} [{index}/{total}, {len(content)} chars] ---")
            print(content)
            self._posted_any = True
            return

        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            if self._posted_any:
                self._sleep(POST_DELAY_SECONDS)

            failure = None
            try:
                response = self.session.post(
                    self.webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
                )
            except requests.RequestException as exc:
                # Keep only the exception's type name. requests puts the full
                # request URL -- the webhook credential -- into its message.
                failure = type(exc).__name__

            if failure is not None:
                # Raised outside the except block on purpose. `raise ... from
                # None` would still leave __context__ pointing at the original
                # exception, and anything that walks it would find the token.
                raise DiscordError(f"network error posting to Discord: {failure}")

            self._posted_any = True
            status = getattr(response, "status_code", 0)

            if 200 <= status < 300:
                return

            if status == 429:
                if attempt >= MAX_RATE_LIMIT_RETRIES:
                    raise DiscordError(
                        f"rate limited by Discord {attempt + 1} times in a row; giving up"
                    )
                wait = self._retry_after(response)
                warn(f"rate limited by Discord; retrying in {wait:.2f}s")
                self._sleep(wait)
                continue

            # Deliberately not response.raise_for_status(): its message
            # includes the request URL, i.e. the webhook credential.
            raise DiscordError(f"Discord returned HTTP {status}")

    @staticmethod
    def _retry_after(response) -> float:
        """Seconds to wait, from the JSON body, else the header, else a default."""
        value = None
        try:
            body = response.json()
            if isinstance(body, dict):
                value = body.get("retry_after")
        except (ValueError, AttributeError):
            pass

        if value is None:
            headers = getattr(response, "headers", None) or {}
            value = headers.get("Retry-After")

        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = DEFAULT_RETRY_AFTER_SECONDS

        # Clamped so a bogus value cannot park the job until the Actions
        # six-hour ceiling.
        return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


class DiscordRouter:
    """Maps message categories to the channel each should be posted to.

    One ``Discord`` instance per *distinct* URL, shared by every category
    pointing at the same channel. That sharing is the whole point: the
    inter-post throttle lives on the instance, so four separate instances
    aimed at one channel would each believe they were posting first and could
    burst straight into a 429.
    """

    def __init__(self, config: Config, *, dry_run: bool = False, session=None, sleep=None):
        self._by_url: dict[str, Discord] = {}
        self._by_category: dict[str, Discord] = {}

        for category in CATEGORIES:
            url = config.webhook_for(category)
            if url not in self._by_url:
                kwargs = {"dry_run": dry_run, "label": CATEGORY_LABELS.get(category, category)}
                if session is not None:
                    kwargs["session"] = session
                if sleep is not None:
                    kwargs["sleep"] = sleep
                self._by_url[url] = Discord(url, **kwargs)
            self._by_category[category] = self._by_url[url]

    def for_category(self, category: str) -> Discord:
        """The poster for a category; unknown categories fall back to main."""
        return self._by_category.get(category, self._by_category[CATEGORY_MAIN])

    def post(self, category: str, content: str) -> int:
        return self.for_category(category).post(content)

    @property
    def main(self) -> Discord:
        return self._by_category[CATEGORY_MAIN]

    @property
    def channel_count(self) -> int:
        """How many distinct channels are actually in use."""
        return len(self._by_url)

    def shares_channel(self, first: str, second: str) -> bool:
        return self.for_category(first) is self.for_category(second)


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

ACTION_TRADE_SENT = "TRADE_SENT"
ACTION_TRADE_RECEIVED = "TRADE_RECEIVED"
ACTION_FA_ADDED = "FA ADDED"
ACTION_WAIVER_ADDED = "WAIVER ADDED"
ACTION_DROPPED = "DROPPED"

UNKNOWN_TEAM_LABEL = "Unknown team"

# U+2212, a real minus sign -- it lines up with "+" in Discord's font where a
# hyphen sits too high.
DROP_MARK = "−"


def _normalized_actions(activity):
    """Flatten one Activity's action rows, absorbing the library's quirks.

    espn_api hands back 4-tuples but is loose about their contents: the team
    can be ``''``, the player can be a bare int id or the string 'Unknown',
    and the verb is 'UNKNOWN' for any message id missing from ACTIVITY_MAP.
    Everything downstream assumes clean strings, so it is all handled here.
    """
    for action in getattr(activity, "actions", []) or []:
        row = tuple(action)
        if not row:
            continue
        team = team_name(row[0]) or UNKNOWN_TEAM_LABEL
        verb = str(row[1]).strip().upper() if len(row) > 1 and row[1] is not None else ""
        player = player_name(row[2]) if len(row) > 2 else ""
        bid = _as_int(row[3] if len(row) > 3 else 0)
        if not player:
            continue
        yield team, verb, player, bid


def _append_unique(bucket: dict, order: list, key: str, value) -> None:
    """Record value under key, tracking first-seen order.

    The order list is checked independently of the bucket because adds and
    drops share one order list while keeping separate buckets -- tying the
    two together appends a team twice and splits its add/drop pair into two
    blocks, which is the exact thing this feature is meant to avoid.
    """
    if key not in bucket:
        bucket[key] = []
    if key not in order:
        order.append(key)
    if value not in bucket[key]:
        bucket[key].append(value)


def render_activity(activity) -> str:
    """Render one Activity as a single Discord message.

    One message per Activity, not per action: a trade reads as one block
    showing what each side received, and an add/drop pair from one team reads
    as one entry rather than two disconnected messages.

    Returns "" when there is nothing worth posting -- an activity made
    entirely of unrecognised message types, for example. Callers must still
    record such an activity as seen, or it will be reconsidered forever.
    """
    received: dict[str, list[str]] = {}
    sent: dict[str, list[str]] = {}
    adds: dict[str, list[tuple[str, int, bool]]] = {}
    drops: dict[str, list[str]] = {}
    trade_order: list[str] = []
    sent_order: list[str] = []
    roster_order: list[str] = []

    for team, verb, player, bid in _normalized_actions(activity):
        if verb == ACTION_TRADE_RECEIVED:
            _append_unique(received, trade_order, team, player)
        elif verb == ACTION_TRADE_SENT:
            _append_unique(sent, sent_order, team, player)
        elif verb in (ACTION_FA_ADDED, ACTION_WAIVER_ADDED):
            _append_unique(adds, roster_order, team, (player, bid, verb == ACTION_WAIVER_ADDED))
        elif verb == ACTION_DROPPED:
            _append_unique(drops, roster_order, team, player)
        # Anything else -- including the library's 'UNKNOWN' -- is skipped
        # rather than rendered, so a message id we don't recognise never
        # reaches the channel as noise.

    blocks: list[str] = []

    if received or sent:
        blocks.append(_render_trade(received, sent, trade_order, sent_order))

    for team in roster_order:
        blocks.append(_render_roster_moves(team, adds.get(team, []), drops.get(team, [])))

    return "\n".join(block for block in blocks if block)


def _render_trade(received, sent, trade_order, sent_order) -> str:
    lines = ["\U0001f501 Trade processed"]

    for team in trade_order:
        lines.append(f"  {team} gets: {', '.join(received[team])}")

    # A TRADE_SENT row is only paired with a TRADE_RECEIVED when espn_api
    # could resolve the receiving team (activity.py:32). When it could not,
    # say who gave the player up rather than dropping them from the message.
    claimed = {player for players in received.values() for player in players}
    for team in sent_order:
        orphaned = [player for player in sent[team] if player not in claimed]
        if orphaned:
            lines.append(f"  {team} gives up: {', '.join(orphaned)}")

    return "\n".join(lines)


def _render_roster_moves(team: str, adds, drops) -> str:
    if not adds and not drops:
        return ""

    header = "\U0001f4e5" if adds else "\U0001f4e4"
    lines = [f"{header} {team}"]

    for player, bid, is_waiver in adds:
        if is_waiver and bid > 0:
            lines.append(f"  + {player} (${bid} waiver)")
        elif is_waiver:
            lines.append(f"  + {player} (waiver)")
        else:
            lines.append(f"  + {player}")

    for player in drops:
        lines.append(f"  {DROP_MARK} {player}")

    return "\n".join(lines)


RECENT_ACTIVITY_SIZE = 50


def process_transactions(league, state: dict, discord: Discord) -> int:
    """Post activities newer than the watermark, oldest first.

    Returns the number of messages sent.

    Two dedup mechanisms, and both are load-bearing:

    * The watermark on ``Activity.date`` is the cheap primary filter.
    * Fingerprints catch what the watermark alone would drop. The date
      comparison is ``>=``, not ``>``, precisely so an activity sharing a
      millisecond with one already posted is reconsidered rather than
      skipped forever; the fingerprint is what stops it posting twice.

    ``recent_activity`` returns newest-first, so the list is reversed before
    posting -- otherwise the channel would read backwards. The watermark and
    fingerprint are recorded only after a post succeeds, so a mid-run failure
    leaves the remaining activities to retry on the next run rather than
    being silently skipped.
    """
    activities = league.recent_activity(size=RECENT_ACTIVITY_SIZE) or []

    # Reverse first, then stable-sort: activities sharing a timestamp keep
    # their reversed (oldest-first) relative order.
    ordered = list(reversed(list(activities)))
    ordered.sort(key=lambda activity: _as_int(getattr(activity, "date", 0)))

    watermark = _as_int(state.get("last_activity_ms"), 0)
    seen = list(state.get("seen_fingerprints") or [])
    seen_set = set(seen)
    posted = 0

    for activity in ordered:
        date = _as_int(getattr(activity, "date", 0))
        if date < watermark:
            continue

        mark = fingerprint(activity)
        if mark in seen_set:
            continue

        message = render_activity(activity)
        if message:
            discord.post(message)
            posted += 1

        # Recorded even when nothing rendered. An activity made entirely of
        # message types we don't recognise has nothing to say, but leaving it
        # unmarked means reconsidering it on every run forever.
        seen.append(mark)
        seen_set.add(mark)
        watermark = max(watermark, date)

        state["last_activity_ms"] = watermark
        state["seen_fingerprints"] = seen

    state["last_activity_ms"] = watermark
    state["seen_fingerprints"] = seen
    return posted


# --------------------------------------------------------------------------
# Weekly results
# --------------------------------------------------------------------------

BYE_LABEL = "Unknown team"


def _as_float(value, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _format_score(value) -> str:
    return f"{_as_float(value):.1f}"


def _is_missing_side(team) -> bool:
    """True when a Matchup side is absent rather than a real team.

    A bye comes back with the team id and score as 0 rather than as a Team
    object, so falsy scalars mean "no opponent". A real Team instance is
    always truthy, even when its name is blank.
    """
    if team is None:
        return True
    if isinstance(team, bool):
        return not team
    if isinstance(team, (int, float)):
        return not team
    if isinstance(team, str):
        return not team.strip()
    return False


def render_week(week: int, matchups) -> str:
    """Render one completed week's scoreboard as a single message.

    Returns "" when there is nothing to show.
    """
    matchups = list(matchups or [])
    if not matchups:
        return ""

    lines: list[str] = []
    scores: list[tuple[str, float]] = []
    playoff_flags: list[bool] = []

    for matchup in matchups:
        home = getattr(matchup, "home_team", None)
        away = getattr(matchup, "away_team", None)
        home_score = _as_float(getattr(matchup, "home_score", 0))
        away_score = _as_float(getattr(matchup, "away_score", 0))
        is_playoff = bool(getattr(matchup, "is_playoff", False))
        playoff_flags.append(is_playoff)

        home_label = team_name(home) or BYE_LABEL
        away_label = team_name(away) or BYE_LABEL

        if _is_missing_side(away):
            # A bye is not a result, so it stays out of the high/low race.
            lines.append((f"\U0001f4a4 {home_label} {_format_score(home_score)} (bye)", is_playoff))
            continue
        if _is_missing_side(home):
            lines.append((f"\U0001f4a4 {away_label} {_format_score(away_score)} (bye)", is_playoff))
            continue

        scores.append((home_label, home_score))
        scores.append((away_label, away_score))

        if home_score == away_score:
            line = (
                f"\U0001f91d {home_label} {_format_score(home_score)} — "
                f"{away_label} {_format_score(away_score)} (tie)"
            )
        elif home_score > away_score:
            line = (
                f"✅ {home_label} {_format_score(home_score)} — "
                f"{away_label} {_format_score(away_score)}"
            )
        else:
            line = (
                f"✅ {away_label} {_format_score(away_score)} — "
                f"{home_label} {_format_score(home_score)}"
            )
        lines.append((line, is_playoff))

    all_playoff = bool(playoff_flags) and all(playoff_flags)
    any_playoff = any(playoff_flags)

    if all_playoff:
        header = f"\U0001f3c6 Week {week} Playoff Results"
    else:
        header = f"\U0001f3c8 Week {week} Results"

    body = []
    for line, is_playoff in lines:
        # Tag individual games only in a mixed week; repeating "(playoff)" on
        # every line of an all-playoff week is noise the header already covers.
        if is_playoff and any_playoff and not all_playoff:
            body.append(f"  {line} (playoff)")
        else:
            body.append(f"  {line}")

    rendered = [header] + body

    if scores:
        best = max(score for _, score in scores)
        worst = min(score for _, score in scores)
        high_teams = ", ".join(name for name, score in scores if score == best)
        low_teams = ", ".join(name for name, score in scores if score == worst)
        rendered.append("")
        rendered.append(f"  \U0001f4c8 High: {high_teams} — {_format_score(best)}")
        rendered.append(f"  \U0001f4c9 Low: {low_teams} — {_format_score(worst)}")

    return "\n".join(rendered)


def process_results(league, state: dict, discord: Discord) -> int:
    """Post results for every completed week not already announced.

    A week counts as complete when ``week < league.current_week``. Detecting
    "all games final" from player states was considered and rejected as not
    worth the complexity.

    Returns the number of weeks posted.
    """
    current_week = _as_int(getattr(league, "current_week", 0))
    posted_weeks = list(state.get("posted_weeks") or [])
    posted_set = set(posted_weeks)
    posted = 0

    for week in range(1, current_week):
        if week in posted_set:
            continue

        message = render_week(week, league.scoreboard(week))
        if not message:
            # Deliberately NOT marked as posted. An empty scoreboard is more
            # likely a transient ESPN hiccup than a week that truly had no
            # games, and marking it would skip those results permanently.
            # Re-checking costs one call per run and self-heals.
            warn(f"week {week} returned no matchups; will retry next run")
            continue

        discord.post(message)
        posted += 1

        posted_weeks.append(week)
        posted_set.add(week)
        state["posted_weeks"] = posted_weeks

    state["posted_weeks"] = posted_weeks
    return posted


# --------------------------------------------------------------------------
# Lineup lock reminders
# --------------------------------------------------------------------------

# weekday() is Monday=0 .. Sunday=6.
THURSDAY = 3
SUNDAY = 6

REMINDER_SLOTS = {
    "thursday": (THURSDAY, (19, 15), "the Thursday night game"),
    "sunday": (SUNDAY, (12, 0), "the Sunday early games"),
}

REMINDER_LEAD_MINUTES = 30

# The window opens a little earlier than the nominal 30-minute lead so a
# 20-minute cron cannot step over it. A 30-minute window would be cutting it
# fine: GitHub's scheduler is best-effort and routinely runs several minutes
# late, which can stretch the real gap between runs past 30 minutes. 35 gives
# 15 minutes of slack while still closing at kickoff -- the reminder is
# useless once lineups have locked.
REMINDER_WINDOW_MINUTES = 35

# The league year is "active" across these weeks. Fantasy playoffs land
# inside NFL weeks 1-18, so this covers the regular season and the postseason
# without needing to know the league's playoff configuration.
ACTIVE_WEEK_MIN = 1
ACTIVE_WEEK_MAX = 18


class TimezoneUnavailable(RuntimeError):
    """The tz database could not resolve the configured TIMEZONE.

    Bare Windows ships no tz database. This must never be swallowed: silently
    skipping reminders forever is exactly the quiet failure this project is
    built to avoid.
    """


def local_now(timezone_name: str, now: datetime | None = None) -> datetime:
    """Current time in the configured zone.

    ``now`` is injectable so tests can freeze the clock. Computing this at
    runtime rather than baking UTC cron times is the whole point: Central
    shifts under DST mid-season, and a fixed offset would drift an hour in
    November without anything failing loudly.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise TimezoneUnavailable(
            f"could not resolve timezone {timezone_name!r}: {type(exc).__name__}. "
            "On Windows this usually means the 'tzdata' package is missing."
        ) from None

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    return now.astimezone(zone)


def due_reminder(when: datetime):
    """Return (key, slot, minutes_remaining) if inside a reminder window.

    Returns None otherwise.
    """
    for slot, (weekday, (hour, minute), _) in REMINDER_SLOTS.items():
        if when.weekday() != weekday:
            continue

        # Safe across DST: neither kickoff time falls in the transition hour.
        kickoff = when.replace(hour=hour, minute=minute, second=0, microsecond=0)
        opens = kickoff - timedelta(minutes=REMINDER_WINDOW_MINUTES)

        if opens <= when < kickoff:
            remaining = int((kickoff - when).total_seconds() // 60)
            return f"{when.date().isoformat()}-{slot}", slot, remaining

    return None


def render_reminder(slot: str, minutes_remaining: int) -> str:
    _, _, description = REMINDER_SLOTS[slot]
    unit = "minute" if minutes_remaining == 1 else "minutes"
    return f"⏰ Lineups lock in {minutes_remaining} {unit} for {description}."


def process_reminders(
    config: Config,
    state: dict,
    discord: Discord,
    *,
    current_week=None,
    now: datetime | None = None,
) -> int:
    """Post a lineup lock reminder if one is due. Returns 1 or 0.

    Folded into the same run as everything else on purpose: a second workflow
    with hardcoded UTC cron times would silently drift by an hour when
    Central changes offset in November.
    """
    if not config.lineup_reminders:
        return 0

    week = _as_int(current_week, 0)
    if not (ACTIVE_WEEK_MIN <= week <= ACTIVE_WEEK_MAX):
        return 0

    when = local_now(config.timezone, now)
    due = due_reminder(when)
    if due is None:
        return 0

    key, slot, remaining = due
    posted_reminders = list(state.get("posted_reminders") or [])
    if key in posted_reminders:
        return 0

    discord.post(render_reminder(slot, remaining))

    posted_reminders.append(key)
    state["posted_reminders"] = posted_reminders
    return 1


# --------------------------------------------------------------------------
# First-run bootstrap
# --------------------------------------------------------------------------


def bootstrap(league, state: dict, discord: Discord) -> int:
    """Seed state from the league's current position without posting backlog.

    The single most dangerous moment in this project's life: on a fresh
    state.json the naive path would replay every activity and every finished
    week into the channel at once. Instead the current position is recorded
    as already-seen and one confirmation message is sent.

    Storing fingerprints here is required, not belt-and-braces. The watermark
    comparison in process_transactions is ``>=`` (see T-006), so the newest
    activity is reconsidered on the very next run -- without its fingerprint
    on file it would post as though it were new.

    Returns the number of messages posted (always 1).
    """
    activities = list(league.recent_activity(size=RECENT_ACTIVITY_SIZE) or [])
    current_week = _as_int(getattr(league, "current_week", 0))

    watermark = max(
        (_as_int(getattr(activity, "date", 0)) for activity in activities),
        default=0,
    )
    fingerprints = [fingerprint(activity) for activity in activities]
    completed_weeks = list(range(1, current_week))

    state["last_activity_ms"] = watermark
    state["seen_fingerprints"] = fingerprints
    state["posted_weeks"] = completed_weeks
    # posted_reminders is deliberately left alone. A reminder due right now
    # is current news, not backlog, so the next run may legitimately send it.

    discord.post(render_bootstrap_message(len(activities), len(completed_weeks)))
    return 1


def render_bootstrap_message(activity_count: int, week_count: int) -> str:
    weeks = "week" if week_count == 1 else "weeks"
    entries = "transaction" if activity_count == 1 else "transactions"
    return (
        "✅ Fantasy notifier is online.\n"
        "Watching transactions, weekly results, and lineup lock reminders.\n"
        f"Starting from now: {activity_count} existing {entries} and "
        f"{week_count} completed {weeks} marked as already seen, "
        "so no backlog will be posted."
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILURE = 1

ERROR_COOLDOWN_MS = 24 * 60 * 60 * 1000

# Matched by class name rather than imported: these live in espn_api's
# internals, and a name check keeps a library reshuffle from turning an auth
# failure into an unhandled crash.
AUTH_ERROR_NAMES = frozenset(
    {"ESPNAccessDenied", "ESPNInvalidLeague", "ESPNUnknownError"}
)

ERROR_MESSAGES = {
    "auth": (
        "⚠️ ESPN rejected the notifier's credentials.\n"
        "The ESPN_S2 and SWID cookies have likely expired. Re-pull them from a "
        "browser and update the repository secrets — updates are paused until then."
    ),
    "league": (
        "⚠️ The notifier could not reach ESPN.\n"
        "It will retry on the next scheduled run."
    ),
    "timezone": (
        "⚠️ The notifier could not resolve its configured timezone, so lineup "
        "reminders are paused.\nCheck that TIMEZONE names a real zone."
    ),
    "transactions": (
        "⚠️ The notifier hit an error while posting transactions.\n"
        "Results and reminders are unaffected; it will retry on the next run."
    ),
    "results": (
        "⚠️ The notifier hit an error while posting weekly results.\n"
        "Transactions and reminders are unaffected; it will retry on the next run."
    ),
    "reminders": (
        "⚠️ The notifier hit an error while checking lineup reminders.\n"
        "Transactions and results are unaffected; it will retry on the next run."
    ),
    "bootstrap": (
        "⚠️ The notifier could not complete its first-run setup.\n"
        "It will try again on the next scheduled run."
    ),
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_auth_error(exc: BaseException) -> bool:
    """Whether an exception means ESPN refused our credentials.

    Note the message text is inspected only for classification and is never
    logged or posted: espn_api builds these strings as
    f"League {league_id} cannot be accessed ..." -- the raw text embeds the
    league id.
    """
    if type(exc).__name__ in AUTH_ERROR_NAMES:
        return True
    text = str(exc).lower()
    return any(token in text for token in ("401", "403", "access denied", "cookie"))


def notify_error(kind: str, state: dict, discord: Discord, *, now_ms=None) -> bool:
    """Post a rate-limited error notice. Returns True if one was sent.

    Rate limited to once per day per kind: a persistent failure on a
    20-minute cron would otherwise post 72 times a day, and a channel full
    of identical warnings gets muted, which is just silence with extra steps.
    """
    now_ms = _now_ms() if now_ms is None else now_ms
    notices = dict(state.get("error_notices") or {})

    if now_ms - _as_int(notices.get(kind), 0) < ERROR_COOLDOWN_MS:
        return False

    try:
        discord.post(ERROR_MESSAGES.get(kind, ERROR_MESSAGES["league"]))
    except DiscordError as exc:
        # If Discord itself is the problem there is nowhere left to report to.
        # Say so on stderr and let the run finish rather than crashing here.
        warn(f"could not report {kind} failure: {exc}")
        return False

    notices[kind] = now_ms
    state["error_notices"] = notices
    return True


def build_league(config: Config):
    """Connect to ESPN.

    Imported lazily so that importing this module -- as the tests do -- does
    not require espn_api to be importable.
    """
    from espn_api.football import League

    return League(
        league_id=config.league_id,
        year=config.league_year,
        espn_s2=config.espn_s2,
        swid=config.swid,
    )


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------
#
# Fixture data lives here rather than in tests/ because `python poller.py
# --dry-run` has to work from a bare checkout with no env vars and no test
# package on the path. It is the one place production code carries fixtures,
# and it is deliberate.


class _FixtureTeam:
    def __init__(self, team_name):
        self.team_name = team_name


class _FixturePlayer:
    def __init__(self, name):
        self.name = name


class _FixtureActivity:
    def __init__(self, date, actions):
        self.date = date
        self.actions = actions


class _FixtureMatchup:
    def __init__(self, home_team, home_score, away_team, away_score, is_playoff=False):
        self.home_team = home_team
        self.home_score = home_score
        self.away_team = away_team
        self.away_score = away_score
        self.is_playoff = is_playoff


class _FixtureLeague:
    """A league exercising every rendering branch worth eyeballing."""

    current_week = 4

    def __init__(self):
        wolves = _FixtureTeam("Waiver Wolves")
        sharks = _FixtureTeam("Sofa Sharks")
        badgers = _FixtureTeam("Backup Badgers")

        self.activities = [
            # A three-player, two-team trade.
            _FixtureActivity(
                1757000000000,
                [
                    (wolves, "TRADE_SENT", _FixturePlayer("Amari Cooper"), 0),
                    (sharks, "TRADE_RECEIVED", _FixturePlayer("Amari Cooper"), 0),
                    (wolves, "TRADE_SENT", _FixturePlayer("Rhamondre Stevenson"), 0),
                    (sharks, "TRADE_RECEIVED", _FixturePlayer("Rhamondre Stevenson"), 0),
                    (sharks, "TRADE_SENT", _FixturePlayer("Jaylen Waddle"), 0),
                    (wolves, "TRADE_RECEIVED", _FixturePlayer("Jaylen Waddle"), 0),
                ],
            ),
            # A waiver claim paired with the corresponding drop.
            _FixtureActivity(
                1757000100000,
                [
                    (wolves, "WAIVER ADDED", _FixturePlayer("Marvin Waivers Jr."), 42),
                    (wolves, "DROPPED", _FixturePlayer("Cordarrelle Patterson"), 0),
                ],
            ),
            # A free agent add, plus a message type the library could not
            # classify -- it must be skipped, not printed.
            _FixtureActivity(
                1757000200000,
                [
                    (badgers, "FA ADDED", _FixturePlayer("Kimani Vidal"), 0),
                    (badgers, "UNKNOWN", _FixturePlayer("Should Not Appear"), 0),
                ],
            ),
        ]

        self.weeks = {
            1: [_FixtureMatchup(wolves, 128.4, sharks, 96.2)],
            2: [_FixtureMatchup(wolves, 101.0, badgers, 101.0)],
            3: [
                _FixtureMatchup(wolves, 134.8, sharks, 118.6),
                _FixtureMatchup(badgers, 88.0, 0, 0),
            ],
        }

    def recent_activity(self, size=25):
        return sorted(self.activities, key=lambda a: a.date, reverse=True)[:size]

    def scoreboard(self, week):
        return self.weeks.get(week, [])


def _fixture_config() -> Config:
    """Config for a dry run. No env vars required, no real values."""
    return Config(
        league_id=1234567,
        league_year=2026,
        espn_s2="DRY-RUN",
        swid="{DRY-RUN}",
        webhook_url="https://example.invalid/hook",
        webhook_url_results="https://example.invalid/hook",
        timezone=DEFAULT_TIMEZONE,
        lineup_reminders=True,
    )


def _force_utf8_stdout() -> None:
    """Make stdout accept emoji even when the locale codepage will not.

    On Windows a redirected stdout defaults to cp1252, and printing the
    messages this notifier builds raises UnicodeEncodeError on the first
    emoji. Only affects the dry-run printer -- the live path posts JSON over
    HTTP and never touches the console encoding.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass


def run_dry_run(now: datetime | None = None) -> int:
    """Run the real pipeline against fixture data. No network, no state write."""
    _force_utf8_stdout()

    config = _fixture_config()
    league = _FixtureLeague()
    discord = Discord(
        config.webhook_url, dry_run=True, label="dry-run", session=object(), sleep=lambda _: None
    )

    # 16:30 UTC on Sunday 13 Sep 2026 is 11:30 CDT: inside the Sunday
    # reminder window, so that section actually renders.
    if now is None:
        now = datetime(2026, 9, 13, 16, 30, tzinfo=timezone.utc)

    print("=== DRY RUN — no network calls, no state written ===")
    sent = 0

    print("\n--- first run (bootstrap) ---")
    sent += bootstrap(league, default_state(), discord)

    # A state that has already been bootstrapped, so the feature areas have
    # something to say.
    state = default_state()
    state["posted_weeks"] = [1, 2]

    print("\n--- transactions ---")
    sent += process_transactions(league, state, discord)

    print("\n--- weekly results ---")
    sent += process_results(league, state, discord)

    print("\n--- lineup reminders ---")
    try:
        sent += process_reminders(
            config, state, discord, current_week=league.current_week, now=now
        )
    except TimezoneUnavailable as exc:
        # Demonstrating the failure is the useful outcome here; a dry run
        # should not fail because the local box lacks a tz database.
        print(f"(skipped: {exc})")

    print(f"\n=== DRY RUN COMPLETE — {sent} messages would have been sent ===")
    return EXIT_OK


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Post ESPN fantasy football updates to a Discord webhook."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline against fixture data: no network, no state write.",
    )
    return parser.parse_args(argv)


def main(
    argv=None,
    *,
    env=None,
    league=None,
    state_path: str = STATE_PATH,
    discord=None,
    results_discord=None,
    now=None,
    now_ms=None,
) -> int:
    """Entry point. Returns a process exit code.

    Every keyword is injectable so tests can run the real orchestration
    without a network, a clock, or a state file.
    """
    args = _parse_args(argv)

    # A real `--dry-run` from the CLI uses fixture everything, so it works
    # from a bare checkout with no secrets set. Tests inject a league and go
    # through the normal path instead.
    if args.dry_run and league is None and env is None:
        return run_dry_run(now=now)

    try:
        config = load_config(env)
    except ConfigError as exc:
        # No validated webhook yet, so stderr is the only place to complain.
        warn(str(exc))
        return EXIT_FAILURE

    state = load_state(state_path)

    if discord is None:
        discord = Discord(config.webhook_url, dry_run=args.dry_run, label="transactions")
    if results_discord is None:
        # Share the instance when both webhooks are the same URL so the
        # inter-post throttle applies across features too.
        results_discord = (
            discord
            if config.webhook_url_results == config.webhook_url
            else Discord(
                config.webhook_url_results, dry_run=args.dry_run, label="results"
            )
        )

    failures: list[str] = []

    if league is None:
        try:
            league = build_league(config)
        except Exception as exc:
            kind = "auth" if is_auth_error(exc) else "league"
            # Only the type name: the raw message embeds the league id.
            warn(f"could not connect to ESPN ({type(exc).__name__}); reporting as {kind}")
            notify_error(kind, state, discord, now_ms=now_ms)
            if not args.dry_run:
                save_state(state, state_path)
            return EXIT_FAILURE

    if needs_bootstrap(state_path):
        try:
            bootstrap(league, state, discord)
        except Exception as exc:
            warn(f"bootstrap failed ({type(exc).__name__})")
            notify_error("bootstrap", state, discord, now_ms=now_ms)
            failures.append("bootstrap")
    else:
        # Each feature is isolated: one raising must never suppress the others.
        try:
            process_transactions(league, state, discord)
        except Exception as exc:
            warn(f"transactions failed ({type(exc).__name__})")
            notify_error("transactions", state, discord, now_ms=now_ms)
            failures.append("transactions")

        try:
            process_results(league, state, results_discord)
        except Exception as exc:
            warn(f"results failed ({type(exc).__name__})")
            notify_error("results", state, discord, now_ms=now_ms)
            failures.append("results")

        try:
            process_reminders(
                config,
                state,
                discord,
                current_week=getattr(league, "current_week", 0),
                now=now,
            )
        except TimezoneUnavailable as exc:
            # Raised rather than posted by design (T-008). Surfacing it here
            # keeps reminders from failing silently for a whole season.
            warn(str(exc))
            notify_error("timezone", state, discord, now_ms=now_ms)
            failures.append("reminders")
        except Exception as exc:
            warn(f"reminders failed ({type(exc).__name__})")
            notify_error("reminders", state, discord, now_ms=now_ms)
            failures.append("reminders")

    if not args.dry_run:
        save_state(state, state_path)

    if failures:
        warn(f"run completed with failures: {', '.join(failures)}")
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
