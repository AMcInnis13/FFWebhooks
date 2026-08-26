"""Tests for weekly results (T-007).

Covers the three cases the spec calls out -- a tie, a bye, and a playoff
week -- plus the dedup and completeness rules around posted_weeks.
"""

import unittest

from poller import (
    DiscordError,
    default_state,
    process_results,
    render_week,
)


class FakeTeam:
    def __init__(self, name):
        self.team_name = name


class FakeMatchup:
    def __init__(self, home, home_score, away, away_score, is_playoff=False):
        self.home_team = home
        self.home_score = home_score
        self.away_team = away
        self.away_score = away_score
        self.is_playoff = is_playoff


class FakeLeague:
    def __init__(self, current_week, weeks=None):
        self.current_week = current_week
        self.weeks = weeks or {}
        self.requested = []

    def scoreboard(self, week):
        self.requested.append(week)
        return self.weeks.get(week, [])


class RecordingDiscord:
    def __init__(self, fail_on=None):
        self.messages = []
        self.fail_on = fail_on

    def post(self, content):
        if self.fail_on is not None and len(self.messages) + 1 == self.fail_on:
            raise DiscordError("Discord returned HTTP 500")
        self.messages.append(content)
        return 1


def matchup(home="Team A", hs=100.0, away="Team B", as_=90.0, playoff=False):
    return FakeMatchup(FakeTeam(home), hs, FakeTeam(away), as_, playoff)


def bye(team="Team E", score=88.0, playoff=False):
    """A bye: espn_api reports the missing side's id and score as 0."""
    return FakeMatchup(FakeTeam(team), score, 0, 0, playoff)


class TestRenderBasics(unittest.TestCase):
    def test_empty_scoreboard_renders_nothing(self):
        self.assertEqual(render_week(3, []), "")
        self.assertEqual(render_week(3, None), "")

    def test_header_names_the_week(self):
        self.assertIn("Week 3 Results", render_week(3, [matchup()]))

    def test_winner_is_listed_first(self):
        message = render_week(1, [matchup("Loser", 80.0, "Winner", 120.0)])
        line = [l for l in message.splitlines() if "Winner" in l][0]
        self.assertLess(line.index("Winner"), line.index("Loser"))

    def test_winner_is_marked(self):
        self.assertIn("✅", render_week(1, [matchup()]))

    def test_both_scores_appear(self):
        message = render_week(1, [matchup("Team A", 128.4, "Team B", 96.2)])
        self.assertIn("128.4", message)
        self.assertIn("96.2", message)

    def test_scores_render_to_one_decimal(self):
        message = render_week(1, [matchup("Team A", 101.0, "Team B", 99.0)])
        self.assertIn("101.0", message)
        self.assertIn("99.0", message)

    def test_multiple_matchups_all_appear(self):
        message = render_week(
            1, [matchup("A", 100.0, "B", 90.0), matchup("C", 120.0, "D", 110.0)]
        )
        for name in ("A", "B", "C", "D"):
            self.assertIn(name, message)

    def test_is_a_single_message(self):
        message = render_week(1, [matchup(), matchup("C", 1.0, "D", 2.0)])
        self.assertEqual(message.count("Week 1"), 1)


class TestTies(unittest.TestCase):
    def test_tie_is_labelled(self):
        message = render_week(1, [matchup("Team C", 101.0, "Team D", 101.0)])
        self.assertIn("(tie)", message)

    def test_tie_does_not_claim_a_winner(self):
        message = render_week(1, [matchup("Team C", 101.0, "Team D", 101.0)])
        self.assertNotIn("✅", message)

    def test_both_tied_teams_appear(self):
        message = render_week(1, [matchup("Team C", 101.0, "Team D", 101.0)])
        self.assertIn("Team C", message)
        self.assertIn("Team D", message)

    def test_tied_scores_still_count_for_high_and_low(self):
        message = render_week(1, [matchup("Team C", 101.0, "Team D", 101.0)])
        self.assertIn("High:", message)
        self.assertIn("101.0", message)


