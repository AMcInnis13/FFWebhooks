"""Tests for main() orchestration (T-010).

Two guarantees dominate: the three feature areas fail independently, and a
run never goes quiet -- a failure posts a rate-limited notice and exits
non-zero so the Actions run shows red.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone

from poller import (
    ERROR_COOLDOWN_MS,
    EXIT_FAILURE,
    EXIT_OK,
    TimezoneUnavailable,
    is_auth_error,
    load_state,
    main,
    notify_error,
    save_state,
)

BASE_MS = 1756200000000
NOW_MS = 1760000000000

UNSET = object()

FAKE_ENV = {
    "LEAGUE_ID": "1234567",
    "LEAGUE_YEAR": "2026",
    "ESPN_S2": "FAKE_S2",
    "SWID": "{FAKE-SWID}",
    "DISCORD_WEBHOOK_URL": "https://example.invalid/hook",
}


class FakeTeam:
    def __init__(self, name):
        self.team_name = name


class FakePlayer:
    def __init__(self, name):
        self.name = name


class FakeActivity:
    def __init__(self, date, actions):
        self.date = date
        self.actions = actions


class FakeMatchup:
    def __init__(self, home, home_score, away, away_score, is_playoff=False):
        self.home_team = home
        self.home_score = home_score
        self.away_team = away
        self.away_score = away_score
        self.is_playoff = is_playoff


def an_activity(index=0):
    return FakeActivity(
        BASE_MS + index,
        [(FakeTeam("Team A"), "FA ADDED", FakePlayer(f"Player {index}"), 0)],
    )


class FakeLeague:
    def __init__(self, current_week=3, activities=None, raise_on=None):
        self.current_week = current_week
        self.activities = activities if activities is not None else [an_activity(0)]
        self.raise_on = raise_on or set()

    def recent_activity(self, size=25):
        if "activity" in self.raise_on:
            raise RuntimeError("ESPN activity endpoint blew up")
        return sorted(self.activities, key=lambda a: a.date, reverse=True)

    def scoreboard(self, week):
        if "scoreboard" in self.raise_on:
            raise RuntimeError("ESPN scoreboard endpoint blew up")
        return [FakeMatchup(FakeTeam("Home"), 100.0 + week, FakeTeam("Away"), 90.0)]


class ESPNAccessDenied(Exception):
    """Mirrors espn_api's real class, including the league id in its text."""


class RecordingDiscord:
    def __init__(self):
        self.messages = []

    def post(self, content):
        self.messages.append(content)
        return 1


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "state.json")
        self.discord = RecordingDiscord()

    def seed_state(self, **overrides):
        """A non-bootstrap state, so main() runs the feature areas."""
        state = {
            "last_activity_ms": BASE_MS + 500,
            "seen_fingerprints": [],
            "posted_weeks": [1, 2],
            "posted_reminders": [],
        }
        state.update(overrides)
        save_state(state, self.path)
        return state

    def run_main(self, league=UNSET, env=UNSET, argv=None, **kwargs):
        # Sentinels, not None-defaults: passing league=None must genuinely
        # mean "let main() build one", and env={} must genuinely mean empty.
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                argv or [],
                env=FAKE_ENV if env is UNSET else env,
                league=FakeLeague() if league is UNSET else league,
                state_path=self.path,
                discord=self.discord,
                now_ms=NOW_MS,
                **kwargs,
            )
        self.stderr = stderr.getvalue()
        return code


class TestHappyPath(MainTestCase):
    def test_bootstrap_run_posts_once_and_succeeds(self):
        self.assertEqual(self.run_main(), EXIT_OK)
        self.assertEqual(len(self.discord.messages), 1)
        self.assertIn("online", self.discord.messages[0].lower())

    def test_state_is_written(self):
        self.run_main()
        self.assertTrue(os.path.exists(self.path))
        self.assertGreater(load_state(self.path)["last_activity_ms"], 0)

    def test_second_run_is_quiet_and_succeeds(self):
        self.run_main()
        self.discord.messages.clear()
        self.assertEqual(self.run_main(), EXIT_OK)
        self.assertEqual(self.discord.messages, [])

    def test_new_activity_posts_on_a_later_run(self):
        league = FakeLeague()
        self.run_main(league=league)
        self.discord.messages.clear()

        league.activities.append(an_activity(9999))
        self.assertEqual(self.run_main(league=league), EXIT_OK)
        self.assertEqual(len(self.discord.messages), 1)
        self.assertIn("Player 9999", self.discord.messages[0])


class TestConfigFailure(MainTestCase):
    def test_missing_env_exits_non_zero_without_posting(self):
        code = self.run_main(env={})
        self.assertEqual(code, EXIT_FAILURE)
        self.assertEqual(self.discord.messages, [])

    def test_missing_env_is_reported_on_stderr(self):
        self.run_main(env={})
        self.assertIn("LEAGUE_ID", self.stderr)


class TestAuthFailure(MainTestCase):
    def test_auth_error_posts_once_and_exits_non_zero(self):
        self.seed_state()

        def explode(config):
            raise ESPNAccessDenied("League 1234567 cannot be accessed with the provided credentials")

        import poller

        original = poller.build_league
        poller.build_league = explode
        self.addCleanup(setattr, poller, "build_league", original)

        code = self.run_main(league=None)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertEqual(len(self.discord.messages), 1)
        self.assertIn("cookies", self.discord.messages[0].lower())

    def test_auth_message_does_not_leak_the_league_id(self):
        self.seed_state()

        def explode(config):
            raise ESPNAccessDenied("League 1234567 cannot be accessed with the provided credentials")

        import poller

        original = poller.build_league
        poller.build_league = explode
        self.addCleanup(setattr, poller, "build_league", original)

        self.run_main(league=None)
        self.assertNotIn("1234567", self.discord.messages[0])
        self.assertNotIn("1234567", self.stderr)

    def test_classification(self):
        self.assertTrue(is_auth_error(ESPNAccessDenied("League 1 cannot be accessed")))
        self.assertTrue(is_auth_error(RuntimeError("ESPN returned an HTTP 401")))
        self.assertTrue(is_auth_error(RuntimeError("HTTP 403 forbidden")))
        self.assertFalse(is_auth_error(RuntimeError("connection reset")))


