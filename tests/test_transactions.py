"""Tests for transaction rendering (T-005).

Fixtures mirror the tuples espn_api actually builds in
espn_api/football/activity.py, including its rough edges: 'UNKNOWN' verbs,
TRADE_SENT rows with no matching TRADE_RECEIVED, empty-string teams, and
players that come back as a bare int id.
"""

import unittest

from poller import (
    CATEGORY_ROSTER,
    CATEGORY_TRADES,
    UNKNOWN_TEAM_LABEL,
)
from poller import render_activity as render_categorised


def render_activity(activity):
    """All categories joined into one string.

    Most assertions here are about wording and grouping, not routing, and
    reading identically whichever channel a block lands in is the point.
    Routing itself is covered by TestCategoryRouting below.
    """
    return "\n".join(message for _, message in render_categorised(activity))


class FakeTeam:
    def __init__(self, name):
        self.team_name = name


class FakePlayer:
    def __init__(self, name):
        self.name = name


class FakeActivity:
    def __init__(self, actions, date=1756200000000):
        self.date = date
        self.actions = actions


def team(name):
    return FakeTeam(name)


def player(name):
    return FakePlayer(name)


class TestTrades(unittest.TestCase):
    def three_player_trade(self):
        """Two teams, three players -- the shape activity.py emits."""
        a, b = team("Team A"), team("Team B")
        x, y, z = player("Player X"), player("Player Y"), player("Player Z")
        return FakeActivity(
            [
                (a, "TRADE_SENT", x, 0),
                (b, "TRADE_RECEIVED", x, 0),
                (a, "TRADE_SENT", y, 0),
                (b, "TRADE_RECEIVED", y, 0),
                (b, "TRADE_SENT", z, 0),
                (a, "TRADE_RECEIVED", z, 0),
            ]
        )

    def test_renders_one_block_not_six_messages(self):
        message = render_activity(self.three_player_trade())
        self.assertEqual(message.count("Trade processed"), 1)

    def test_direction_is_correct(self):
        message = render_activity(self.three_player_trade())
        self.assertIn("Team B gets: Player X, Player Y", message)
        self.assertIn("Team A gets: Player Z", message)

    def test_does_not_merely_list_names(self):
        # The failure mode this task exists to avoid: a flat list of players
        # with no indication of who got what.
        message = render_activity(self.three_player_trade())
        self.assertIn("gets:", message)

    def test_players_are_not_duplicated_across_lines(self):
        message = render_activity(self.three_player_trade())
        self.assertEqual(message.count("Player X"), 1)
        self.assertEqual(message.count("Player Z"), 1)

    def test_unpaired_trade_sent_still_names_the_giver(self):
        # activity.py only appends TRADE_RECEIVED `if to_team`, so a send can
        # arrive alone. Dropping it would silently lose half a trade.
        activity = FakeActivity([(team("Team A"), "TRADE_SENT", player("Player Q"), 0)])
        message = render_activity(activity)
        self.assertIn("Team A", message)
        self.assertIn("Player Q", message)
        self.assertIn("gives up", message)

    def test_partially_paired_trade_renders_both_halves(self):
        a, b = team("Team A"), team("Team B")
        activity = FakeActivity(
            [
                (a, "TRADE_SENT", player("Player X"), 0),
                (b, "TRADE_RECEIVED", player("Player X"), 0),
                (b, "TRADE_SENT", player("Player Z"), 0),
            ]
        )
        message = render_activity(activity)
        self.assertIn("Team B gets: Player X", message)
        self.assertIn("Team B gives up: Player Z", message)

    def test_paired_players_are_not_reported_as_given_up(self):
        message = render_activity(self.three_player_trade())
        self.assertNotIn("gives up", message)


