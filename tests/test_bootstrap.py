"""Tests for first-run bootstrap behavior (T-009).

The guarantee under test: a fresh state.json produces exactly ONE message,
never a season of backlog. This is the easiest way to spam a server with
hundreds of messages, so the assertions here are deliberately blunt.
"""

import os
import tempfile
import unittest

from poller import (
    RECENT_ACTIVITY_SIZE,
    bootstrap,
    default_state,
    fingerprint,
    load_state,
    needs_bootstrap,
    process_reminders,
    process_results,
    process_transactions,
    render_bootstrap_message,
    save_state,
)

BASE_MS = 1756200000000


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


def activity(index):
    return FakeActivity(
        BASE_MS + index,
        [(FakeTeam(f"Team {index % 10}"), "FA ADDED", FakePlayer(f"Player {index}"), 0)],
    )


class BusyLeague:
    """A league mid-season: 50 activities and 6 completed weeks."""

    def __init__(self, activity_count=50, current_week=7):
        self.activities = [activity(i) for i in range(activity_count)]
        self.current_week = current_week

    def recent_activity(self, size=25):
        return sorted(self.activities, key=lambda a: a.date, reverse=True)[:size]

    def scoreboard(self, week):
        return [FakeMatchup(FakeTeam("Home"), 100.0 + week, FakeTeam("Away"), 90.0)]


class RecordingDiscord:
    def __init__(self):
        self.messages = []

    def post(self, content):
        self.messages.append(content)
        return 1


class TestExactlyOneMessage(unittest.TestCase):
    def test_busy_league_produces_exactly_one_post(self):
        league = BusyLeague()
        discord = RecordingDiscord()
        state = default_state()

        self.assertEqual(bootstrap(league, state, discord), 1)
        self.assertEqual(
            len(discord.messages), 1, f"expected 1 message, got {len(discord.messages)}"
        )

    def test_message_says_the_notifier_is_online(self):
        discord = RecordingDiscord()
        bootstrap(BusyLeague(), default_state(), discord)
        self.assertIn("online", discord.messages[0].lower())

    def test_message_does_not_contain_any_transaction(self):
        discord = RecordingDiscord()
        bootstrap(BusyLeague(), default_state(), discord)
        message = discord.messages[0]
        self.assertNotIn("Player 1", message)
        self.assertNotIn("Team 1", message)

    def test_empty_league_still_confirms(self):
        league = BusyLeague(activity_count=0, current_week=1)
        discord = RecordingDiscord()
        state = default_state()
        self.assertEqual(bootstrap(league, state, discord), 1)
        self.assertEqual(state["last_activity_ms"], 0)
        self.assertEqual(state["posted_weeks"], [])


class TestStateSeeding(unittest.TestCase):
    def setUp(self):
        self.league = BusyLeague()
        self.state = default_state()
        bootstrap(self.league, self.state, RecordingDiscord())

    def test_watermark_is_the_newest_activity(self):
        newest = max(a.date for a in self.league.activities)
        self.assertEqual(self.state["last_activity_ms"], newest)

    def test_every_activity_is_fingerprinted(self):
        self.assertEqual(len(self.state["seen_fingerprints"]), RECENT_ACTIVITY_SIZE)
        for a in self.league.recent_activity(RECENT_ACTIVITY_SIZE):
            self.assertIn(fingerprint(a), self.state["seen_fingerprints"])

    def test_all_completed_weeks_are_marked_posted(self):
        self.assertEqual(self.state["posted_weeks"], [1, 2, 3, 4, 5, 6])

    def test_in_progress_week_is_not_marked_posted(self):
        self.assertNotIn(7, self.state["posted_weeks"])

    def test_reminders_are_left_open(self):
        # A reminder due right now is current news, not backlog.
        self.assertEqual(self.state["posted_reminders"], [])


