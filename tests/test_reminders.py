"""Tests for lineup lock reminders (T-008).

The DST assertions are the point of this file. 11:30 America/Chicago is
16:30 UTC in September and 17:30 UTC in November; a fixed UTC cron would
drift a full hour and nothing would fail loudly. Every test freezes the
clock rather than reading the real one.

The tz-unavailable path is stubbed, never inferred from the machine: the
project venv has tzdata installed, so a test relying on the local
environment would pass for the wrong reason.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from poller import (
    REMINDER_WINDOW_MINUTES,
    Config,
    TimezoneUnavailable,
    default_state,
    due_reminder,
    local_now,
    process_reminders,
    render_reminder,
)

CT = ZoneInfo("America/Chicago")
UTC = timezone.utc

# Verified: Sep 13 2026 is a Sunday in CDT, Nov 8 2026 a Sunday in CST,
# Sep 10 a Thursday in CDT, Nov 12 a Thursday in CST.
SUNDAY_CDT = (2026, 9, 13)
SUNDAY_CST = (2026, 11, 8)
THURSDAY_CDT = (2026, 9, 10)


def ct(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=CT)


def utc(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def config(timezone_name="America/Chicago", lineup_reminders=True):
    return Config(
        league_id=1234567,
        league_year=2026,
        espn_s2="FAKE",
        swid="{FAKE}",
        webhook_url="https://example.invalid/hook",
        webhook_url_results="https://example.invalid/hook",
        timezone=timezone_name,
        lineup_reminders=lineup_reminders,
    )


class RecordingDiscord:
    def __init__(self):
        self.messages = []

    def post(self, content):
        self.messages.append(content)
        return 1


def fire(now, state=None, cfg=None, current_week=3):
    state = default_state() if state is None else state
    discord = RecordingDiscord()
    posted = process_reminders(
        cfg or config(), state, discord, current_week=current_week, now=now
    )
    return posted, state, discord


class TestWindowDetection(unittest.TestCase):
    def test_fires_thirty_minutes_before_sunday_kickoff(self):
        result = due_reminder(ct(*SUNDAY_CDT, 11, 30))
        self.assertIsNotNone(result)
        key, slot, remaining = result
        self.assertEqual(slot, "sunday")
        self.assertEqual(remaining, 30)

    def test_fires_thirty_minutes_before_thursday_kickoff(self):
        result = due_reminder(ct(*THURSDAY_CDT, 18, 45))
        self.assertIsNotNone(result)
        _, slot, remaining = result
        self.assertEqual(slot, "thursday")
        self.assertEqual(remaining, 30)

    def test_silent_well_before_the_window(self):
        self.assertIsNone(due_reminder(ct(*SUNDAY_CDT, 9, 0)))

    def test_silent_after_kickoff(self):
        # A reminder is useless once lineups have locked.
        self.assertIsNone(due_reminder(ct(*SUNDAY_CDT, 12, 1)))
        self.assertIsNone(due_reminder(ct(*SUNDAY_CDT, 15, 0)))

    def test_kickoff_minute_itself_is_closed(self):
        self.assertIsNone(due_reminder(ct(*SUNDAY_CDT, 12, 0)))

    def test_window_opens_exactly_on_schedule(self):
        opens = ct(*SUNDAY_CDT, 12, 0) - timedelta(minutes=REMINDER_WINDOW_MINUTES)
        self.assertIsNotNone(due_reminder(opens))
        self.assertIsNone(due_reminder(opens - timedelta(minutes=1)))

    def test_wrong_weekday_never_fires(self):
        for day in range(1, 8):
            when = ct(2026, 9, 7 + day - 1, 11, 30)  # Mon 7th through Sun 13th
            result = due_reminder(when)
            if when.weekday() == 6:
                self.assertIsNotNone(result)
            else:
                self.assertIsNone(result)

    def test_thursday_window_does_not_fire_on_sunday(self):
        self.assertIsNone(due_reminder(ct(*SUNDAY_CDT, 18, 45)))


class TestDaylightSaving(unittest.TestCase):
    """The reason reminders are computed at runtime instead of by UTC cron."""

    def test_same_wall_clock_fires_in_both_offsets(self):
        for date in (SUNDAY_CDT, SUNDAY_CST):
            with self.subTest(date=date):
                result = due_reminder(ct(*date, 11, 30))
                self.assertIsNotNone(result, "11:30 CT must fire regardless of DST")
                self.assertEqual(result[2], 30)

    def test_september_fires_at_1630_utc_not_1730(self):
        self.assertIsNotNone(due_reminder(local_now("America/Chicago", utc(2026, 9, 13, 16, 30))))
        self.assertIsNone(due_reminder(local_now("America/Chicago", utc(2026, 9, 13, 17, 30))))

    def test_november_fires_at_1730_utc_not_1630(self):
        self.assertIsNotNone(due_reminder(local_now("America/Chicago", utc(2026, 11, 8, 17, 30))))
        self.assertIsNone(due_reminder(local_now("America/Chicago", utc(2026, 11, 8, 16, 30))))

    def test_a_fixed_utc_cron_would_have_drifted(self):
        # 16:30 UTC is correct in September and an hour early in November.
        # This is the bug a second hardcoded-cron workflow would have shipped.
        september = due_reminder(local_now("America/Chicago", utc(2026, 9, 13, 16, 30)))
        november = due_reminder(local_now("America/Chicago", utc(2026, 11, 8, 16, 30)))
        self.assertIsNotNone(september)
        self.assertIsNone(november)

    def test_keys_use_the_local_date_not_utc(self):
        # 00:30 UTC Monday is still Sunday evening in Chicago.
        when = local_now("America/Chicago", utc(2026, 9, 13, 23, 45))
        self.assertEqual(when.date().isoformat(), "2026-09-13")


class TestCronCoverage(unittest.TestCase):
    """A 20-minute cron must never step over the window."""

    def simulate_day(self, date, start_minute_offset):
        """Run every 20 minutes through the day; count reminders posted."""
        state = default_state()
        total = 0
        cursor = ct(*date, 0, 0) + timedelta(minutes=start_minute_offset)
        end = ct(*date, 23, 59)
        while cursor < end:
            posted, state, _ = fire(cursor.astimezone(UTC), state=state)
            total += posted
            cursor += timedelta(minutes=20)
        return total

    def test_exactly_one_reminder_regardless_of_cron_phase(self):
        for offset in range(0, 20):
            with self.subTest(offset=offset):
                self.assertEqual(self.simulate_day(SUNDAY_CDT, offset), 1)

    def test_holds_across_the_dst_boundary(self):
        for offset in (0, 7, 13, 19):
            with self.subTest(offset=offset):
                self.assertEqual(self.simulate_day(SUNDAY_CST, offset), 1)

    def test_window_is_wider_than_the_cron_interval(self):
        # 20-minute cron plus GitHub's best-effort scheduling delay.
        self.assertGreater(REMINDER_WINDOW_MINUTES, 20)


class TestFiresOnce(unittest.TestCase):
    def test_repeated_runs_inside_the_window_post_once(self):
        state = default_state()
        posted, state, discord = fire(ct(*SUNDAY_CDT, 11, 30).astimezone(UTC), state=state)
        self.assertEqual(posted, 1)
        posted, state, discord = fire(ct(*SUNDAY_CDT, 11, 50).astimezone(UTC), state=state)
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])

    def test_key_format_matches_the_documented_shape(self):
        _, state, _ = fire(ct(*SUNDAY_CST, 11, 30).astimezone(UTC))
        self.assertEqual(state["posted_reminders"], ["2026-11-08-sunday"])

    def test_next_week_fires_again(self):
        _, state, _ = fire(ct(2026, 9, 13, 11, 30).astimezone(UTC))
        posted, state, _ = fire(ct(2026, 9, 20, 11, 30).astimezone(UTC), state=state)
        self.assertEqual(posted, 1)
        self.assertEqual(len(state["posted_reminders"]), 2)

    def test_thursday_and_sunday_are_separate_keys(self):
        _, state, _ = fire(ct(2026, 9, 10, 18, 45).astimezone(UTC))
        posted, state, _ = fire(ct(2026, 9, 13, 11, 30).astimezone(UTC), state=state)
        self.assertEqual(posted, 1)
        self.assertIn("2026-09-10-thursday", state["posted_reminders"])
        self.assertIn("2026-09-13-sunday", state["posted_reminders"])


class TestSuppression(unittest.TestCase):
    def test_disabled_by_config(self):
        posted, _, discord = fire(
            ct(*SUNDAY_CDT, 11, 30).astimezone(UTC), cfg=config(lineup_reminders=False)
        )
        self.assertEqual(posted, 0)
        self.assertEqual(discord.messages, [])

    def test_suppressed_outside_an_active_season(self):
        for week in (0, 19, 25, -1):
            with self.subTest(week=week):
                posted, _, _ = fire(ct(*SUNDAY_CDT, 11, 30).astimezone(UTC), current_week=week)
                self.assertEqual(posted, 0)

    def test_fires_through_the_fantasy_playoffs(self):
        for week in (1, 14, 15, 17, 18):
            with self.subTest(week=week):
                posted, _, _ = fire(ct(*SUNDAY_CDT, 11, 30).astimezone(UTC), current_week=week)
                self.assertEqual(posted, 1)

    def test_missing_current_week_suppresses(self):
        posted, _, _ = fire(ct(*SUNDAY_CDT, 11, 30).astimezone(UTC), current_week=None)
        self.assertEqual(posted, 0)


class TestMessage(unittest.TestCase):
    def test_matches_the_documented_wording(self):
        self.assertEqual(
            render_reminder("sunday", 30),
            "⏰ Lineups lock in 30 minutes for the Sunday early games.",
        )

    def test_thursday_wording(self):
        self.assertIn("Thursday night game", render_reminder("thursday", 30))

    def test_reports_the_real_remaining_time(self):
        # Firing late in the window and still claiming "30 minutes" would be
        # actively misleading.
        _, _, discord = fire(ct(*SUNDAY_CDT, 11, 45).astimezone(UTC))
        self.assertIn("15 minutes", discord.messages[0])

    def test_singular_minute(self):
        self.assertIn("1 minute for", render_reminder("sunday", 1))

    def test_message_is_short(self):
        self.assertLess(len(render_reminder("sunday", 30)), 100)


class TestTimezoneFailure(unittest.TestCase):
    """Stubbed, never inferred from the machine -- this venv has tzdata."""

    def test_missing_tz_database_raises(self):
        with mock.patch("poller.ZoneInfo", side_effect=ZoneInfoNotFoundError("no tzdata")):
            with self.assertRaises(TimezoneUnavailable):
                local_now("America/Chicago", utc(2026, 9, 13, 16, 30))

    def test_failure_is_not_swallowed_into_silence(self):
        # Skipping reminders quietly for a whole season is the failure mode
        # this project exists to prevent.
        with mock.patch("poller.ZoneInfo", side_effect=ZoneInfoNotFoundError("no tzdata")):
            with self.assertRaises(TimezoneUnavailable):
                process_reminders(
                    config(),
                    default_state(),
                    RecordingDiscord(),
                    current_week=3,
                    now=utc(2026, 9, 13, 16, 30),
                )

    def test_error_names_the_likely_cause(self):
        with mock.patch("poller.ZoneInfo", side_effect=ZoneInfoNotFoundError("no tzdata")):
            with self.assertRaises(TimezoneUnavailable) as ctx:
                local_now("America/Chicago", utc(2026, 9, 13, 16, 30))
        self.assertIn("tzdata", str(ctx.exception))

    def test_garbage_timezone_name_raises(self):
        with self.assertRaises(TimezoneUnavailable):
            local_now("Not/AZone", utc(2026, 9, 13, 16, 30))

    def test_suppression_short_circuits_before_the_tz_lookup(self):
        # A broken tz must not stop a user who disabled reminders anyway.
        with mock.patch("poller.ZoneInfo", side_effect=ZoneInfoNotFoundError("no tzdata")):
            posted = process_reminders(
                config(lineup_reminders=False),
                default_state(),
                RecordingDiscord(),
                current_week=3,
                now=utc(2026, 9, 13, 16, 30),
            )
        self.assertEqual(posted, 0)


class TestAlternateTimezone(unittest.TestCase):
    def test_eastern_league_fires_on_its_own_wall_clock(self):
        cfg = config(timezone_name="America/New_York")
        # 11:30 Eastern is 10:30 Central; only the configured zone matters.
        posted, _, _ = fire(
            datetime(2026, 9, 13, 11, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC),
            cfg=cfg,
        )
        self.assertEqual(posted, 1)

    def test_naive_datetime_is_rejected(self):
        with self.assertRaises(ValueError):
            local_now("America/Chicago", datetime(2026, 9, 13, 16, 30))


if __name__ == "__main__":
    unittest.main()