class TestWaiversAndFreeAgents(unittest.TestCase):
    def test_waiver_add_shows_the_bid(self):
        activity = FakeActivity(
            [(team("Waiver Wolves"), "WAIVER ADDED", player("Marvin Waivers Jr."), 42)]
        )
        message = render_activity(activity)
        self.assertIn("Waiver Wolves", message)
        self.assertIn("+ Marvin Waivers Jr. ($42 waiver)", message)

    def test_zero_bid_waiver_does_not_print_a_dollar_zero(self):
        activity = FakeActivity([(team("Team A"), "WAIVER ADDED", player("Player X"), 0)])
        message = render_activity(activity)
        self.assertIn("(waiver)", message)
        self.assertNotIn("$0", message)

    def test_free_agent_add_has_no_bid_annotation(self):
        activity = FakeActivity([(team("Team A"), "FA ADDED", player("Player X"), 0)])
        message = render_activity(activity)
        self.assertIn("+ Player X", message)
        self.assertNotIn("waiver", message)

    def test_drop_is_rendered(self):
        activity = FakeActivity(
            [(team("Team A"), "DROPPED", player("Cordarrelle Patterson"), 0)]
        )
        self.assertIn("Cordarrelle Patterson", render_activity(activity))


class TestAddDropGrouping(unittest.TestCase):
    def add_drop(self):
        wolves = team("Waiver Wolves")
        return FakeActivity(
            [
                (wolves, "WAIVER ADDED", player("Marvin Waivers Jr."), 42),
                (wolves, "DROPPED", player("Cordarrelle Patterson"), 0),
            ]
        )

    def test_reads_as_one_entry_not_two(self):
        message = render_activity(self.add_drop())
        self.assertEqual(message.count("Waiver Wolves"), 1)

    def test_both_moves_appear(self):
        message = render_activity(self.add_drop())
        self.assertIn("+ Marvin Waivers Jr. ($42 waiver)", message)
        self.assertIn("Cordarrelle Patterson", message)

    def test_add_is_listed_before_the_drop(self):
        message = render_activity(self.add_drop())
        self.assertLess(message.index("Marvin"), message.index("Cordarrelle"))

    def test_two_teams_get_separate_blocks(self):
        activity = FakeActivity(
            [
                (team("Team A"), "FA ADDED", player("Player X"), 0),
                (team("Team B"), "FA ADDED", player("Player Y"), 0),
            ]
        )
        message = render_activity(activity)
        self.assertIn("Team A", message)
        self.assertIn("Team B", message)

    def test_multiple_adds_for_one_team_share_a_header(self):
        t = team("Team A")
        activity = FakeActivity(
            [
                (t, "FA ADDED", player("Player X"), 0),
                (t, "FA ADDED", player("Player Y"), 0),
            ]
        )
        message = render_activity(activity)
        self.assertEqual(message.count("Team A"), 1)
        self.assertIn("Player X", message)
        self.assertIn("Player Y", message)

    def test_trade_and_roster_move_in_one_activity_both_render(self):
        activity = FakeActivity(
            [
                (team("Team A"), "TRADE_SENT", player("Player X"), 0),
                (team("Team B"), "TRADE_RECEIVED", player("Player X"), 0),
                (team("Team C"), "FA ADDED", player("Player Y"), 0),
            ]
        )
        message = render_activity(activity)
        self.assertIn("Trade processed", message)
        self.assertIn("Team C", message)
        self.assertIn("Player Y", message)


class TestLibraryQuirks(unittest.TestCase):
    """The three activity.py behaviors found while verifying T-001."""

    def test_unknown_verb_is_skipped_not_printed(self):
        activity = FakeActivity(
            [
                (team("Team A"), "UNKNOWN", player("Player X"), 0),
                (team("Team A"), "FA ADDED", player("Player Y"), 0),
            ]
        )
        message = render_activity(activity)
        self.assertNotIn("UNKNOWN", message)
        self.assertNotIn("Player X", message)
        self.assertIn("Player Y", message)

    def test_activity_of_only_unknown_verbs_renders_empty(self):
        activity = FakeActivity([(team("Team A"), "UNKNOWN", player("Player X"), 0)])
        self.assertEqual(render_activity(activity), "")

    def test_raw_int_player_id_renders_without_crashing(self):
        activity = FakeActivity([(team("Team A"), "FA ADDED", 4241457, 0)])
        message = render_activity(activity)
        self.assertIn("4241457", message)

    def test_unknown_sentinel_player_renders(self):
        activity = FakeActivity([(team("Team A"), "DROPPED", "Unknown", 0)])
        self.assertIn("Unknown", render_activity(activity))

    def test_empty_string_team_gets_a_label(self):
        activity = FakeActivity([("", "FA ADDED", player("Player X"), 0)])
        message = render_activity(activity)
        self.assertIn(UNKNOWN_TEAM_LABEL, message)
        self.assertIn("Player X", message)

    def test_team_object_with_blank_name_gets_a_label(self):
        activity = FakeActivity([(FakeTeam(""), "DROPPED", player("Player X"), 0)])
        self.assertIn(UNKNOWN_TEAM_LABEL, render_activity(activity))


