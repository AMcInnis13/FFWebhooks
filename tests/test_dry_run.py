"""Tests for --dry-run (T-011).

Guards three properties that are easy to break by accident: no network, no
state write, and output that survives a non-UTF-8 stdout.
"""

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from poller import EXIT_OK, main, run_dry_run

try:
    ZoneInfo("America/Chicago")
    HAVE_TZDATA = True
except ZoneInfoNotFoundError:  # pragma: no cover - environment dependent
    HAVE_TZDATA = False


def capture(fn, *args, **kwargs):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = fn(*args, **kwargs)
    return result, buffer.getvalue()


class TestOutput(unittest.TestCase):
    def setUp(self):
        self.code, self.output = capture(run_dry_run)

    def test_exits_successfully(self):
        self.assertEqual(self.code, EXIT_OK)

    def test_all_four_sections_are_present(self):
        for section in ("bootstrap", "transactions", "weekly results", "lineup reminders"):
            with self.subTest(section=section):
                self.assertIn(section, self.output)

    def test_every_message_type_renders(self):
        self.assertIn("Trade processed", self.output)
        self.assertIn("$42 waiver", self.output)
        self.assertIn("Week 3 Results", self.output)
        self.assertIn("notifier is online", self.output)

    @unittest.skipUnless(HAVE_TZDATA, "tz database unavailable (pip install tzdata)")
    def test_reminder_renders_when_a_timezone_is_resolvable(self):
        self.assertIn("Lineups lock in 30 minutes", self.output)

    def test_reminder_section_degrades_rather_than_crashing(self):
        # Without a tz database the section reports why it was skipped. A dry
        # run must not fail because the local box lacks tzdata.
        self.assertTrue(
            "Lineups lock in 30 minutes" in self.output or "skipped:" in self.output,
            "reminder section neither rendered nor explained itself",
        )

    def test_bye_and_tie_paths_are_exercised(self):
        self.assertIn("(bye)", self.output)

    def test_unclassified_actions_are_not_printed(self):
        # The fixture deliberately includes an 'UNKNOWN' verb.
        self.assertNotIn("Should Not Appear", self.output)
        self.assertNotIn("UNKNOWN", self.output)

    def test_reports_a_message_count(self):
        self.assertIn("messages would have been sent", self.output)

    def test_says_it_is_a_dry_run(self):
        self.assertIn("DRY RUN", self.output)
        self.assertIn("no network calls", self.output)

    def test_reports_how_many_channels_are_in_use(self):
        self.assertIn("Routing across 4 channels", self.output)


class TestChannelLabels(unittest.TestCase):
    """Every message must name the channel it would reach.

    This caught a real bug: weekly results were being handed the main
    channel rather than the results channel, so a split setup would have
    quietly posted scores into the wrong place.
    """

    def setUp(self):
        self.code, self.output = capture(run_dry_run)

    def line_for(self, needle):
        lines = self.output.splitlines()
        for index, line in enumerate(lines):
            if needle in line:
                # Walk back to the nearest "would POST" header.
                for header in reversed(lines[:index]):
                    if "would POST" in header:
                        return header
        self.fail(f"no message containing {needle!r}")

    def test_trades_go_to_the_trades_channel(self):
        self.assertIn("trades channel", self.line_for("Trade processed"))

    def test_waivers_go_to_the_roster_channel(self):
        self.assertIn("roster moves channel", self.line_for("$42 waiver"))

    def test_free_agent_adds_go_to_the_roster_channel(self):
        self.assertIn("roster moves channel", self.line_for("Kimani Vidal"))

    def test_results_go_to_the_results_channel(self):
        self.assertIn("results channel", self.line_for("Week 3 Results"))

    def test_the_startup_message_goes_to_main(self):
        self.assertIn("main channel", self.line_for("notifier is online"))

    @unittest.skipUnless(HAVE_TZDATA, "tz database unavailable (pip install tzdata)")
    def test_reminders_go_to_main(self):
        self.assertIn("main channel", self.line_for("Lineups lock"))

    def test_every_post_names_a_channel(self):
        headers = [l for l in self.output.splitlines() if "would POST" in l]
        self.assertTrue(headers)
        for header in headers:
            with self.subTest(header=header):
                self.assertIn("channel", header)


class TestNoSideEffects(unittest.TestCase):
    def test_no_http_session_is_created(self):
        with mock.patch("poller.requests.Session", side_effect=AssertionError("network!")):
            code, _ = capture(run_dry_run)
        self.assertEqual(code, EXIT_OK)

    def test_state_is_never_written(self):
        with mock.patch("poller.save_state", side_effect=AssertionError("state written!")):
            code, _ = capture(run_dry_run)
        self.assertEqual(code, EXIT_OK)

    def test_no_config_is_loaded_from_the_environment(self):
        # `python poller.py --dry-run` must work from a bare checkout with no
        # secrets set.
        with mock.patch("poller.load_config", side_effect=AssertionError("read env!")):
            code, _ = capture(main, ["--dry-run"])
        self.assertEqual(code, EXIT_OK)


class TestEncoding(unittest.TestCase):
    """Regression test for the Windows cp1252 crash found in T-005."""

    def test_survives_a_cp1252_stdout(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with redirect_stdout(stream):
            code = run_dry_run()
        self.assertEqual(code, EXIT_OK)

    def test_non_cp1252_characters_reach_the_stream(self):
        # Messages no longer contain emoji, but the minus sign on drop lines
        # (U+2212) is still outside cp1252, so the reconfigure still matters.
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        with redirect_stdout(stream):
            run_dry_run()
        stream.flush()
        self.assertIn("−".encode("utf-8"), raw.getvalue())

    def test_a_stream_without_reconfigure_does_not_crash(self):
        # io.StringIO has no reconfigure(); the helper must tolerate that.
        code, output = capture(run_dry_run)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("−", output)


class TestRouting(unittest.TestCase):
    def test_cli_dry_run_uses_the_fixture_pipeline(self):
        code, output = capture(main, ["--dry-run"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("DRY RUN", output)

    def test_an_injected_league_takes_the_normal_path(self):
        # T-010's tests rely on this: injecting a league must not divert into
        # the fixture pipeline.
        with mock.patch("poller.run_dry_run", side_effect=AssertionError("diverted!")):
            with mock.patch("poller.load_config", side_effect=RuntimeError("stop here")):
                with self.assertRaises(RuntimeError):
                    main(["--dry-run"], env={"X": "Y"}, league=object())

    def test_normal_run_does_not_enter_dry_run(self):
        with mock.patch("poller.run_dry_run", side_effect=AssertionError("diverted!")):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main([], env={})
        self.assertNotEqual(code, EXIT_OK)


if __name__ == "__main__":
    unittest.main()
