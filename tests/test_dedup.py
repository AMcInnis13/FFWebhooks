"""Tests for transaction dedup, watermark, and ordering (T-006).

The central guarantee: across any sequence of runs, every activity posts
exactly once, in chronological order, and a failure mid-run never silently
skips what it did not get to.
"""

import unittest

from poller import (
    RECENT_ACTIVITY_SIZE,
    DiscordError,
    default_state,
    fingerprint,
    process_transactions,
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


def add_activity(date, team="Team A", player="Player X"):
    """A simple FA add, which always renders to a non-empty message."""
    return FakeActivity(date, [(FakeTeam(team), "FA ADDED", FakePlayer(player), 0)])


def unrenderable_activity(date):
    """An activity espn_api could not classify -- renders to ''."""
    return FakeActivity(date, [(FakeTeam("Team A"), "UNKNOWN", FakePlayer("Ghost"), 0)])


class FakeLeague:
    """Returns activities newest-first, the way espn_api does."""

    def __init__(self, activities):
        self.activities = list(activities)
        self.calls = []

    def recent_activity(self, size=25):
        self.calls.append(size)
        return sorted(self.activities, key=lambda a: a.date, reverse=True)


class RecordingDiscord:
    """Stands in for DiscordRouter: post() takes a category."""

    def __init__(self, fail_on=None, fail_category=None):
        self.messages = []
        self.categories = []
        self.fail_on = fail_on
        self.fail_category = fail_category

    def post(self, category, content):
        if self.fail_category is not None and category == self.fail_category:
            raise DiscordError("Discord returned HTTP 500")
        if self.fail_on is not None and len(self.messages) + 1 == self.fail_on:
            raise DiscordError("Discord returned HTTP 500")
        self.messages.append(content)
        self.categories.append(category)
        return 1


def run(activities, state=None, discord=None):
    state = default_state() if state is None else state
    discord = RecordingDiscord() if discord is None else discord
    posted = process_transactions(FakeLeague(activities), state, discord)
    return posted, state, discord


class TestBasics(unittest.TestCase):
    def test_no_activities_posts_nothing(self):
        posted, state, discord = run([])
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])
        self.assertEqual(state["last_activity_ms"], 0)

    def test_recent_activity_requested_with_the_documented_size(self):
        league = FakeLeague([])
        process_transactions(league, default_state(), RecordingDiscord())
        self.assertEqual(league.calls, [RECENT_ACTIVITY_SIZE])
        self.assertEqual(RECENT_ACTIVITY_SIZE, 50)

    def test_recent_activity_returning_none_is_tolerated(self):
        class NoneLeague:
            def recent_activity(self, size=25):
                return None

        self.assertEqual(
            process_transactions(NoneLeague(), default_state(), RecordingDiscord()), 0
        )

    def test_all_activities_post_on_a_fresh_state(self):
        posted, _, discord = run([add_activity(BASE_MS + i, player=f"P{i}") for i in range(3)])
        self.assertEqual(posted, 3)
        self.assertEqual(len(discord.messages), 3)


class TestOrdering(unittest.TestCase):
    def test_posts_oldest_first(self):
        # The library hands back newest-first; posting in that order would
        # make the channel read backwards.
        activities = [add_activity(BASE_MS + i, player=f"P{i}") for i in range(5)]
        _, _, discord = run(activities)
        self.assertEqual(
            discord.messages, [m for m in sorted(discord.messages, key=lambda s: int(s.split("P")[1]))]
        )
        self.assertIn("P0", discord.messages[0])
        self.assertIn("P4", discord.messages[-1])

    def test_same_millisecond_activities_are_ordered_deterministically(self):
        activities = [
            add_activity(BASE_MS, player="P0"),
            add_activity(BASE_MS, player="P1"),
        ]
        first = run(activities)[2].messages
        second = run(activities)[2].messages
        self.assertEqual(first, second)


class TestWatermark(unittest.TestCase):
    def test_watermark_advances_to_the_newest_posted(self):
        _, state, _ = run([add_activity(BASE_MS + i) for i in range(3)])
        self.assertEqual(state["last_activity_ms"], BASE_MS + 2)

    def test_second_run_posts_nothing(self):
        activities = [add_activity(BASE_MS + i, player=f"P{i}") for i in range(3)]
        _, state, _ = run(activities)
        posted, state, discord = run(activities, state=state)
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])

    def test_older_activity_is_ignored(self):
        state = default_state()
        state["last_activity_ms"] = BASE_MS + 100
        posted, _, discord = run([add_activity(BASE_MS)], state=state)
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])

    def test_watermark_never_goes_backwards(self):
        state = default_state()
        state["last_activity_ms"] = BASE_MS + 500
        _, state, _ = run([add_activity(BASE_MS)], state=state)
        self.assertEqual(state["last_activity_ms"], BASE_MS + 500)

    def test_only_newer_activities_post_on_an_incremental_run(self):
        old = [add_activity(BASE_MS + i, player=f"old{i}") for i in range(2)]
        _, state, _ = run(old)

        fresh = old + [add_activity(BASE_MS + 10, player="new")]
        posted, _, discord = run(fresh, state=state)
        self.assertEqual(posted, 1)
        self.assertIn("new", discord.messages[0])