class TestCategoryRouting(unittest.TestCase):
    """Which channel each block is destined for."""

    def categories(self, activity):
        return [category for category, _ in render_categorised(activity)]

    def message_for(self, activity, category):
        for name, message in render_categorised(activity):
            if name == category:
                return message
        return None

    def test_a_trade_is_categorised_as_a_trade(self):
        activity = FakeActivity(
            [
                (team("Team A"), "TRADE_SENT", player("Player X"), 0),
                (team("Team B"), "TRADE_RECEIVED", player("Player X"), 0),
            ]
        )
        self.assertEqual(self.categories(activity), [CATEGORY_TRADES])

    def test_a_waiver_claim_is_categorised_as_a_roster_move(self):
        activity = FakeActivity(
            [(team("Waiver Wolves"), "WAIVER ADDED", player("Marvin Waivers Jr."), 42)]
        )
        self.assertEqual(self.categories(activity), [CATEGORY_ROSTER])

    def test_free_agent_adds_and_drops_are_roster_moves(self):
        for verb in ("FA ADDED", "DROPPED"):
            with self.subTest(verb=verb):
                activity = FakeActivity([(team("Team A"), verb, player("Player X"), 0)])
                self.assertEqual(self.categories(activity), [CATEGORY_ROSTER])

    def test_an_activity_with_both_splits_into_two_messages(self):
        # They belong in different channels; merging them defeats routing.
        activity = FakeActivity(
            [
                (team("Team A"), "TRADE_SENT", player("Traded Guy"), 0),
                (team("Team B"), "TRADE_RECEIVED", player("Traded Guy"), 0),
                (team("Team C"), "FA ADDED", player("Added Guy"), 0),
            ]
        )
        self.assertEqual(sorted(self.categories(activity)), sorted([CATEGORY_TRADES, CATEGORY_ROSTER]))

        trade = self.message_for(activity, CATEGORY_TRADES)
        roster = self.message_for(activity, CATEGORY_ROSTER)
        self.assertIn("Traded Guy", trade)
        self.assertNotIn("Added Guy", trade)
        self.assertIn("Added Guy", roster)
        self.assertNotIn("Traded Guy", roster)

    def test_all_teams_roster_moves_share_one_message(self):
        # They route to the same channel, so one message keeps the channel
        # readable rather than firing one post per team.
        activity = FakeActivity(
            [
                (team("Team A"), "FA ADDED", player("Player X"), 0),
                (team("Team B"), "FA ADDED", player("Player Y"), 0),
            ]
        )
        self.assertEqual(self.categories(activity), [CATEGORY_ROSTER])
        message = self.message_for(activity, CATEGORY_ROSTER)
        self.assertIn("Player X", message)
        self.assertIn("Player Y", message)

    def test_nothing_renderable_yields_no_pairs(self):
        activity = FakeActivity([(team("Team A"), "UNKNOWN", player("Ghost"), 0)])
        self.assertEqual(render_categorised(activity), [])

    def test_trades_are_listed_before_roster_moves(self):
        # Deterministic order matters: a partial failure resumes from it.
        activity = FakeActivity(
            [
                (team("Team A"), "FA ADDED", player("Added Guy"), 0),
                (team("Team B"), "TRADE_SENT", player("Traded Guy"), 0),
                (team("Team C"), "TRADE_RECEIVED", player("Traded Guy"), 0),
            ]
        )
        self.assertEqual(self.categories(activity), [CATEGORY_TRADES, CATEGORY_ROSTER])

    def test_every_message_is_non_empty(self):
        activity = FakeActivity(
            [
                (team("Team A"), "TRADE_SENT", player("X"), 0),
                (team("Team B"), "TRADE_RECEIVED", player("X"), 0),
                (team("Team C"), "DROPPED", player("Y"), 0),
            ]
        )
        for category, message in render_categorised(activity):
            with self.subTest(category=category):
                self.assertTrue(message.strip())


