"""Tests for the state layer (T-003).

state.json is committed to a public repo, so these tests also pin the
properties that keep it safe and diff-stable: deterministic serialization,
LF endings, and no leakage of anything but hashes and integers.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from poller import (
    MAX_FINGERPRINTS,
    default_state,
    fingerprint,
    load_state,
    needs_bootstrap,
    player_name,
    save_state,
    team_name,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeTeam:
    def __init__(self, name):
        self.team_name = name


class FakePlayer:
    def __init__(self, name):
        self.name = name


class FakeActivity:
    """Mirrors the shape espn_api builds: .date in epoch ms, .actions 4-tuples."""

    def __init__(self, date, actions):
        self.date = date
        self.actions = actions


class StateFileTestCase(unittest.TestCase):
    """Base providing a scratch state.json path that never touches the repo's."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "state.json")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)


class TestDefaultState(unittest.TestCase):
    def test_shape_matches_the_documented_schema(self):
        self.assertEqual(
            default_state(),
            {
                "last_activity_ms": 0,
                "seen_fingerprints": [],
                "posted_weeks": [],
                "posted_reminders": [],
            },
        )

    def test_each_call_returns_an_independent_object(self):
        first = default_state()
        first["posted_weeks"].append(1)
        self.assertEqual(default_state()["posted_weeks"], [])


class TestLoadDegradesGracefully(StateFileTestCase):
    def test_missing_file(self):
        self.assertEqual(load_state(self.path), default_state())

    def test_empty_file(self):
        self.write("")
        self.assertEqual(load_state(self.path), default_state())

    def test_whitespace_only_file(self):
        self.write("   \n\t\n")
        self.assertEqual(load_state(self.path), default_state())

    def test_empty_object_is_the_shipped_default(self):
        self.write("{}")
        self.assertEqual(load_state(self.path), default_state())

    def test_malformed_json(self):
        self.write('{"last_activity_ms": 5, ')  # truncated mid-write
        self.assertEqual(load_state(self.path), default_state())

    def test_json_that_is_not_an_object(self):
        for raw in ("[]", '"a string"', "42", "null"):
            with self.subTest(raw=raw):
                self.write(raw)
                self.assertEqual(load_state(self.path), default_state())

    def test_directory_in_place_of_file_does_not_crash(self):
        os.mkdir(os.path.join(self.tmpdir.name, "as_dir"))
        self.assertEqual(load_state(os.path.join(self.tmpdir.name, "as_dir")), default_state())


class TestLoadCoercesTypes(StateFileTestCase):
    def test_valid_state_round_trips(self):
        original = {
            "last_activity_ms": 1756200000000,
            "seen_fingerprints": ["aaaa", "bbbb"],
            "posted_weeks": [1, 2, 3],
            "posted_reminders": ["2026-11-09-sunday"],
        }
        self.write(json.dumps(original))
        self.assertEqual(load_state(self.path), original)

    def test_numeric_string_watermark_is_coerced(self):
        self.write('{"last_activity_ms": "1756200000000"}')
        self.assertEqual(load_state(self.path)["last_activity_ms"], 1756200000000)

    def test_garbage_watermark_falls_back_to_zero(self):
        for raw in ('"abc"', "null", "[]", "{}"):
            with self.subTest(raw=raw):
                self.write('{"last_activity_ms": %s}' % raw)
                self.assertEqual(load_state(self.path)["last_activity_ms"], 0)

    def test_non_list_collections_become_empty_lists(self):
        self.write('{"seen_fingerprints": "nope", "posted_weeks": 5, "posted_reminders": null}')
        state = load_state(self.path)
        self.assertEqual(state["seen_fingerprints"], [])
        self.assertEqual(state["posted_weeks"], [])
        self.assertEqual(state["posted_reminders"], [])

    def test_posted_weeks_coerces_numeric_strings_and_drops_garbage(self):
        self.write('{"posted_weeks": [1, "2", "week three", null, 4]}')
        self.assertEqual(load_state(self.path)["posted_weeks"], [1, 2, 4])

    def test_unknown_keys_are_preserved(self):
        self.write('{"last_activity_ms": 7, "future_feature": {"a": 1}}')
        state = load_state(self.path)
        self.assertEqual(state["future_feature"], {"a": 1})
        self.assertEqual(state["last_activity_ms"], 7)

    def test_unknown_keys_survive_a_save_round_trip(self):
        self.write('{"future_feature": ["keep", "me"]}')
        save_state(load_state(self.path), self.path)
        self.assertEqual(load_state(self.path)["future_feature"], ["keep", "me"])