class TestMillisecondCollision(unittest.TestCase):
    """The exact case the fingerprint list exists for.

    A watermark alone cannot distinguish two activities stamped with the same
    millisecond, so the second would be lost forever.
    """

    def test_both_post_exactly_once_across_two_runs(self):
        first_activity = add_activity(BASE_MS, player="First")
        second_activity = add_activity(BASE_MS, player="Second")

        # Run 1: ESPN has only reported the first one.
        posted, state, discord = run([first_activity])
        self.assertEqual(posted, 1)
        self.assertIn("First", discord.messages[0])
        self.assertEqual(state["last_activity_ms"], BASE_MS)

        # Run 2: the second activity appears, stamped with the same ms.
        posted, state, discord = run([first_activity, second_activity], state=state)
        self.assertEqual(posted, 1, "activity sharing a millisecond was dropped")
        self.assertIn("Second", discord.messages[0])

        # Run 3: nothing left to say.
        posted, state, discord = run([first_activity, second_activity], state=state)
        self.assertEqual(posted, 0)

    def test_both_in_one_run_post_once_each(self):
        activities = [add_activity(BASE_MS, player="First"), add_activity(BASE_MS, player="Second")]
        posted, state, discord = run(activities)
        self.assertEqual(posted, 2)
        posted, _, _ = run(activities, state=state)
        self.assertEqual(posted, 0)

    def test_duplicate_activity_in_one_response_posts_once(self):
        activity = add_activity(BASE_MS)
        posted, _, discord = run([activity, activity])
        self.assertEqual(posted, 1)
        self.assertEqual(len(discord.messages), 1)


class TestFingerprintBookkeeping(unittest.TestCase):
    def test_fingerprints_are_recorded(self):
        # An FA add is a roster move, so the marker is scoped to that
        # category rather than the activity as a whole.
        activity = add_activity(BASE_MS)
        _, state, _ = run([activity])
        self.assertIn(fingerprint(activity, "roster"), state["seen_fingerprints"])

    def test_unrenderable_activity_is_marked_seen_without_posting(self):
        # Otherwise it is reconsidered on every run, forever.
        activity = unrenderable_activity(BASE_MS)
        posted, state, discord = run([activity])
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])
        self.assertIn(fingerprint(activity), state["seen_fingerprints"])
        self.assertEqual(state["last_activity_ms"], BASE_MS)

    def test_unrenderable_activity_does_not_block_later_ones(self):
        activities = [unrenderable_activity(BASE_MS), add_activity(BASE_MS + 1, player="Real")]
        posted, _, discord = run(activities)
        self.assertEqual(posted, 1)
        self.assertIn("Real", discord.messages[0])

    def test_a_known_fingerprint_is_skipped_even_at_a_newer_timestamp(self):
        activity = add_activity(BASE_MS)
        state = default_state()
        state["seen_fingerprints"] = [fingerprint(activity)]
        posted, _, discord = run([activity], state=state)
        self.assertEqual(posted, 0)


class TestPartialFailure(unittest.TestCase):
    def test_failure_stops_the_run_and_propagates(self):
        activities = [add_activity(BASE_MS + i, player=f"P{i}") for i in range(3)]
        discord = RecordingDiscord(fail_on=2)
        state = default_state()
        with self.assertRaises(DiscordError):
            process_transactions(FakeLeague(activities), state, discord)
        self.assertEqual(len(discord.messages), 1)

    def test_successful_posts_before_the_failure_are_recorded(self):
        activities = [add_activity(BASE_MS + i, player=f"P{i}") for i in range(3)]
        state = default_state()
        with self.assertRaises(DiscordError):
            process_transactions(FakeLeague(activities), state, RecordingDiscord(fail_on=2))
        self.assertEqual(state["last_activity_ms"], BASE_MS)
        self.assertIn(fingerprint(activities[0], "roster"), state["seen_fingerprints"])

    def test_the_failed_activity_is_not_marked_seen(self):
        activities = [add_activity(BASE_MS + i, player=f"P{i}") for i in range(3)]
        state = default_state()
        with self.assertRaises(DiscordError):
            process_transactions(FakeLeague(activities), state, RecordingDiscord(fail_on=2))
        self.assertNotIn(fingerprint(activities[1], "roster"), state["seen_fingerprints"])

    def test_next_run_retries_only_what_did_not_post(self):
        activities = [add_activity(BASE_MS + i, player=f"P{i}") for i in range(3)]
        state = default_state()
        with self.assertRaises(DiscordError):
            process_transactions(FakeLeague(activities), state, RecordingDiscord(fail_on=2))

        posted, _, discord = run(activities, state=state)
        self.assertEqual(posted, 2)
        self.assertIn("P1", discord.messages[0])
        self.assertIn("P2", discord.messages[1])
        self.assertFalse(any("P0" in m for m in discord.messages))