class TestByes(unittest.TestCase):
    def test_bye_is_labelled(self):
        self.assertIn("(bye)", render_week(1, [bye()]))

    def test_bye_names_the_team_and_score(self):
        message = render_week(1, [bye("Team E", 88.0)])
        self.assertIn("Team E", message)
        self.assertIn("88.0", message)

    def test_bye_does_not_invent_an_opponent(self):
        message = render_week(1, [bye("Team E", 88.0)])
        self.assertNotIn("0.0", message)

    def test_bye_is_excluded_from_high_and_low(self):
        # A bye is not a result; a 200-point bye must not take the high score.
        message = render_week(
            1, [matchup("Team A", 120.0, "Team B", 95.0), bye("Bye Team", 200.0)]
        )
        high_line = [l for l in message.splitlines() if "High:" in l][0]
        low_line = [l for l in message.splitlines() if "Low:" in l][0]
        self.assertIn("Team A", high_line)
        self.assertNotIn("Bye Team", high_line)
        self.assertNotIn("Bye Team", low_line)

    def test_missing_home_side_also_handled(self):
        m = FakeMatchup(0, 0, FakeTeam("Team F"), 77.0)
        message = render_week(1, [m])
        self.assertIn("Team F", message)
        self.assertIn("(bye)", message)

    def test_a_week_of_only_byes_has_no_high_low(self):
        message = render_week(1, [bye("Team E", 88.0)])
        self.assertNotIn("High:", message)


class TestPlayoffs(unittest.TestCase):
    def test_all_playoff_week_uses_a_playoff_header(self):
        message = render_week(
            15, [matchup(playoff=True), matchup("C", 100.0, "D", 90.0, playoff=True)]
        )
        self.assertIn("Playoff Results", message)

    def test_regular_week_does_not_say_playoff(self):
        self.assertNotIn("Playoff", render_week(3, [matchup()]))

    def test_mixed_week_tags_only_the_playoff_games(self):
        message = render_week(
            15,
            [
                matchup("Contender", 120.0, "Challenger", 110.0, playoff=True),
                matchup("Consolation A", 80.0, "Consolation B", 70.0, playoff=False),
            ],
        )
        playoff_line = [l for l in message.splitlines() if "Contender" in l][0]
        consolation_line = [l for l in message.splitlines() if "Consolation A" in l][0]
        self.assertIn("(playoff)", playoff_line)
        self.assertNotIn("(playoff)", consolation_line)

    def test_all_playoff_week_does_not_repeat_the_tag_on_every_line(self):
        message = render_week(15, [matchup(playoff=True), matchup("C", 1.0, "D", 2.0, playoff=True)])
        self.assertNotIn("(playoff)", message)


class TestHighAndLow(unittest.TestCase):
    def week(self):
        return [
            matchup("Top Dog", 150.5, "Middle", 100.0),
            matchup("Second", 120.0, "Bottom", 60.25),
        ]

    def test_high_is_the_best_score(self):
        line = [l for l in render_week(1, self.week()).splitlines() if "High:" in l][0]
        self.assertIn("Top Dog", line)
        self.assertIn("150.5", line)

    def test_low_is_the_worst_score(self):
        line = [l for l in render_week(1, self.week()).splitlines() if "Low:" in l][0]
        self.assertIn("Bottom", line)

    def test_losers_can_hold_the_high_score(self):
        message = render_week(1, [matchup("Winner", 100.0, "Loser", 99.0), bye("X", 10.0)])
        self.assertIn("Winner", message)

    def test_shared_high_score_lists_both_teams(self):
        message = render_week(
            1, [matchup("A", 120.0, "B", 80.0), matchup("C", 120.0, "D", 70.0)]
        )
        high_line = [l for l in message.splitlines() if "High:" in l][0]
        self.assertIn("A", high_line)
        self.assertIn("C", high_line)


