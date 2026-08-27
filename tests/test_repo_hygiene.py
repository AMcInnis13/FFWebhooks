"""Repository hygiene checks (T-014).

This repo is public and every tracked file is world-readable forever. These
tests scan what git actually tracks, so a credential pasted into any file --
code, test, fixture, or doc -- fails the suite before it can be pushed.

Written as tests rather than a one-off command so the guarantee is
repeatable and the reviewed false positives stay documented.
"""

import json
import os
import re
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Numbers of six digits or more that are known-safe and reviewed. Anything
# else long and numeric is treated as a possible real league id.
ALLOWED_NUMBERS = {
    "1234567",      # the documented fake league id, used throughout
    "4241457",      # fake ESPN player id, for the raw-id fallback fixture
    "41898282",     # github-actions[bot] user id, required by the commit step
}

# Fixture timestamps: epoch milliseconds, which are inherently long.
TIMESTAMP_RE = re.compile(r"^17[0-9]{11}$")

BINARY_SUFFIXES = (".png", ".jpg", ".gif", ".ico", ".pyc", ".zip")


def tracked_files():
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def read_tracked():
    for path in tracked_files():
        if path.endswith(BINARY_SUFFIXES):
            continue
        full = os.path.join(REPO_ROOT, path)
        if not os.path.exists(full):
            continue
        with open(full, encoding="utf-8", errors="replace") as handle:
            yield path, handle.read()


