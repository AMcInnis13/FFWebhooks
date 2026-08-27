"""Tests for the README (T-013).

Documentation drifts silently. These assertions pin the parts a user cannot
succeed without -- and the two warnings that cost real money or real
confusion if they go missing: the public-repo Actions minutes note and the
annual LEAGUE_YEAR bump.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README_PATH = os.path.join(REPO_ROOT, "README.md")

REQUIRED_ENV_VARS = (
    "LEAGUE_ID",
    "LEAGUE_YEAR",
    "ESPN_S2",
    "SWID",
    "DISCORD_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL_TRADES",
    "DISCORD_WEBHOOK_URL_ROSTER",
    "DISCORD_WEBHOOK_URL_RESULTS",
    "TIMEZONE",
    "LINEUP_REMINDERS",
)


class ReadmeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(README_PATH, encoding="utf-8") as handle:
            cls.text = handle.read()
        cls.lower = cls.text.lower()
        cls.headings = re.findall(r"(?m)^#{1,4}\s+(.+?)\s*$", cls.text)

    def assert_heading_matching(self, pattern):
        for heading in self.headings:
            if re.search(pattern, heading, re.IGNORECASE):
                return heading
        self.fail(f"no heading matching {pattern!r}; found {self.headings}")

    # The two overrides below keep unittest's exact semantics and argument
    # order. They exist only to shorten failure output: the defaults render
    # both operands, and an 8KB README buries the actual problem.

    def assertIn(self, member, container, msg=None):
        if member not in container:
            self.fail(msg or f"README does not contain {member!r}")

    def assertRegex(self, text, expected_regex, msg=None):
        if re.search(expected_regex, text) is None:
            self.fail(msg or f"README does not match {expected_regex!r}")


class TestStructure(ReadmeTestCase):
    def test_readme_exists(self):
        self.assertTrue(os.path.exists(README_PATH))

    def test_has_a_title(self):
        self.assertRegex(self.text, r"(?m)\A# \S")

    def test_has_setup_instructions(self):
        self.assert_heading_matching(r"setup")

    def test_has_a_configuration_reference(self):
        self.assert_heading_matching(r"configuration")

    def test_documents_every_environment_variable(self):
        for name in REQUIRED_ENV_VARS:
            with self.subTest(variable=name):
                self.assertIn(name, self.text)

    def test_says_which_variables_are_required(self):
        self.assertIn("Required", self.text)


class TestEspnCookies(ReadmeTestCase):
    def test_explains_where_to_find_the_cookies(self):
        self.assert_heading_matching(r"cookie")

    def test_names_both_cookies_exactly(self):
        self.assertIn("espn_s2", self.text)
        self.assertIn("SWID", self.text)

    def test_mentions_developer_tools(self):
        self.assertTrue(
            any(term in self.lower for term in ("developer tools", "devtools", "f12")),
            "no instruction for opening browser developer tools",
        )

    def test_mentions_the_cookies_storage_location(self):
        self.assertIn("cookies", self.lower)
        self.assertTrue(
            any(term in self.lower for term in ("application", "storage")),
            "no mention of the Application/Storage panel",
        )

    def test_warns_the_cookies_are_credentials(self):
        self.assertIn("password", self.lower)

    def test_explains_the_swid_braces(self):
        self.assertIn("braces", self.lower)

    def test_explains_where_to_find_the_league_id(self):
        self.assertIn("league id", self.lower)


class TestDiscordWebhook(ReadmeTestCase):
    def test_has_a_webhook_section(self):
        self.assert_heading_matching(r"webhook")

    def test_gives_the_exact_discord_menu_path(self):
        for step in ("Server Settings", "Integrations", "Webhooks"):
            with self.subTest(step=step):
                self.assertIn(step, self.text)

    def test_warns_the_webhook_url_is_a_credential(self):
        self.assertIn("credential", self.lower)


class TestChannelRouting(ReadmeTestCase):
    def test_has_a_routing_section(self):
        self.assert_heading_matching(r"routing|multiple channels")

    def test_states_that_a_webhook_is_bound_to_one_channel(self):
        # The whole reason multiple webhooks are needed.
        self.assertRegex(self.text, r"(?i)bound to \*\*one\*\* channel|bound to one channel")

    def test_lists_what_lands_in_each_channel(self):
        for term in ("Trades", "Waiver claims", "Weekly results"):
            with self.subTest(term=term):
                self.assertIn(term, self.text)

    def test_says_the_extra_webhooks_are_optional(self):
        self.assertRegex(self.text, r"(?i)optional except the first|falls back to")

    def test_explains_the_fallback(self):
        self.assertRegex(self.text, r"(?i)falls? back to\s+`?DISCORD_WEBHOOK_URL`?")

    def test_explains_why_errors_are_not_routable(self):
        # A warning in a channel nobody watches is nearly no warning at all.
        self.assertRegex(self.text, r"(?i)errors? and the startup confirmation always go")

    def test_points_at_dry_run_for_previewing_routing(self):
        self.assertIn("--dry-run", self.text)


class TestSecretsSetup(ReadmeTestCase):
    def test_explains_where_repository_secrets_live(self):
        self.assertIn("Secrets and variables", self.text)
        self.assertIn("Actions", self.text)

    def test_distinguishes_secrets_from_variables(self):
        # TIMEZONE and LINEUP_REMINDERS are repository variables, not
        # secrets; a reader who misses that will look in the wrong tab.
        self.assertIn("Variables", self.text)
        self.assertRegex(self.text, r"(?i)variables? tab|→ Variables|under the \*\*Variables\*\*")


class TestManualRun(ReadmeTestCase):
    def test_explains_workflow_dispatch(self):
        self.assertIn("Run workflow", self.text)
        self.assertIn("Actions", self.text)

    def test_states_what_a_successful_first_run_looks_like(self):
        self.assertIn("notifier is online", self.lower)

    def test_explains_that_the_first_run_posts_no_backlog(self):
        self.assertIn("backlog", self.lower)


class TestPublicRepoWarning(ReadmeTestCase):
    """The warning that costs money if it goes missing."""

    def test_has_a_dedicated_section(self):
        self.assert_heading_matching(r"public")

    def test_recommends_a_public_repository(self):
        self.assertRegex(self.text, r"(?i)make (this|the) repository public")

    def test_states_the_free_minute_allowance(self):
        self.assertIn("2,000", self.text)
        self.assertIn("minute", self.lower)

    def test_says_public_repos_are_unlimited(self):
        self.assertIn("unlimited", self.lower)

    def test_reassures_that_secrets_stay_encrypted(self):
        self.assertIn("encrypted", self.lower)

    def test_discloses_that_state_json_is_publicly_visible(self):
        self.assertIn("state.json", self.text)
        self.assertRegex(self.text, r"(?i)publicly visible|public")

    def test_describes_state_json_contents_accurately(self):
        # It holds hashes and integers, not readable transaction data.
        # Overstating this is as unhelpful as understating it.
        for key in ("last_activity_ms", "seen_fingerprints", "posted_weeks", "posted_reminders"):
            with self.subTest(key=key):
                self.assertIn(key, self.text)
        self.assertRegex(self.text, r"(?i)no player names|never written")


class TestAnnualMaintenance(ReadmeTestCase):
    """The note that turns a mystery outage into a two-minute fix."""

    def test_has_a_maintenance_section(self):
        self.assert_heading_matching(r"annual|maintenance")

    def test_says_cookies_expire_annually(self):
        self.assertRegex(self.text, r"(?i)expire (roughly )?annually|expire.{0,30}year")

    def test_says_a_password_change_invalidates_them(self):
        self.assertRegex(self.text, r"(?i)password")

    def test_says_league_year_needs_bumping(self):
        self.assertRegex(self.text, r"(?i)bump.{0,40}LEAGUE_YEAR|LEAGUE_YEAR.{0,40}bump")

    def test_names_august(self):
        self.assertIn("August", self.text)


class TestOperationalGuidance(ReadmeTestCase):
    def test_has_troubleshooting(self):
        self.assert_heading_matching(r"troubleshoot")

    def test_covers_expired_credentials(self):
        self.assertRegex(self.text, r"(?i)rejected the notifier's credentials|cookies have expired")

    def test_warns_about_scheduled_workflow_deactivation(self):
        # GitHub disables cron in repos idle for 60 days -- a silent stop
        # that looks exactly like a broken notifier.
        self.assertIn("60 days", self.text)

    def test_documents_the_dynasty_cron_widening(self):
        self.assertIn("Dynasty", self.text)
        self.assertRegex(self.text, r'cron:\s*"\*/20 \* \* \* \*"')

    def test_documents_local_testing(self):
        self.assertIn("--dry-run", self.text)
        self.assertIn("unittest", self.text)

    def test_states_the_two_dependency_rule(self):
        self.assertIn("espn_api", self.text)
        self.assertIn("requests", self.text)


class TestNoLeakedCredentials(ReadmeTestCase):
    """The README is the likeliest place to paste a real value by accident."""

    def test_no_real_discord_webhook_url(self):
        self.assertNotIn("discord.com/api/webhooks", self.text)

    def test_no_plausible_real_league_id(self):
        for match in re.findall(r"\b\d{6,}\b", self.text):
            with self.subTest(number=match):
                self.assertEqual(match, "1234567", "unexpected numeric literal")

    def test_no_swid_shaped_value(self):
        # A real SWID is a braced UUID; the example must stay masked.
        self.assertNotRegex(
            self.text, r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        )


if __name__ == "__main__":
    unittest.main()
