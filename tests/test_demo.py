"""Tests for --demo (T-023).

Demo mode posts sample messages to the real channels, so the guarantees that
matter are what it does NOT do: no state write, no ESPN call, and no way for
a scheduled run to reach it.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from poller import (
    CATEGORY_MAIN,
    CATEGORY_RESULTS,
    CATEGORY_ROSTER,
    CATEGORY_TRADES,
    DEMO_FOOTER,
    DEMO_HEADER,
    EXIT_FAILURE,
    EXIT_OK,
    DiscordError,
    main,
    run_demo,
    save_state,
)

FAKE_ENV = {
    "LEAGUE_ID": "1234567",
    "LEAGUE_YEAR": "2026",
    "ESPN_S2": "FAKE_S2",
    "SWID": "{FAKE-SWID}",
    "DISCORD_WEBHOOK_URL": "https://example.invalid/hook/main",
    "DISCORD_WEBHOOK_URL_TRADES": "https://example.invalid/hook/trades",
    "DISCORD_WEBHOOK_URL_ROSTER": "https://example.invalid/hook/roster",
    "DISCORD_WEBHOOK_URL_RESULTS": "https://example.invalid/hook/results",
}


class RecordingRouter:
    def __init__(self, fail_on=None):
        self.posts = []
        self.fail_on = fail_on
        self.channel_count = 4

    def post(self, category, content):
        if self.fail_on is not None and len(self.posts) + 1 == self.fail_on:
            raise DiscordError("Discord returned HTTP 404")
        self.posts.append((category, content))
        return 1

    def for_category(self, category):
        return self

    @property
    def main(self):
        return self

    def messages_in(self, category):
        return [content for name, content in self.posts if name == category]


def run(router):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = run_demo(router)
    return code, buffer.getvalue()


class TestDemoOutput(unittest.TestCase):
    def setUp(self):
        self.router = RecordingRouter()
        self.code, self.output = run(self.router)

    def test_succeeds(self):
        self.assertEqual(self.code, EXIT_OK)

    def test_is_bookended_so_nobody_mistakes_it_for_real_activity(self):
        self.assertEqual(self.router.posts[0], (CATEGORY_MAIN, DEMO_HEADER))
        self.assertEqual(self.router.posts[-1], (CATEGORY_MAIN, DEMO_FOOTER))

    def test_the_notices_say_the_data_is_fake(self):
        self.assertIn("not real league activity", DEMO_HEADER)
        self.assertIn("deleted", DEMO_FOOTER)

    def test_a_trade_goes_to_the_trades_channel(self):
        trades = "\n".join(self.router.messages_in(CATEGORY_TRADES))
        self.assertIn("Trade processed", trades)

    def test_roster_moves_go_to_the_roster_channel(self):
        roster = "\n".join(self.router.messages_in(CATEGORY_ROSTER))
        self.assertIn("$42 waiver", roster)
        self.assertIn("Kimani Vidal", roster)

    def test_results_go_to_the_results_channel(self):
        results = "\n".join(self.router.messages_in(CATEGORY_RESULTS))
        self.assertIn("Week 3 Results", results)

    def test_both_reminders_are_shown_regardless_of_the_day(self):
        main_messages = "\n".join(self.router.messages_in(CATEGORY_MAIN))
        self.assertIn("Thursday night game", main_messages)
        self.assertIn("Sunday early games", main_messages)

    def test_reports_how_many_messages_were_sent(self):
        self.assertIn("demo complete", self.output)

    def test_no_unclassified_action_leaks_through(self):
        everything = "\n".join(content for _, content in self.router.posts)
        self.assertNotIn("Should Not Appear", everything)
        self.assertNotIn("UNKNOWN", everything)

    def test_sends_a_manageable_number_of_messages(self):
        # Enough to prove routing, few enough to delete by hand.
        self.assertLessEqual(len(self.router.posts), 12)


class TestDemoIsHarmless(unittest.TestCase):
    """It must not be able to disturb the real run."""

    def test_no_state_is_written(self):
        with mock.patch("poller.save_state", side_effect=AssertionError("state written!")):
            code, _ = run(RecordingRouter())
        self.assertEqual(code, EXIT_OK)

    def test_no_espn_connection_is_made(self):
        with mock.patch("poller.build_league", side_effect=AssertionError("called ESPN!")):
            code, _ = run(RecordingRouter())
        self.assertEqual(code, EXIT_OK)

    def test_main_demo_does_not_touch_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            save_state({"last_activity_ms": 999, "seen_fingerprints": ["abc"]}, path)
            with open(path, encoding="utf-8") as handle:
                before = handle.read()

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                main(
                    ["--demo"],
                    env=FAKE_ENV,
                    state_path=path,
                    router=RecordingRouter(),
                )

            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), before)

    def test_main_demo_does_not_build_a_league(self):
        with mock.patch("poller.build_league", side_effect=AssertionError("called ESPN!")):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--demo"], env=FAKE_ENV, router=RecordingRouter())
        self.assertEqual(code, EXIT_OK)


class TestDemoFailure(unittest.TestCase):
    def test_a_bad_webhook_reports_failure(self):
        # The point of the exercise: catching a wrong URL before going live.
        code, _ = run(RecordingRouter(fail_on=1))
        self.assertEqual(code, EXIT_FAILURE)

    def test_failure_does_not_leak_the_webhook_url(self):
        stderr = io.StringIO()
        from contextlib import redirect_stderr

        with redirect_stderr(stderr):
            with redirect_stdout(io.StringIO()):
                run_demo(RecordingRouter(fail_on=2))
        rendered = stderr.getvalue()
        self.assertNotIn("example.invalid", rendered)
        self.assertIn("demo failed", rendered)


class TestRouting(unittest.TestCase):
    def test_a_normal_run_never_enters_demo(self):
        with mock.patch("poller.run_demo", side_effect=AssertionError("entered demo!")):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main([], env={})
        self.assertEqual(code, EXIT_FAILURE)

    def test_demo_requires_real_config(self):
        # Unlike --dry-run, demo posts to real channels, so missing secrets
        # must fail rather than silently using fixtures.
        buffer = io.StringIO()
        from contextlib import redirect_stderr

        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(buffer):
            code = main(["--demo"], env={})
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("DISCORD_WEBHOOK_URL", stderr.getvalue())

    def test_demo_with_dry_run_does_not_divert_to_the_fixture_pipeline(self):
        with mock.patch("poller.run_dry_run", side_effect=AssertionError("diverted!")):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["--demo", "--dry-run"], env=FAKE_ENV, router=RecordingRouter())
        self.assertEqual(code, EXIT_OK)


if __name__ == "__main__":
    unittest.main()