class TestNoBacklogAfterBootstrap(unittest.TestCase):
    """The whole point: the run after bootstrap must be silent."""

    def test_transactions_post_nothing(self):
        league = BusyLeague()
        state = default_state()
        bootstrap(league, state, RecordingDiscord())

        discord = RecordingDiscord()
        self.assertEqual(process_transactions(league, state, discord), 0)
        self.assertEqual(discord.messages, [])

    def test_results_post_nothing(self):
        league = BusyLeague()
        state = default_state()
        bootstrap(league, state, RecordingDiscord())

        discord = RecordingDiscord()
        self.assertEqual(process_results(league, state, discord), 0)
        self.assertEqual(discord.messages, [])

    def test_newest_activity_does_not_repost(self):
        # process_transactions compares with >= so that same-millisecond
        # activities are reconsidered. That makes storing fingerprints during
        # bootstrap mandatory: without them the newest activity looks new.
        league = BusyLeague()
        state = default_state()
        bootstrap(league, state, RecordingDiscord())

        discord = RecordingDiscord()
        process_transactions(league, state, discord)
        self.assertEqual(discord.messages, [])

    def test_a_genuinely_new_activity_still_posts(self):
        league = BusyLeague()
        state = default_state()
        bootstrap(league, state, RecordingDiscord())

        league.activities.append(
            FakeActivity(
                BASE_MS + 9999,
                [(FakeTeam("Latecomer"), "FA ADDED", FakePlayer("Fresh Face"), 0)],
            )
        )
        discord = RecordingDiscord()
        self.assertEqual(process_transactions(league, state, discord), 1)
        self.assertIn("Fresh Face", discord.messages[0])

    def test_a_newly_completed_week_still_posts(self):
        league = BusyLeague()
        state = default_state()
        bootstrap(league, state, RecordingDiscord())

        league.current_week = 8
        discord = RecordingDiscord()
        self.assertEqual(process_results(league, state, discord), 1)
        self.assertIn("Week 7", discord.messages[0])


class TestPersistedBootstrap(unittest.TestCase):
    """Bootstrap must survive a real state.json round trip."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "state.json")

    def test_shipped_empty_object_triggers_bootstrap_exactly_once(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.assertTrue(needs_bootstrap(self.path))

        league = BusyLeague()
        state = load_state(self.path)
        discord = RecordingDiscord()
        bootstrap(league, state, discord)
        save_state(state, self.path)

        self.assertEqual(len(discord.messages), 1)
        self.assertFalse(needs_bootstrap(self.path))

    def test_second_run_is_completely_silent(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{}")

        league = BusyLeague()
        state = load_state(self.path)
        bootstrap(league, state, RecordingDiscord())
        save_state(state, self.path)

        reloaded = load_state(self.path)
        discord = RecordingDiscord()
        process_transactions(league, reloaded, discord)
        process_results(league, reloaded, discord)
        self.assertEqual(discord.messages, [])

    def test_missing_file_triggers_bootstrap(self):
        self.assertTrue(needs_bootstrap(self.path))

    def test_fingerprints_survive_the_trim(self):
        # 50 activities is well under the 300 cap, so none should be lost.
        league = BusyLeague()
        state = load_state(self.path)
        bootstrap(league, state, RecordingDiscord())
        saved = save_state(state, self.path)
        self.assertEqual(len(saved["seen_fingerprints"]), 50)


class TestMessageRendering(unittest.TestCase):
    def test_counts_are_reported(self):
        message = render_bootstrap_message(50, 6)
        self.assertIn("50", message)
        self.assertIn("6", message)

    def test_singular_forms(self):
        message = render_bootstrap_message(1, 1)
        self.assertIn("1 existing transaction ", message)
        self.assertIn("1 completed week ", message)

    def test_explains_that_no_backlog_follows(self):
        self.assertIn("no backlog", render_bootstrap_message(50, 6))

    def test_no_league_id_leaks(self):
        self.assertNotIn("1234567", render_bootstrap_message(50, 6))


if __name__ == "__main__":
    unittest.main()
