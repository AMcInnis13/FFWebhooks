"""Tests for the GitHub Actions workflow (T-012).

The workflow is read as text: there is no YAML parser in the standard
library, and adding PyYAML would break the two-dependency rule for the sake
of a config file. These assertions cover the settings that are expensive to
get wrong -- a bad cron burns runs, a missing concurrency group corrupts
state, and an echoed secret is permanent on a public repo.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "notifier.yml")

SECRET_NAMES = (
    "LEAGUE_ID",
    "LEAGUE_YEAR",
    "ESPN_S2",
    "SWID",
    "DISCORD_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL_RESULTS",
)


class WorkflowTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(WORKFLOW_PATH, encoding="utf-8") as handle:
            cls.text = handle.read()
        cls.lines = cls.text.splitlines()

    def code_lines(self):
        """Lines with comments stripped, so prose cannot satisfy a check."""
        out = []
        for line in self.lines:
            stripped = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            if stripped.strip():
                out.append(stripped)
        return out

    def code_text(self):
        return "\n".join(self.code_lines())


class TestFileBasics(WorkflowTestCase):
    def test_workflow_exists(self):
        self.assertTrue(os.path.exists(WORKFLOW_PATH))

    def test_contains_no_tabs(self):
        # YAML forbids tabs for indentation; one would break the whole file.
        for number, line in enumerate(self.lines, start=1):
            with self.subTest(line=number):
                self.assertNotIn("\t", line)

    def test_has_a_name_and_a_job(self):
        self.assertRegex(self.code_text(), r"(?m)^name:")
        self.assertRegex(self.code_text(), r"(?m)^jobs:")
        self.assertIn("runs-on: ubuntu-latest", self.code_text())


class TestTriggers(WorkflowTestCase):
    def test_runs_every_twenty_minutes(self):
        self.assertRegex(self.code_text(), r'cron:\s*"\*/20 ')

    def test_cron_is_restricted_to_september_through_january(self):
        match = re.search(r'cron:\s*"([^"]+)"', self.code_text())
        self.assertIsNotNone(match, "no cron expression found")
        fields = match.group(1).split()
        self.assertEqual(len(fields), 5, f"malformed cron: {match.group(1)}")

        month_field = fields[3]
        self.assertNotEqual(month_field, "*", "cron must not run year round by default")
        self.assertEqual(month_field, "9-12,1")

    def test_supports_manual_dispatch(self):
        self.assertIn("workflow_dispatch:", self.code_text())

    def test_explains_how_to_widen_for_dynasty_leagues(self):
        # This one is meant to be satisfied by a comment.
        self.assertIn("DYNASTY", self.text.upper())
        self.assertRegex(self.text, r'cron:\s*"\*/20 \* \* \* \*"')


class TestPermissionsAndConcurrency(WorkflowTestCase):
    def test_has_write_permission_for_the_state_commit(self):
        self.assertRegex(self.code_text(), r"permissions:\s*\n\s*contents:\s*write")

    def test_has_a_concurrency_group(self):
        self.assertRegex(self.code_text(), r"(?m)^concurrency:")
        self.assertRegex(self.code_text(), r"group:\s*\S+")

    def test_queues_rather_than_cancels(self):
        # A cancelled run may have posted to Discord without saving the
        # watermark, which reposts those messages next run.
        self.assertIn("cancel-in-progress: false", self.code_text())
        self.assertNotIn("cancel-in-progress: true", self.code_text())

    def test_job_has_a_timeout(self):
        self.assertRegex(self.code_text(), r"timeout-minutes:\s*\d+")


class TestSetup(WorkflowTestCase):
    def test_uses_checkout_v4(self):
        self.assertIn("actions/checkout@v4", self.code_text())

    def test_fetches_full_history_for_the_rebase(self):
        self.assertRegex(self.code_text(), r"fetch-depth:\s*0")

    def test_uses_setup_python_v5(self):
        self.assertIn("actions/setup-python@v5", self.code_text())

    def test_pins_python_312(self):
        self.assertRegex(self.code_text(), r'python-version:\s*"?3\.12"?')

    def test_enables_pip_caching(self):
        self.assertRegex(self.code_text(), r"cache:\s*pip")

    def test_installs_requirements(self):
        self.assertIn("requirements.txt", self.code_text())

    def test_runs_the_poller(self):
        self.assertRegex(self.code_text(), r"python poller\.py")

    def test_does_not_run_the_poller_in_dry_run_mode(self):
        self.assertNotIn("--dry-run", self.code_text())


class TestCommitStep(WorkflowTestCase):
    def test_skips_cleanly_when_state_is_unchanged(self):
        self.assertIn("git diff --quiet -- state.json", self.code_text())
        self.assertRegex(self.code_text(), r"exit 0")

    def test_rebases_before_pushing(self):
        self.assertIn("git pull --rebase", self.code_text())
        pull_index = self.code_text().index("git pull --rebase")
        push_index = self.code_text().index("git push")
        self.assertLess(pull_index, push_index, "must rebase before pushing")

    def test_commits_as_the_actions_bot(self):
        self.assertIn("github-actions[bot]", self.code_text())
        self.assertIn("users.noreply.github.com", self.code_text())

    def test_only_state_json_is_staged(self):
        self.assertIn("git add state.json", self.code_text())
        self.assertNotIn("git add -A", self.code_text())
        self.assertNotIn("git add .", self.code_text())

    def test_commits_even_when_the_notifier_failed(self):
        # A partial failure still wrote real progress; discarding it would
        # repost those messages on the next run.
        self.assertIn("if: always()", self.code_text())


class TestSecretHygiene(WorkflowTestCase):
    def test_secrets_are_referenced_only_through_the_secrets_context(self):
        for name in SECRET_NAMES:
            with self.subTest(secret=name):
                self.assertIn(f"secrets.{name}", self.code_text())

    def test_no_step_echoes_a_secret(self):
        for line in self.code_lines():
            lowered = line.lower()
            if "echo" not in lowered:
                continue
            with self.subTest(line=line.strip()):
                self.assertNotIn("secrets.", lowered)
                for name in SECRET_NAMES:
                    self.assertNotIn(f"${name.lower()}", lowered)
                    self.assertNotIn(f"${{{name.lower()}", lowered)

    def test_no_environment_dump(self):
        text = self.code_text()
        for forbidden in ("printenv", "env |", "env >", "set -x", "set -o xtrace"):
            with self.subTest(pattern=forbidden):
                self.assertNotIn(forbidden, text)

    def test_secrets_are_scoped_to_a_single_step(self):
        # A workflow-level or job-level `env:` block would expose them to
        # every step, including the one that runs git.
        env_blocks = re.findall(r"(?m)^(\s*)env:", self.code_text())
        for indent in env_blocks:
            with self.subTest(indent=len(indent)):
                self.assertGreater(
                    len(indent), 4, "env: block is not scoped to a single step"
                )

    def test_no_literal_credentials_are_present(self):
        text = self.text
        self.assertNotIn("discord.com/api/webhooks", text)
        self.assertNotIn("espn_s2=", text.lower())
        # No bare long digit run that could be a real league id. The only
        # legitimate one is the github-actions[bot] user id.
        for match in re.findall(r"\b\d{6,}\b", text):
            with self.subTest(number=match):
                self.assertEqual(match, "41898282", "unexpected numeric literal")

    def test_non_sensitive_settings_use_variables_not_secrets(self):
        self.assertIn("vars.TIMEZONE", self.code_text())
        self.assertIn("vars.LINEUP_REMINDERS", self.code_text())


try:
    import yaml
except ImportError:  # pragma: no cover - depends on the local environment
    yaml = None


@unittest.skipIf(yaml is None, "PyYAML not installed (dev-only; never a runtime dependency)")
class TestWorkflowParses(WorkflowTestCase):
    """Structural checks, so a malformed workflow is caught here.

    GitHub silently declines to run a workflow it cannot parse, which would
    show up as a mysterious no-op rather than an error. PyYAML is installed
    in the dev venv only and must never enter requirements.txt, so these
    tests skip rather than fail when it is absent.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if yaml is not None:
            cls.doc = yaml.safe_load(cls.text)

    def triggers(self):
        # YAML 1.1 reads a bare `on:` key as the boolean True. GitHub uses
        # its own parser and is unaffected, but PyYAML needs handling.
        return self.doc[True] if True in self.doc else self.doc["on"]

    def job(self):
        return self.doc["jobs"]["notify"]

    def test_document_is_a_mapping(self):
        self.assertIsInstance(self.doc, dict)

    def test_both_triggers_are_present(self):
        triggers = self.triggers()
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)

    def test_permissions_parse_to_contents_write(self):
        self.assertEqual(self.doc["permissions"], {"contents": "write"})

    def test_concurrency_parses_with_cancel_disabled(self):
        self.assertIs(self.doc["concurrency"]["cancel-in-progress"], False)
        self.assertTrue(self.doc["concurrency"]["group"])

    def test_expected_steps_are_present_in_order(self):
        names = [step.get("name") for step in self.job()["steps"]]
        self.assertEqual(
            names,
            [
                "Check out repository",
                "Set up Python",
                "Install dependencies",
                "Run notifier",
                "Commit state",
            ],
        )

    def test_exactly_one_step_receives_secrets(self):
        with_env = [s for s in self.job()["steps"] if "env" in s]
        self.assertEqual([s["name"] for s in with_env], ["Run notifier"])

    def test_every_required_variable_is_supplied(self):
        env = next(s for s in self.job()["steps"] if s.get("name") == "Run notifier")["env"]
        for name in SECRET_NAMES:
            with self.subTest(variable=name):
                self.assertIn(name, env)

    def test_no_step_before_the_notifier_touches_secrets(self):
        for step in self.job()["steps"]:
            if step.get("name") == "Run notifier":
                break
            with self.subTest(step=step.get("name")):
                self.assertNotIn("secrets.", str(step))


if __name__ == "__main__":
    unittest.main()