class TestMalformedInput(unittest.TestCase):
    def test_missing_attributes_do_not_crash(self):
        self.assertIsInstance(render_week(1, [object()]), str)

    def test_none_scores_are_treated_as_zero(self):
        m = FakeMatchup(FakeTeam("A"), None, FakeTeam("B"), None)
        self.assertIsInstance(render_week(1, [m]), str)

    def test_string_scores_are_coerced(self):
        m = FakeMatchup(FakeTeam("A"), "120.5", FakeTeam("B"), "99.5")
        message = render_week(1, [m])
        self.assertIn("120.5", message)

    def test_blank_team_name_gets_a_label(self):
        m = FakeMatchup(FakeTeam(""), 100.0, FakeTeam("B"), 90.0)
        self.assertIsInstance(render_week(1, [m]), str)


class TestProcessResults(unittest.TestCase):
    def league(self, current_week=4):
        return FakeLeague(
            current_week,
            {
                1: [matchup("A", 100.0, "B", 90.0)],
                2: [matchup("C", 110.0, "D", 95.0)],
                3: [matchup("E", 120.0, "F", 85.0)],
                4: [matchup("G", 130.0, "H", 75.0)],
            },
        )

    def test_only_completed_weeks_are_posted(self):
        # week < current_week; week 4 is still in progress.
        league = self.league(current_week=4)
        state = default_state()
        discord = RecordingDiscord()
        self.assertEqual(process_results(league, state, discord), 3)
        self.assertEqual(state["posted_weeks"], [1, 2, 3])
        self.assertNotIn(4, league.requested)

    def test_weeks_post_in_chronological_order(self):
        discord = RecordingDiscord()
        process_results(self.league(), default_state(), discord)
        self.assertIn("Week 1", discord.messages[0])
        self.assertIn("Week 3", discord.messages[-1])

    def test_second_run_posts_nothing(self):
        state = default_state()
        process_results(self.league(), state, RecordingDiscord())
        discord = RecordingDiscord()
        self.assertEqual(process_results(self.league(), state, discord), 0)
        self.assertEqual(discord.messages, [])

    def test_already_posted_weeks_are_skipped(self):
        state = default_state()
        state["posted_weeks"] = [1, 2]
        discord = RecordingDiscord()
        self.assertEqual(process_results(self.league(), state, discord), 1)
        self.assertIn("Week 3", discord.messages[0])

    def test_a_new_completed_week_posts_incrementally(self):
        state = default_state()
        process_results(self.league(current_week=3), state, RecordingDiscord())
        self.assertEqual(state["posted_weeks"], [1, 2])

        discord = RecordingDiscord()
        process_results(self.league(current_week=4), state, discord)
        self.assertEqual(state["posted_weeks"], [1, 2, 3])
        self.assertEqual(len(discord.messages), 1)

    def test_week_one_in_progress_posts_nothing(self):
        league = FakeLeague(1, {})
        self.assertEqual(process_results(league, default_state(), RecordingDiscord()), 0)

    def test_empty_scoreboard_is_not_marked_posted(self):
        # More likely a transient ESPN hiccup than a week with no games.
        # Marking it would skip those results permanently.
        league = FakeLeague(3, {1: [], 2: [matchup()]})
        state = default_state()
        self.assertEqual(process_results(league, state, RecordingDiscord()), 1)
        self.assertEqual(state["posted_weeks"], [2])

        league = FakeLeague(3, {1: [matchup("Late", 100.0, "Data", 90.0)], 2: [matchup()]})
        discord = RecordingDiscord()
        self.assertEqual(process_results(league, state, discord), 1)
        self.assertIn("Late", discord.messages[0])

    def test_failure_leaves_the_week_unposted_for_retry(self):
        state = default_state()
        with self.assertRaises(DiscordError):
            process_results(self.league(), state, RecordingDiscord(fail_on=2))
        self.assertEqual(state["posted_weeks"], [1])

        discord = RecordingDiscord()
        process_results(self.league(), state, discord)
        self.assertEqual(len(discord.messages), 2)
        self.assertIn("Week 2", discord.messages[0])

    def test_other_state_keys_are_untouched(self):
        state = default_state()
        state["last_activity_ms"] = 12345
        state["future_key"] = "keep"
        process_results(self.league(), state, RecordingDiscord())
        self.assertEqual(state["last_activity_ms"], 12345)
        self.assertEqual(state["future_key"], "keep")


if __name__ == "__main__":
    unittest.main()