class TestIndependentFeatures(MainTestCase):
    def test_results_failure_does_not_stop_transactions(self):
        # The core isolation guarantee.
        self.seed_state()
        # current_week=4 so week 3 is unposted and the scoreboard is actually
        # reached; with current_week=3 there would be nothing left to fail on.
        league = FakeLeague(
            current_week=4, raise_on={"scoreboard"}, activities=[an_activity(9999)]
        )

        code = self.run_main(league=league)
        self.assertEqual(code, EXIT_FAILURE)

        posted = "\n".join(self.discord.messages)
        self.assertIn("Player 9999", posted)
        self.assertIn("weekly results", posted)

    def test_transactions_failure_does_not_stop_results(self):
        self.seed_state(posted_weeks=[])
        league = FakeLeague(raise_on={"activity"})

        code = self.run_main(league=league)
        self.assertEqual(code, EXIT_FAILURE)

        posted = "\n".join(self.discord.messages)
        self.assertIn("Week 1", posted)
        self.assertIn("transactions", posted)

    def test_a_feature_failure_still_writes_state(self):
        self.seed_state(posted_weeks=[])
        self.run_main(league=FakeLeague(raise_on={"activity"}))
        self.assertEqual(load_state(self.path)["posted_weeks"], [1, 2])

    def test_reminder_timezone_failure_is_surfaced(self):
        self.seed_state()

        import poller

        original = poller.process_reminders

        def explode(*args, **kwargs):
            raise TimezoneUnavailable("no tzdata")

        poller.process_reminders = explode
        self.addCleanup(setattr, poller, "process_reminders", original)

        code = self.run_main()
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("timezone", "\n".join(self.discord.messages).lower())

    def test_all_three_failing_still_exits_once(self):
        self.seed_state()
        league = FakeLeague(raise_on={"activity", "scoreboard"})
        self.assertEqual(self.run_main(league=league), EXIT_FAILURE)


class TestErrorRateLimiting(MainTestCase):
    def test_repeated_failure_posts_once_per_day(self):
        self.seed_state()
        league = FakeLeague(raise_on={"activity"})

        self.run_main(league=league)
        first_count = len(self.discord.messages)
        self.assertEqual(first_count, 1)

        # Twenty minutes later, the same failure.
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            main(
                [],
                env=FAKE_ENV,
                league=league,
                state_path=self.path,
                discord=self.discord,
                now_ms=NOW_MS + 20 * 60 * 1000,
            )
        self.assertEqual(len(self.discord.messages), first_count)

    def test_posts_again_after_the_cooldown(self):
        self.seed_state()
        league = FakeLeague(raise_on={"activity"})
        self.run_main(league=league)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            main(
                [],
                env=FAKE_ENV,
                league=league,
                state_path=self.path,
                discord=self.discord,
                now_ms=NOW_MS + ERROR_COOLDOWN_MS + 1,
            )
        self.assertEqual(len(self.discord.messages), 2)

    def test_cooldown_is_twenty_four_hours(self):
        self.assertEqual(ERROR_COOLDOWN_MS, 24 * 60 * 60 * 1000)

    def test_notice_timestamps_are_persisted_as_integers(self):
        self.seed_state()
        self.run_main(league=FakeLeague(raise_on={"activity"}))
        with open(self.path, encoding="utf-8") as handle:
            stored = json.load(handle)
        for value in stored["error_notices"].values():
            self.assertIsInstance(value, int)

    def test_different_kinds_are_limited_separately(self):
        state = {"error_notices": {"transactions": NOW_MS}}
        discord = RecordingDiscord()
        self.assertFalse(notify_error("transactions", state, discord, now_ms=NOW_MS))
        self.assertTrue(notify_error("results", state, discord, now_ms=NOW_MS))

    def test_a_failing_discord_does_not_crash_the_run(self):
        class BrokenDiscord:
            def post(self, content):
                from poller import DiscordError

                raise DiscordError("Discord returned HTTP 500")

        state = {}
        self.assertFalse(notify_error("results", state, BrokenDiscord(), now_ms=NOW_MS))
        self.assertEqual(state.get("error_notices"), None)


class TestStateSafety(MainTestCase):
    def test_state_json_holds_only_safe_values(self):
        # It is committed to a public repo.
        self.run_main()
        with open(self.path, encoding="utf-8") as handle:
            raw = handle.read()
        for secret in ("FAKE_S2", "FAKE-SWID", "example.invalid", "1234567"):
            self.assertNotIn(secret, raw)

    def test_no_team_or_player_names_are_persisted(self):
        self.run_main(league=FakeLeague(activities=[an_activity(1)]))
        with open(self.path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("Team A", raw)
        self.assertNotIn("Player", raw)


class TestDryRun(MainTestCase):
    def test_dry_run_does_not_write_state(self):
        self.seed_state()
        with open(self.path, encoding="utf-8") as handle:
            before = handle.read()

        self.run_main(argv=["--dry-run"], league=FakeLeague(activities=[an_activity(9999)]))

        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_dry_run_still_reports_an_exit_code(self):
        self.seed_state()
        self.assertEqual(self.run_main(argv=["--dry-run"]), EXIT_OK)


if __name__ == "__main__":
    unittest.main()