class TestPerCategoryDedup(unittest.TestCase):
    """One activity, two channels, and a failure in between.

    Recording dedup per activity rather than per category would mean a
    failure on the second post leaves nothing recorded -- so the next run
    reposts the message that already succeeded.
    """

    def mixed_activity(self, date=BASE_MS):
        """A trade and a roster move in one activity: two categories."""
        return FakeActivity(
            date,
            [
                (FakeTeam("Team A"), "TRADE_SENT", FakePlayer("Traded Guy"), 0),
                (FakeTeam("Team B"), "TRADE_RECEIVED", FakePlayer("Traded Guy"), 0),
                (FakeTeam("Team C"), "FA ADDED", FakePlayer("Added Guy"), 0),
            ],
        )

    def test_one_activity_posts_to_both_channels(self):
        posted, _, discord = run([self.mixed_activity()])
        self.assertEqual(posted, 2)
        self.assertEqual(sorted(discord.categories), ["roster", "trades"])

    def test_second_run_posts_neither_again(self):
        _, state, _ = run([self.mixed_activity()])
        posted, _, discord = run([self.mixed_activity()], state=state)
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])

    def test_a_failed_second_category_does_not_repost_the_first(self):
        activity = self.mixed_activity()
        state = default_state()

        # The trade posts; the roster message fails.
        discord = RecordingDiscord(fail_category="roster")
        with self.assertRaises(DiscordError):
            process_transactions(FakeLeague([activity]), state, discord)
        self.assertEqual(discord.categories, ["trades"])

        # Next run: only the roster message is retried.
        posted, state, retry = run([activity], state=state)
        self.assertEqual(posted, 1, "expected only the failed category to retry")
        self.assertEqual(retry.categories, ["roster"])
        self.assertIn("Added Guy", retry.messages[0])
        self.assertFalse(any("Traded Guy" in m for m in retry.messages))

    def test_the_watermark_stays_back_until_every_category_posts(self):
        activity = self.mixed_activity()
        state = default_state()
        with self.assertRaises(DiscordError):
            process_transactions(
                FakeLeague([activity]), state, RecordingDiscord(fail_category="roster")
            )
        # Advancing past the activity would strand the unsent category.
        self.assertEqual(state["last_activity_ms"], 0)

    def test_a_third_run_after_recovery_is_silent(self):
        activity = self.mixed_activity()
        state = default_state()
        with self.assertRaises(DiscordError):
            process_transactions(
                FakeLeague([activity]), state, RecordingDiscord(fail_category="roster")
            )
        run([activity], state=state)
        posted, _, discord = run([activity], state=state)
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])

    def test_category_markers_are_distinct_and_opaque(self):
        activity = self.mixed_activity()
        trades = fingerprint(activity, "trades")
        roster = fingerprint(activity, "roster")
        whole = fingerprint(activity)
        self.assertEqual(len({trades, roster, whole}), 3)
        for mark in (trades, roster, whole):
            with self.subTest(mark=mark):
                # state.json is public: the marker must not disclose the type.
                self.assertNotIn("trade", mark)
                self.assertNotIn("roster", mark)
                self.assertTrue(all(c in "0123456789abcdef" for c in mark))

    def test_an_activity_wide_marker_suppresses_every_category(self):
        # This is what bootstrap seeds.
        activity = self.mixed_activity()
        state = default_state()
        state["seen_fingerprints"] = [fingerprint(activity)]
        posted, _, discord = run([activity], state=state)
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])


class TestMalformedInput(unittest.TestCase):
    def test_missing_date_is_treated_as_zero(self):
        activity = FakeActivity(None, [(FakeTeam("A"), "FA ADDED", FakePlayer("X"), 0)])
        posted, state, _ = run([activity])
        self.assertEqual(posted, 1)
        self.assertEqual(state["last_activity_ms"], 0)

    def test_string_date_is_coerced(self):
        activity = FakeActivity(str(BASE_MS), [(FakeTeam("A"), "FA ADDED", FakePlayer("X"), 0)])
        _, state, _ = run([activity])
        self.assertEqual(state["last_activity_ms"], BASE_MS)

    def test_existing_state_is_not_replaced_wholesale(self):
        state = default_state()
        state["posted_weeks"] = [1, 2]
        state["future_key"] = "keep"
        run([add_activity(BASE_MS)], state=state)
        self.assertEqual(state["posted_weeks"], [1, 2])
        self.assertEqual(state["future_key"], "keep")


if __name__ == "__main__":
    unittest.main()