class TestDegenerateInput(unittest.TestCase):
    def test_no_actions(self):
        self.assertEqual(render_activity(FakeActivity([])), "")

    def test_actions_attribute_missing(self):
        self.assertEqual(render_activity(object()), "")

    def test_actions_is_none(self):
        activity = FakeActivity([])
        activity.actions = None
        self.assertEqual(render_activity(activity), "")

    def test_short_rows_do_not_crash(self):
        activity = FakeActivity([(team("Team A"), "DROPPED"), (), (team("B"),)])
        self.assertIsInstance(render_activity(activity), str)

    def test_none_verb_does_not_crash(self):
        activity = FakeActivity([(team("Team A"), None, player("Player X"), 0)])
        self.assertIsInstance(render_activity(activity), str)

    def test_lowercase_verb_is_still_matched(self):
        activity = FakeActivity([(team("Team A"), "fa added", player("Player X"), 0)])
        self.assertIn("Player X", render_activity(activity))

    def test_rendering_is_deterministic(self):
        activity = FakeActivity(
            [
                (team("Team A"), "TRADE_SENT", player("Player X"), 0),
                (team("Team B"), "TRADE_RECEIVED", player("Player X"), 0),
                (team("Team C"), "WAIVER ADDED", player("Player Y"), 12),
            ]
        )
        self.assertEqual(render_activity(activity), render_activity(activity))


class TestMessageShape(unittest.TestCase):
    def test_matches_the_documented_format(self):
        activity = FakeActivity(
            [
                (team("Waiver Wolves"), "WAIVER ADDED", player("Marvin Waivers Jr."), 42),
                (team("Waiver Wolves"), "DROPPED", player("Cordarrelle Patterson"), 0),
            ]
        )
        self.assertEqual(
            render_activity(activity),
            "\U0001f4e5 Waiver Wolves\n"
            "  + Marvin Waivers Jr. ($42 waiver)\n"
            "  − Cordarrelle Patterson",
        )

    def test_trade_matches_the_documented_format(self):
        a, b = team("Team A"), team("Team B")
        activity = FakeActivity(
            [
                (a, "TRADE_SENT", player("Player X"), 0),
                (b, "TRADE_RECEIVED", player("Player X"), 0),
                (a, "TRADE_SENT", player("Player Y"), 0),
                (b, "TRADE_RECEIVED", player("Player Y"), 0),
                (b, "TRADE_SENT", player("Player Z"), 0),
                (a, "TRADE_RECEIVED", player("Player Z"), 0),
            ]
        )
        self.assertEqual(
            render_activity(activity),
            "\U0001f501 Trade processed\n"
            "  Team B gets: Player X, Player Y\n"
            "  Team A gets: Player Z",
        )

    def test_drop_only_block_uses_the_outbox_marker(self):
        activity = FakeActivity([(team("Team A"), "DROPPED", player("Player X"), 0)])
        self.assertTrue(render_activity(activity).startswith("\U0001f4e4"))

    def test_no_mention_syntax_is_introduced(self):
        # allowed_mentions is the real guard, but the renderer should not be
        # manufacturing pings either.
        activity = FakeActivity([(team("Team A"), "FA ADDED", player("Player X"), 0)])
        message = render_activity(activity)
        self.assertNotIn("<@", message)
        self.assertNotIn("@everyone", message)


if __name__ == "__main__":
    unittest.main()