class TestSave(StateFileTestCase):
    def test_round_trip(self):
        state = default_state()
        state["last_activity_ms"] = 1756200000000
        state["posted_weeks"] = [1, 2]
        save_state(state, self.path)
        self.assertEqual(load_state(self.path), state)

    def test_fingerprints_trimmed_to_the_newest_max(self):
        state = default_state()
        state["seen_fingerprints"] = [f"fp{i:04d}" for i in range(MAX_FINGERPRINTS + 50)]
        saved = save_state(state, self.path)

        self.assertEqual(len(saved["seen_fingerprints"]), MAX_FINGERPRINTS)
        reloaded = load_state(self.path)["seen_fingerprints"]
        self.assertEqual(len(reloaded), MAX_FINGERPRINTS)
        # Newest entries are appended, so the tail must survive and the head
        # must be the one that gets dropped.
        self.assertEqual(reloaded[-1], f"fp{MAX_FINGERPRINTS + 49:04d}")
        self.assertEqual(reloaded[0], "fp0050")
        self.assertNotIn("fp0000", reloaded)

    def test_under_the_limit_is_untouched(self):
        state = default_state()
        state["seen_fingerprints"] = ["a", "b", "c"]
        self.assertEqual(save_state(state, self.path)["seen_fingerprints"], ["a", "b", "c"])

    def test_caller_state_is_not_mutated(self):
        state = default_state()
        state["seen_fingerprints"] = [f"fp{i}" for i in range(MAX_FINGERPRINTS + 10)]
        save_state(state, self.path)
        self.assertEqual(len(state["seen_fingerprints"]), MAX_FINGERPRINTS + 10)

    def test_serialization_is_byte_identical_across_saves(self):
        # The workflow skips its commit when state.json is unchanged; that is
        # only meaningful if the same state always produces the same bytes.
        state = load_state(self.path)
        state.update({"last_activity_ms": 99, "posted_weeks": [3, 1, 2]})
        save_state(state, self.path)
        with open(self.path, "rb") as handle:
            first = handle.read()
        save_state(state, self.path)
        with open(self.path, "rb") as handle:
            second = handle.read()
        self.assertEqual(first, second)

    def test_written_with_lf_endings_and_trailing_newline(self):
        # Must match the Linux runner's bytes or every local run would show a
        # spurious CRLF diff.
        save_state(default_state(), self.path)
        with open(self.path, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))

    def test_keys_are_sorted(self):
        save_state(default_state(), self.path)
        with open(self.path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertLess(body.index("last_activity_ms"), body.index("posted_reminders"))
        self.assertLess(body.index("posted_reminders"), body.index("posted_weeks"))
        self.assertLess(body.index("posted_weeks"), body.index("seen_fingerprints"))

    def test_no_temp_files_left_behind(self):
        save_state(default_state(), self.path)
        leftovers = [n for n in os.listdir(self.tmpdir.name) if n != "state.json"]
        self.assertEqual(leftovers, [])


class TestNeedsBootstrap(StateFileTestCase):
    def test_missing_file(self):
        self.assertTrue(needs_bootstrap(self.path))

    def test_empty_file(self):
        self.write("")
        self.assertTrue(needs_bootstrap(self.path))

    def test_shipped_empty_object(self):
        self.write("{}")
        self.assertTrue(needs_bootstrap(self.path))

    def test_malformed_json(self):
        self.write("{not json")
        self.assertTrue(needs_bootstrap(self.path))

    def test_populated_state_does_not_need_bootstrap(self):
        self.write(json.dumps({"last_activity_ms": 123}))
        self.assertFalse(needs_bootstrap(self.path))

    def test_preseason_state_does_not_re_bootstrap(self):
        # All-zero state is legitimate before the season starts. Comparing the
        # loaded dict to the default would re-send "notifier is online" every
        # 20 minutes until week 1.
        save_state(default_state(), self.path)
        self.assertEqual(load_state(self.path), default_state())
        self.assertFalse(needs_bootstrap(self.path))


class TestNameHelpers(unittest.TestCase):
    def test_team_name_from_object(self):
        self.assertEqual(team_name(FakeTeam("Waiver Wolves")), "Waiver Wolves")

    def test_team_name_tolerates_empty_string_team(self):
        self.assertEqual(team_name(""), "")

    def test_team_name_tolerates_blank_team_name(self):
        self.assertEqual(team_name(FakeTeam("")), "")
        self.assertEqual(team_name(FakeTeam(None)), "")

    def test_player_name_from_object(self):
        self.assertEqual(player_name(FakePlayer("Marvin Waivers Jr.")), "Marvin Waivers Jr.")

    def test_player_name_tolerates_raw_int_id(self):
        self.assertEqual(player_name(4241457), "4241457")

    def test_player_name_tolerates_unknown_sentinel(self):
        self.assertEqual(player_name("Unknown"), "Unknown")


class TestFingerprint(unittest.TestCase):
    def activity(self, date=1756200000000, actions=None):
        if actions is None:
            actions = [(FakeTeam("Team A"), "FA ADDED", FakePlayer("Player X"), 0)]
        return FakeActivity(date, actions)

    def test_is_deterministic(self):
        self.assertEqual(fingerprint(self.activity()), fingerprint(self.activity()))

    def test_action_order_does_not_change_the_hash(self):
        a = FakePlayer("Player X")
        b = FakePlayer("Player Y")
        forward = self.activity(
            actions=[
                (FakeTeam("Team A"), "TRADE_SENT", a, 0),
                (FakeTeam("Team B"), "TRADE_RECEIVED", b, 0),
            ]
        )
        reversed_ = self.activity(
            actions=[
                (FakeTeam("Team B"), "TRADE_RECEIVED", b, 0),
                (FakeTeam("Team A"), "TRADE_SENT", a, 0),
            ]
        )
        self.assertEqual(fingerprint(forward), fingerprint(reversed_))

    def test_different_date_changes_the_hash(self):
        self.assertNotEqual(
            fingerprint(self.activity(date=1756200000000)),
            fingerprint(self.activity(date=1756200000001)),
        )

    def test_different_player_changes_the_hash(self):
        other = self.activity(actions=[(FakeTeam("Team A"), "FA ADDED", FakePlayer("Player Z"), 0)])
        self.assertNotEqual(fingerprint(self.activity()), fingerprint(other))

    def test_different_bid_changes_the_hash(self):
        base = self.activity(
            actions=[(FakeTeam("Team A"), "WAIVER ADDED", FakePlayer("Player X"), 42)]
        )
        other = self.activity(
            actions=[(FakeTeam("Team A"), "WAIVER ADDED", FakePlayer("Player X"), 7)]
        )
        self.assertNotEqual(fingerprint(base), fingerprint(other))

    def test_two_activities_sharing_a_millisecond_differ(self):
        # The exact case the fingerprint exists to cover: the watermark alone
        # cannot separate these.
        first = self.activity(actions=[(FakeTeam("A"), "FA ADDED", FakePlayer("X"), 0)])
        second = self.activity(actions=[(FakeTeam("B"), "FA ADDED", FakePlayer("Y"), 0)])
        self.assertNotEqual(fingerprint(first), fingerprint(second))

    def test_handles_raw_id_player_and_empty_team(self):
        activity = self.activity(actions=[("", "DROPPED", 4241457, 0)])
        self.assertIsInstance(fingerprint(activity), str)
        self.assertEqual(len(fingerprint(activity)), 16)

    def test_handles_short_and_empty_action_rows(self):
        self.assertIsInstance(fingerprint(FakeActivity(1, [])), str)
        self.assertIsInstance(fingerprint(FakeActivity(1, [(FakeTeam("A"), "DROPPED")])), str)

    def test_is_stable_across_processes(self):
        # Python's built-in hash() is salted per process by PYTHONHASHSEED, so
        # using it would silently produce a new value on every Actions run and
        # break dedup entirely. Prove we don't.
        snippet = (
            "import poller;"
            "t=type('T',(),{'team_name':'Team A'})();"
            "p=type('P',(),{'name':'Player X'})();"
            "a=type('A',(),{'date':1756200000000,'actions':[(t,'FA ADDED',p,0)]})();"
            "print(poller.fingerprint(a))"
        )
        results = []
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, "-c", snippet],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            results.append(proc.stdout.strip())
        self.assertEqual(len(set(results)), 1, f"fingerprint varied by hash seed: {results}")
        self.assertEqual(len(results[0]), 16)


class TestStateStaysPubliclySafe(unittest.TestCase):
    """state.json is world-readable. It must hold only hashes and integers."""

    def test_fingerprint_does_not_embed_readable_names(self):
        activity = FakeActivity(
            1756200000000,
            [(FakeTeam("Waiver Wolves"), "WAIVER ADDED", FakePlayer("Marvin Waivers Jr."), 42)],
        )
        digest = fingerprint(activity)
        self.assertNotIn("Waiver", digest)
        self.assertNotIn("Marvin", digest)
        self.assertTrue(all(c in "0123456789abcdef" for c in digest))


if __name__ == "__main__":
    unittest.main()