def have_git():
    """True only when REPO_ROOT is itself the root of a git checkout.

    Checking merely that git works would also pass when the project has been
    unzipped into a folder that happens to live inside some *other* git repo:
    `git ls-files` would then return an empty list and every check below
    would fail for a reason that has nothing to do with this project.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover
        return False

    toplevel = os.path.normcase(os.path.realpath(result.stdout.strip()))
    return toplevel == os.path.normcase(os.path.realpath(REPO_ROOT))


@unittest.skipUnless(have_git(), "not a git checkout")
class TestNoCredentialsAreTracked(unittest.TestCase):
    def test_no_discord_webhook_url(self):
        # A webhook URL is a credential: anyone holding it can post.
        pattern = re.compile(r"discord(app)?\.com/api/webhooks/\d")
        for path, text in read_tracked():
            with self.subTest(path=path):
                self.assertIsNone(pattern.search(text), f"webhook URL in {path}")

    def test_no_swid_shaped_uuid(self):
        # A real SWID is a braced hex UUID. The docs use X placeholders.
        pattern = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-")
        for path, text in read_tracked():
            with self.subTest(path=path):
                self.assertIsNone(pattern.search(text), f"SWID-shaped value in {path}")

    def test_no_long_opaque_cookie_value(self):
        # espn_s2 is a long URL-encoded blob. Flag any assignment of one.
        pattern = re.compile(r"(?i)espn_s2\s*[=:]\s*[\"']?[A-Za-z0-9%+/_-]{40,}")
        for path, text in read_tracked():
            with self.subTest(path=path):
                self.assertIsNone(pattern.search(text), f"cookie-like value in {path}")

    def test_no_unreviewed_long_numbers(self):
        # Catches a real league id pasted anywhere in the repo.
        for path, text in read_tracked():
            for number in re.findall(r"\b\d{6,}\b", text):
                if number in ALLOWED_NUMBERS or TIMESTAMP_RE.match(number):
                    continue
                with self.subTest(path=path, number=number):
                    self.fail(
                        f"unreviewed long number {number} in {path}. "
                        "If it is safe, add it to ALLOWED_NUMBERS with a reason."
                    )

    def test_no_environment_or_credential_files_tracked(self):
        forbidden = (".env", "cookies.txt", "cookies.json", "secrets.json")
        tracked = {os.path.basename(p) for p in tracked_files()}
        for name in forbidden:
            with self.subTest(name=name):
                self.assertNotIn(name, tracked)

    def test_no_caches_or_virtualenvs_tracked(self):
        for path in tracked_files():
            with self.subTest(path=path):
                self.assertNotIn("__pycache__", path)
                self.assertFalse(path.endswith(".pyc"))
                self.assertFalse(path.startswith(".venv/"))


@unittest.skipUnless(have_git(), "not a git checkout")
class TestGitignoreCoversTheRisks(unittest.TestCase):
    def assert_ignored(self, candidate):
        result = subprocess.run(
            ["git", "check-ignore", "-q", candidate],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, f"{candidate} is not gitignored")

    def test_credential_shaped_paths_are_ignored(self):
        for candidate in (
            ".env",
            ".env.local",
            "cookies.txt",
            "espn_cookies.json",
            "webhook.txt",
            "secrets/creds.yaml",
            "private.key",
            "id_rsa",
        ):
            with self.subTest(candidate=candidate):
                self.assert_ignored(candidate)

    def test_local_working_files_are_ignored(self):
        for candidate in ("CLAUDE.md", "TASKS.md", "status.md", ".claude/bootstrap.md"):
            with self.subTest(candidate=candidate):
                self.assert_ignored(candidate)

    def test_state_json_is_deliberately_not_ignored(self):
        # It is the only persistence and the workflow commits it back.
        result = subprocess.run(
            ["git", "check-ignore", "-q", "state.json"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0, "state.json must stay tracked")


@unittest.skipUnless(have_git(), "not a git checkout")
class TestShippedState(unittest.TestCase):
    def test_state_json_is_tracked(self):
        self.assertIn("state.json", tracked_files())

    def state(self):
        with open(os.path.join(REPO_ROOT, "state.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_state_json_holds_only_known_keys(self):
        # Originally this asserted the file was literally "{}". That is true
        # of a repo that has never run and false the moment the workflow
        # commits real state back, so it failed for a healthy deployment.
        # What actually matters is that the contents stay safe to publish.
        allowed = {
            "last_activity_ms",
            "seen_fingerprints",
            "posted_weeks",
            "posted_reminders",
            "error_notices",
        }
        unexpected = set(self.state()) - allowed
        self.assertEqual(unexpected, set(), f"unexpected keys in state.json: {unexpected}")

    def test_state_json_discloses_nothing_readable(self):
        data = self.state()
        self.assertIsInstance(data.get("last_activity_ms", 0), int)

        for mark in data.get("seen_fingerprints", []):
            with self.subTest(fingerprint=mark):
                self.assertRegex(mark, r"^[0-9a-f]+$", "fingerprints must be opaque hex")

        for week in data.get("posted_weeks", []):
            with self.subTest(week=week):
                self.assertIsInstance(week, int)

        for key in data.get("posted_reminders", []):
            with self.subTest(reminder=key):
                self.assertRegex(key, r"^\d{4}-\d{2}-\d{2}-(thursday|sunday)$")

        for kind, when in (data.get("error_notices") or {}).items():
            with self.subTest(kind=kind):
                self.assertIsInstance(when, int)

    def test_a_fresh_fork_should_ship_empty(self):
        # Not enforced, because a live repo legitimately carries real state.
        # Recorded so anyone publishing this as a template knows to reset it:
        # a populated state.json suppresses the new user's bootstrap run and
        # a zeroed watermark would replay recent_activity in one burst.
        self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, "state.json")))


class TestDependencyDiscipline(unittest.TestCase):
    def requirements(self):
        with open(os.path.join(REPO_ROOT, "requirements.txt"), encoding="utf-8") as handle:
            return [
                line.strip()
                for line in handle
                if line.strip() and not line.strip().startswith("#")
            ]

    def test_exactly_two_runtime_dependencies(self):
        self.assertEqual(len(self.requirements()), 2, self.requirements())

    def test_they_are_the_documented_two(self):
        joined = " ".join(self.requirements()).lower()
        self.assertIn("espn_api", joined)
        self.assertIn("requests", joined)

    def test_dev_only_packages_never_enter_requirements(self):
        joined = " ".join(self.requirements()).lower()
        for package in ("tzdata", "pyyaml", "pytest"):
            with self.subTest(package=package):
                self.assertNotIn(package, joined)


@unittest.skipUnless(have_git(), "not a git checkout")
class TestExpectedLayout(unittest.TestCase):
    def test_the_published_repo_contains_what_it_should(self):
        expected = {
            ".github/workflows/notifier.yml",
            ".gitignore",
            "README.md",
            "poller.py",
            "requirements.txt",
            "state.json",
        }
        tracked = set(tracked_files())
        missing = expected - tracked
        self.assertEqual(missing, set(), f"missing from the repo: {sorted(missing)}")

    def test_all_application_code_lives_in_poller(self):
        modules = [
            p
            for p in tracked_files()
            if p.endswith(".py") and not p.startswith("tests/")
        ]
        self.assertEqual(modules, ["poller.py"])

    def test_no_planning_files_are_tracked(self):
        for path in tracked_files():
            with self.subTest(path=path):
                self.assertNotIn(path, {"CLAUDE.md", "TASKS.md", "status.md"})


if __name__ == "__main__":
    unittest.main()
