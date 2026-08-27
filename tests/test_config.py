"""Tests for the configuration layer (T-002).

All values here are obviously fake. Never put a real league id, cookie, or
Discord webhook URL in a test -- this repo is public. See CLAUDE.md section 6.
"""

import unittest

from poller import (
    DEFAULT_TIMEZONE,
    Config,
    ConfigError,
    load_config,
    normalize_swid,
    parse_bool,
)

FAKE_SWID_INNER = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
FAKE_WEBHOOK = "https://example.invalid/webhook/main"
FAKE_WEBHOOK_RESULTS = "https://example.invalid/webhook/results"
FAKE_WEBHOOK_TRADES = "https://example.invalid/webhook/trades"
FAKE_WEBHOOK_ROSTER = "https://example.invalid/webhook/roster"
FAKE_S2 = "FAKE_ESPN_S2_COOKIE_VALUE"


def env(**overrides):
    """A valid environment, with overrides applied. None removes a key."""
    base = {
        "LEAGUE_ID": "1234567",
        "LEAGUE_YEAR": "2026",
        "ESPN_S2": FAKE_S2,
        "SWID": "{" + FAKE_SWID_INNER + "}",
        "DISCORD_WEBHOOK_URL": FAKE_WEBHOOK,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


class TestRequiredVars(unittest.TestCase):
    def test_valid_env_loads(self):
        cfg = load_config(env())
        self.assertIsInstance(cfg, Config)
        self.assertEqual(cfg.league_id, 1234567)
        self.assertEqual(cfg.league_year, 2026)
        self.assertEqual(cfg.espn_s2, FAKE_S2)

    def test_each_required_var_is_enforced(self):
        for name in ("LEAGUE_ID", "LEAGUE_YEAR", "ESPN_S2", "SWID", "DISCORD_WEBHOOK_URL"):
            with self.subTest(missing=name):
                with self.assertRaises(ConfigError) as ctx:
                    load_config(env(**{name: None}))
                self.assertIn(name, str(ctx.exception))

    def test_all_missing_vars_reported_together(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({})
        message = str(ctx.exception)
        for name in ("LEAGUE_ID", "LEAGUE_YEAR", "ESPN_S2", "SWID", "DISCORD_WEBHOOK_URL"):
            self.assertIn(name, message)

    def test_whitespace_only_counts_as_missing(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(env(ESPN_S2="   "))
        self.assertIn("ESPN_S2", str(ctx.exception))

    def test_empty_string_counts_as_missing(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(env(DISCORD_WEBHOOK_URL=""))
        self.assertIn("DISCORD_WEBHOOK_URL", str(ctx.exception))


class TestSwidNormalization(unittest.TestCase):
    def test_already_braced_is_unchanged(self):
        self.assertEqual(normalize_swid("{" + FAKE_SWID_INNER + "}"), "{" + FAKE_SWID_INNER + "}")

    def test_unbraced_gains_braces(self):
        self.assertEqual(normalize_swid(FAKE_SWID_INNER), "{" + FAKE_SWID_INNER + "}")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(
            normalize_swid("  {" + FAKE_SWID_INNER + "}  "), "{" + FAKE_SWID_INNER + "}"
        )

    def test_whitespace_inside_braces_is_stripped(self):
        self.assertEqual(
            normalize_swid("{  " + FAKE_SWID_INNER + "  }"), "{" + FAKE_SWID_INNER + "}"
        )

    def test_unbalanced_braces_are_repaired(self):
        self.assertEqual(normalize_swid("{" + FAKE_SWID_INNER), "{" + FAKE_SWID_INNER + "}")
        self.assertEqual(normalize_swid(FAKE_SWID_INNER + "}"), "{" + FAKE_SWID_INNER + "}")

    def test_braces_only_is_rejected(self):
        with self.assertRaises(ConfigError):
            normalize_swid("{}")

    def test_load_config_normalizes_unbraced_swid(self):
        cfg = load_config(env(SWID=FAKE_SWID_INNER))
        self.assertEqual(cfg.swid, "{" + FAKE_SWID_INNER + "}")


class TestIntegerVars(unittest.TestCase):
    def test_non_numeric_league_year_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(env(LEAGUE_YEAR="twenty twenty six"))
        self.assertIn("LEAGUE_YEAR", str(ctx.exception))

    def test_non_numeric_league_id_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(env(LEAGUE_ID="not-a-number"))
        self.assertIn("LEAGUE_ID", str(ctx.exception))

    def test_league_id_value_is_redacted_from_the_error(self):
        # The league id is private; an invalid value must not echo into a
        # public Actions log.
        with self.assertRaises(ConfigError) as ctx:
            load_config(env(LEAGUE_ID="secret-ish-garbage"))
        self.assertNotIn("secret-ish-garbage", str(ctx.exception))
        self.assertIn("<redacted>", str(ctx.exception))

    def test_surrounding_whitespace_is_tolerated(self):
        cfg = load_config(env(LEAGUE_ID=" 1234567 ", LEAGUE_YEAR=" 2026 "))
        self.assertEqual(cfg.league_id, 1234567)
        self.assertEqual(cfg.league_year, 2026)


class TestTimezone(unittest.TestCase):
    def test_defaults_to_america_chicago(self):
        self.assertEqual(load_config(env()).timezone, DEFAULT_TIMEZONE)
        self.assertEqual(DEFAULT_TIMEZONE, "America/Chicago")

    def test_override_is_respected(self):
        self.assertEqual(load_config(env(TIMEZONE="America/New_York")).timezone, "America/New_York")

    def test_blank_falls_back_to_default(self):
        self.assertEqual(load_config(env(TIMEZONE="   ")).timezone, DEFAULT_TIMEZONE)

    def test_unknown_timezone_is_not_rejected_at_config_time(self):
        # Resolving the tz needs the tz database, which bare Windows lacks.
        # Failing here would take down transactions and results too, so the
        # reminders feature owns this check (T-008).
        self.assertEqual(load_config(env(TIMEZONE="Not/AZone")).timezone, "Not/AZone")


class TestWebhookFallback(unittest.TestCase):
    def test_results_falls_back_to_main_when_unset(self):
        cfg = load_config(env())
        self.assertEqual(cfg.webhook_url, FAKE_WEBHOOK)
        self.assertEqual(cfg.webhook_url_results, FAKE_WEBHOOK)

    def test_results_falls_back_when_blank(self):
        cfg = load_config(env(DISCORD_WEBHOOK_URL_RESULTS="  "))
        self.assertEqual(cfg.webhook_url_results, FAKE_WEBHOOK)

    def test_explicit_results_webhook_is_used(self):
        cfg = load_config(env(DISCORD_WEBHOOK_URL_RESULTS=FAKE_WEBHOOK_RESULTS))
        self.assertEqual(cfg.webhook_url, FAKE_WEBHOOK)
        self.assertEqual(cfg.webhook_url_results, FAKE_WEBHOOK_RESULTS)


class TestPerCategoryWebhooks(unittest.TestCase):
    """Each category routes to its own channel, or falls back to the main one."""

    def test_all_categories_default_to_the_main_webhook(self):
        # An install that configures nothing extra must behave exactly as it
        # did before routing existed.
        cfg = load_config(env())
        for category in ("main", "trades", "roster", "results"):
            with self.subTest(category=category):
                self.assertEqual(cfg.webhook_for(category), FAKE_WEBHOOK)

    def test_each_category_can_be_routed_separately(self):
        cfg = load_config(
            env(
                DISCORD_WEBHOOK_URL_TRADES=FAKE_WEBHOOK_TRADES,
                DISCORD_WEBHOOK_URL_ROSTER=FAKE_WEBHOOK_ROSTER,
                DISCORD_WEBHOOK_URL_RESULTS=FAKE_WEBHOOK_RESULTS,
            )
        )
        self.assertEqual(cfg.webhook_for("trades"), FAKE_WEBHOOK_TRADES)
        self.assertEqual(cfg.webhook_for("roster"), FAKE_WEBHOOK_ROSTER)
        self.assertEqual(cfg.webhook_for("results"), FAKE_WEBHOOK_RESULTS)
        self.assertEqual(cfg.webhook_for("main"), FAKE_WEBHOOK)

    def test_partial_configuration_falls_back_per_category(self):
        cfg = load_config(env(DISCORD_WEBHOOK_URL_TRADES=FAKE_WEBHOOK_TRADES))
        self.assertEqual(cfg.webhook_for("trades"), FAKE_WEBHOOK_TRADES)
        self.assertEqual(cfg.webhook_for("roster"), FAKE_WEBHOOK)
        self.assertEqual(cfg.webhook_for("results"), FAKE_WEBHOOK)

    def test_blank_values_fall_back(self):
        cfg = load_config(env(DISCORD_WEBHOOK_URL_TRADES="   ", DISCORD_WEBHOOK_URL_ROSTER=""))
        self.assertEqual(cfg.webhook_for("trades"), FAKE_WEBHOOK)
        self.assertEqual(cfg.webhook_for("roster"), FAKE_WEBHOOK)

    def test_unknown_category_falls_back_rather_than_raising(self):
        # Misfiling a message is recoverable; dropping one is not.
        self.assertEqual(load_config(env()).webhook_for("nonsense"), FAKE_WEBHOOK)

    def test_reminders_and_errors_are_not_separately_routable(self):
        # They ride CATEGORY_MAIN on purpose: an error sent to a quiet side
        # channel is barely better than no error at all.
        cfg = load_config(env(DISCORD_WEBHOOK_URL_TRADES=FAKE_WEBHOOK_TRADES))
        self.assertEqual(cfg.webhook_for("main"), FAKE_WEBHOOK)

    def test_new_webhooks_are_hidden_from_repr(self):
        cfg = load_config(
            env(
                DISCORD_WEBHOOK_URL_TRADES=FAKE_WEBHOOK_TRADES,
                DISCORD_WEBHOOK_URL_ROSTER=FAKE_WEBHOOK_ROSTER,
            )
        )
        rendered = repr(cfg)
        self.assertNotIn(FAKE_WEBHOOK_TRADES, rendered)
        self.assertNotIn(FAKE_WEBHOOK_ROSTER, rendered)


class TestLineupReminders(unittest.TestCase):
    def test_defaults_to_true(self):
        self.assertTrue(load_config(env()).lineup_reminders)

    def test_truthy_values(self):
        for raw in ("true", "TRUE", "True", "1", "yes", "Y", "on", " true "):
            with self.subTest(raw=raw):
                self.assertTrue(load_config(env(LINEUP_REMINDERS=raw)).lineup_reminders)

    def test_falsy_values(self):
        for raw in ("false", "FALSE", "False", "0", "no", "N", "off", " false "):
            with self.subTest(raw=raw):
                self.assertFalse(load_config(env(LINEUP_REMINDERS=raw)).lineup_reminders)

    def test_blank_uses_default(self):
        self.assertTrue(load_config(env(LINEUP_REMINDERS="   ")).lineup_reminders)

    def test_unrecognised_value_is_an_error(self):
        # Silently defaulting a typo is how a feature disappears for a season.
        with self.assertRaises(ConfigError) as ctx:
            load_config(env(LINEUP_REMINDERS="ture"))
        self.assertIn("LINEUP_REMINDERS", str(ctx.exception))

    def test_parse_bool_default_is_returned_for_none(self):
        self.assertTrue(parse_bool(None, True, var_name="X"))
        self.assertFalse(parse_bool(None, False, var_name="X"))


class TestSecretsDoNotLeak(unittest.TestCase):
    """repr(Config) must never expose a credential -- Actions logs are public."""

    def setUp(self):
        self.cfg = load_config(env(DISCORD_WEBHOOK_URL_RESULTS=FAKE_WEBHOOK_RESULTS))

    def test_repr_hides_every_secret(self):
        rendered = repr(self.cfg)
        for secret in (FAKE_S2, FAKE_SWID_INNER, FAKE_WEBHOOK, FAKE_WEBHOOK_RESULTS, "1234567"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)

    def test_repr_still_shows_safe_fields(self):
        rendered = repr(self.cfg)
        self.assertIn("2026", rendered)
        self.assertIn("America/Chicago", rendered)

    def test_values_are_still_accessible(self):
        # Hidden from repr, not from the program.
        self.assertEqual(self.cfg.espn_s2, FAKE_S2)
        self.assertEqual(self.cfg.webhook_url, FAKE_WEBHOOK)


if __name__ == "__main__":
    unittest.main()
