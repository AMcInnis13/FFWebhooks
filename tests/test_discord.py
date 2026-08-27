"""Tests for the Discord posting layer (T-004).

No socket is ever opened and no test actually sleeps -- both the session and
the sleep function are injected. Webhook URLs here are fake; see CLAUDE.md
section 6.
"""

import io
import unittest
from contextlib import redirect_stdout

import requests

from poller import (
    DISCORD_CONTENT_LIMIT,
    MAX_RATE_LIMIT_RETRIES,
    MAX_RETRY_AFTER_SECONDS,
    POST_DELAY_SECONDS,
    Config,
    Discord,
    DiscordError,
    DiscordRouter,
    split_content,
)

FAKE_WEBHOOK = "https://example.invalid/api/webhooks/1234/SUPER-SECRET-TOKEN"


class FakeResponse:
    def __init__(self, status_code, json_body=None, headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class FakeSession:
    """Records calls and returns queued responses; 204 once the queue drains."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(204)


class ExplodingSession:
    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(url)
        raise self.exc


def make_discord(responses=None, session=None, **kwargs):
    sleeps = []
    poster = Discord(
        FAKE_WEBHOOK,
        session=session if session is not None else FakeSession(responses),
        sleep=sleeps.append,
        **kwargs,
    )
    return poster, sleeps


class TestSplitContent(unittest.TestCase):
    def test_empty_and_blank_produce_no_chunks(self):
        for raw in ("", None, "   ", "\n\n\n"):
            with self.subTest(raw=raw):
                self.assertEqual(split_content(raw), [])

    def test_short_text_is_one_chunk(self):
        self.assertEqual(split_content("hello"), ["hello"])

    def test_exactly_at_the_limit_is_one_chunk(self):
        text = "x" * DISCORD_CONTENT_LIMIT
        chunks = split_content(text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), DISCORD_CONTENT_LIMIT)

    def test_one_over_the_limit_splits(self):
        self.assertEqual(len(split_content("x" * (DISCORD_CONTENT_LIMIT + 1))), 2)

    def test_no_chunk_exceeds_the_limit(self):
        text = "\n".join(f"line {i} " + "y" * 60 for i in range(200))
        for chunk in split_content(text):
            self.assertLessEqual(len(chunk), DISCORD_CONTENT_LIMIT)

    def test_splits_on_line_boundaries(self):
        text = "\n".join(f"line-{i:03d}" for i in range(500))
        for chunk in split_content(text):
            for line in chunk.split("\n"):
                self.assertRegex(line, r"^line-\d{3}$")

    def test_no_content_is_lost(self):
        text = "\n".join(f"line {i} " + "z" * 50 for i in range(300))
        joined = "".join(chunk.replace("\n", "") for chunk in split_content(text))
        self.assertEqual(joined, text.replace("\n", ""))

    def test_oversized_single_line_is_hard_split(self):
        text = "q" * (DISCORD_CONTENT_LIMIT * 2 + 5)
        chunks = split_content(text)
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), text)

    def test_oversized_line_mixed_with_normal_lines_loses_nothing(self):
        text = "short before\n" + "w" * (DISCORD_CONTENT_LIMIT + 10) + "\nshort after"
        chunks = split_content(text)
        joined = "".join(chunk.replace("\n", "") for chunk in chunks)
        self.assertEqual(joined, text.replace("\n", ""))
        self.assertIn("short before", chunks[0])
        self.assertIn("short after", chunks[-1])

    def test_interior_blank_lines_are_preserved(self):
        self.assertEqual(split_content("a\n\nb"), ["a\n\nb"])


class TestPayloadShape(unittest.TestCase):
    def test_username_and_content_are_sent(self):
        poster, _ = make_discord(username="Fantasy Notifier")
        poster.post("hello world")
        payload = poster._session.calls[0]["json"]
        self.assertEqual(payload["username"], "Fantasy Notifier")
        self.assertEqual(payload["content"], "hello world")

    def test_allowed_mentions_suppresses_every_ping(self):
        poster, _ = make_discord()
        poster.post("@everyone the Waiver Wolves traded @here")
        self.assertEqual(poster._session.calls[0]["json"]["allowed_mentions"], {"parse": []})

    def test_allowed_mentions_present_on_every_chunk(self):
        poster, _ = make_discord()
        poster.post("\n".join("line " + "k" * 80 for _ in range(120)))
        self.assertGreater(len(poster._session.calls), 1)
        for call in poster._session.calls:
            self.assertEqual(call["json"]["allowed_mentions"], {"parse": []})

    def test_a_timeout_is_always_set(self):
        # Without one a hung connection would stall the job to the Actions
        # six-hour ceiling.
        poster, _ = make_discord()
        poster.post("hello")
        self.assertIsNotNone(poster._session.calls[0]["timeout"])

    def test_posts_to_the_configured_url(self):
        poster, _ = make_discord()
        poster.post("hello")
        self.assertEqual(poster._session.calls[0]["url"], FAKE_WEBHOOK)


class TestPostCounts(unittest.TestCase):
    def test_blank_content_sends_nothing(self):
        poster, _ = make_discord()
        self.assertEqual(poster.post("   "), 0)
        self.assertEqual(poster._session.calls, [])

    def test_short_content_is_a_single_post(self):
        poster, _ = make_discord()
        self.assertEqual(poster.post("hi"), 1)
        self.assertEqual(len(poster._session.calls), 1)

    def test_long_content_spans_multiple_posts_with_nothing_lost(self):
        text = "\n".join(f"{i}: " + "m" * 90 for i in range(120))
        poster, _ = make_discord()
        sent = poster.post(text)

        self.assertGreater(sent, 1)
        self.assertEqual(sent, len(poster._session.calls))
        rebuilt = "".join(
            call["json"]["content"].replace("\n", "") for call in poster._session.calls
        )
        self.assertEqual(rebuilt, text.replace("\n", ""))
        for call in poster._session.calls:
            self.assertLessEqual(len(call["json"]["content"]), DISCORD_CONTENT_LIMIT)


class TestThrottling(unittest.TestCase):
    def test_first_post_does_not_sleep(self):
        poster, sleeps = make_discord()
        poster.post("hi")
        self.assertEqual(sleeps, [])

    def test_sleeps_between_chunks(self):
        poster, sleeps = make_discord()
        chunks = poster.post("\n".join("line " + "j" * 80 for _ in range(120)))
        self.assertEqual(len(sleeps), chunks - 1)
        self.assertTrue(all(s == POST_DELAY_SECONDS for s in sleeps))

    def test_sleeps_between_separate_messages(self):
        poster, sleeps = make_discord()
        poster.post("first")
        poster.post("second")
        poster.post("third")
        self.assertEqual(sleeps, [POST_DELAY_SECONDS, POST_DELAY_SECONDS])


class TestRateLimiting(unittest.TestCase):
    def test_429_then_200_retries_and_succeeds(self):
        poster, sleeps = make_discord(
            responses=[FakeResponse(429, {"retry_after": 1.5}), FakeResponse(204)]
        )
        self.assertEqual(poster.post("hello"), 1)
        self.assertEqual(len(poster._session.calls), 2)
        self.assertIn(1.5, sleeps)

    def test_retry_after_read_from_header_when_body_has_none(self):
        poster, sleeps = make_discord(
            responses=[FakeResponse(429, None, {"Retry-After": "2.25"}), FakeResponse(204)]
        )
        poster.post("hello")
        self.assertIn(2.25, sleeps)

    def test_body_wins_over_header(self):
        poster, sleeps = make_discord(
            responses=[
                FakeResponse(429, {"retry_after": 0.5}, {"Retry-After": "30"}),
                FakeResponse(204),
            ]
        )
        poster.post("hello")
        self.assertIn(0.5, sleeps)
        self.assertNotIn(30.0, sleeps)

    def test_missing_retry_after_uses_a_default(self):
        poster, sleeps = make_discord(responses=[FakeResponse(429), FakeResponse(204)])
        poster.post("hello")
        self.assertTrue(any(s > 0 for s in sleeps))

    def test_absurd_retry_after_is_clamped(self):
        poster, sleeps = make_discord(
            responses=[FakeResponse(429, {"retry_after": 99999}), FakeResponse(204)]
        )
        poster.post("hello")
        self.assertTrue(all(s <= MAX_RETRY_AFTER_SECONDS for s in sleeps))

    def test_negative_retry_after_does_not_go_below_zero(self):
        poster, sleeps = make_discord(
            responses=[FakeResponse(429, {"retry_after": -5}), FakeResponse(204)]
        )
        poster.post("hello")
        self.assertTrue(all(s >= 0 for s in sleeps))

    def test_persistent_429_eventually_raises(self):
        responses = [FakeResponse(429, {"retry_after": 0.1})] * (MAX_RATE_LIMIT_RETRIES + 1)
        poster, _ = make_discord(responses=responses)
        with self.assertRaises(DiscordError) as ctx:
            poster.post("hello")
        self.assertIn("rate limited", str(ctx.exception))


class TestErrorHandling(unittest.TestCase):
    def test_non_2xx_raises(self):
        for status in (400, 401, 403, 404, 500, 503):
            with self.subTest(status=status):
                poster, _ = make_discord(responses=[FakeResponse(status)])
                with self.assertRaises(DiscordError) as ctx:
                    poster.post("hello")
                self.assertIn(str(status), str(ctx.exception))

    def test_non_2xx_is_not_retried(self):
        poster, _ = make_discord(responses=[FakeResponse(500), FakeResponse(204)])
        with self.assertRaises(DiscordError):
            poster.post("hello")
        self.assertEqual(len(poster._session.calls), 1)

    def test_2xx_variants_all_succeed(self):
        for status in (200, 201, 202, 204):
            with self.subTest(status=status):
                poster, _ = make_discord(responses=[FakeResponse(status)])
                self.assertEqual(poster.post("hello"), 1)

    def test_network_error_becomes_a_discord_error(self):
        poster, _ = make_discord(
            session=ExplodingSession(requests.ConnectionError("connection refused"))
        )
        with self.assertRaises(DiscordError):
            poster.post("hello")

    def test_timeout_becomes_a_discord_error(self):
        poster, _ = make_discord(session=ExplodingSession(requests.Timeout("timed out")))
        with self.assertRaises(DiscordError):
            poster.post("hello")


class TestWebhookUrlNeverLeaks(unittest.TestCase):
    """The webhook URL is a credential and Actions logs are public."""

    SECRET = "SUPER-SECRET-TOKEN"

    def assert_clean(self, exc):
        rendered = f"{exc}{getattr(exc, 'args', '')}"
        self.assertNotIn(self.SECRET, rendered)
        self.assertNotIn(FAKE_WEBHOOK, rendered)

    def test_http_error_message_is_clean(self):
        poster, _ = make_discord(responses=[FakeResponse(500)])
        with self.assertRaises(DiscordError) as ctx:
            poster.post("hello")
        self.assert_clean(ctx.exception)

    def test_rate_limit_exhaustion_message_is_clean(self):
        responses = [FakeResponse(429, {"retry_after": 0})] * (MAX_RATE_LIMIT_RETRIES + 1)
        poster, _ = make_discord(responses=responses)
        with self.assertRaises(DiscordError) as ctx:
            poster.post("hello")
        self.assert_clean(ctx.exception)

    def test_network_error_message_is_clean(self):
        # requests embeds the full URL in its own exception text, so this
        # would leak if the original were chained or interpolated.
        poster, _ = make_discord(
            session=ExplodingSession(
                requests.ConnectionError(f"failed to reach {FAKE_WEBHOOK}")
            )
        )
        with self.assertRaises(DiscordError) as ctx:
            poster.post("hello")
        self.assert_clean(ctx.exception)

    def test_network_error_does_not_chain_the_leaky_original(self):
        poster, _ = make_discord(
            session=ExplodingSession(
                requests.ConnectionError(f"failed to reach {FAKE_WEBHOOK}")
            )
        )
        with self.assertRaises(DiscordError) as ctx:
            poster.post("hello")
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)


class TestDryRun(unittest.TestCase):
    def test_no_request_is_made(self):
        poster, _ = make_discord(dry_run=True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            poster.post("hello world")
        self.assertEqual(poster._session.calls, [])

    def test_content_is_printed(self):
        poster, _ = make_discord(dry_run=True, label="transactions")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            poster.post("Trade processed")
        output = buffer.getvalue()
        self.assertIn("Trade processed", output)
        self.assertIn("transactions", output)

    def test_long_content_still_reports_each_chunk(self):
        poster, _ = make_discord(dry_run=True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            sent = poster.post("\n".join("line " + "p" * 80 for _ in range(120)))
        self.assertGreater(sent, 1)
        self.assertEqual(buffer.getvalue().count("would POST"), sent)

    def test_dry_run_never_sleeps(self):
        poster, sleeps = make_discord(dry_run=True)
        with redirect_stdout(io.StringIO()):
            poster.post("a")
            poster.post("b")
        self.assertEqual(sleeps, [])


class TestDiscordRouter(unittest.TestCase):
    """Routing categories to channels, and sharing instances per channel."""

    MAIN = "https://example.invalid/hook/main"
    TRADES = "https://example.invalid/hook/trades"
    ROSTER = "https://example.invalid/hook/roster"
    RESULTS = "https://example.invalid/hook/results"

    def config(self, **overrides):
        values = {
            "league_id": 1234567,
            "league_year": 2026,
            "espn_s2": "FAKE",
            "swid": "{FAKE}",
            "webhook_url": self.MAIN,
            "webhook_url_results": self.MAIN,
            "webhook_url_trades": self.MAIN,
            "webhook_url_roster": self.MAIN,
            "timezone": "America/Chicago",
            "lineup_reminders": True,
        }
        values.update(overrides)
        return Config(**values)

    def router(self, config=None):
        return DiscordRouter(
            config or self.config(), session=FakeSession(), sleep=lambda _: None
        )

    def test_single_channel_setup_uses_one_instance(self):
        # The throttle lives on the instance. Four instances aimed at one
        # channel would each think they were posting first and could burst
        # straight into a 429.
        router = self.router()
        self.assertEqual(router.channel_count, 1)
        for category in ("trades", "roster", "results"):
            with self.subTest(category=category):
                self.assertIs(router.for_category(category), router.main)

    def test_fully_split_setup_uses_four_instances(self):
        router = self.router(
            self.config(
                webhook_url_trades=self.TRADES,
                webhook_url_roster=self.ROSTER,
                webhook_url_results=self.RESULTS,
            )
        )
        self.assertEqual(router.channel_count, 4)
        urls = {
            category: router.for_category(category).webhook_url
            for category in ("main", "trades", "roster", "results")
        }
        self.assertEqual(
            urls,
            {
                "main": self.MAIN,
                "trades": self.TRADES,
                "roster": self.ROSTER,
                "results": self.RESULTS,
            },
        )

    def test_categories_sharing_a_url_share_one_instance(self):
        router = self.router(
            self.config(webhook_url_trades=self.TRADES, webhook_url_roster=self.TRADES)
        )
        self.assertEqual(router.channel_count, 2)
        self.assertTrue(router.shares_channel("trades", "roster"))
        self.assertFalse(router.shares_channel("trades", "main"))

    def test_throttle_is_shared_across_categories_on_one_channel(self):
        sleeps = []
        router = DiscordRouter(self.config(), session=FakeSession(), sleep=sleeps.append)
        router.post("trades", "a")
        router.post("roster", "b")
        router.post("results", "c")
        # First post never sleeps; the next two must, because they land in
        # the same channel.
        self.assertEqual(len(sleeps), 2)

    def test_separate_channels_do_not_throttle_each_other(self):
        sleeps = []
        router = DiscordRouter(
            self.config(webhook_url_trades=self.TRADES, webhook_url_roster=self.ROSTER),
            session=FakeSession(),
            sleep=sleeps.append,
        )
        router.post("trades", "a")
        router.post("roster", "b")
        self.assertEqual(sleeps, [])

    def test_posts_reach_the_right_url(self):
        session = FakeSession()
        router = DiscordRouter(
            self.config(webhook_url_trades=self.TRADES),
            session=session,
            sleep=lambda _: None,
        )
        router.post("trades", "trade message")
        router.post("results", "results message")
        self.assertEqual(session.calls[0]["url"], self.TRADES)
        self.assertEqual(session.calls[1]["url"], self.MAIN)

    def test_unknown_category_goes_to_main(self):
        router = self.router(self.config(webhook_url_trades=self.TRADES))
        self.assertIs(router.for_category("nonsense"), router.main)

    def test_dry_run_propagates_to_every_channel(self):
        router = DiscordRouter(
            self.config(webhook_url_trades=self.TRADES), dry_run=True, sleep=lambda _: None
        )
        for category in ("main", "trades", "roster", "results"):
            with self.subTest(category=category):
                self.assertTrue(router.for_category(category).dry_run)

    def test_each_channel_is_labelled_for_dry_run_output(self):
        router = self.router(self.config(webhook_url_trades=self.TRADES))
        self.assertTrue(router.for_category("trades").label)
        self.assertNotEqual(
            router.for_category("trades").label, router.main.label
        )


if __name__ == "__main__":
    unittest.main()
