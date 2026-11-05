"""Offline regression tests: paging, deduplication, aggregation and output stability."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.sync import (
    build_views, fetch_catalog, get_new_history, load_history,
    save_data, validate_submission, write_json_if_changed,
)


def sub(number, second, problem="abc100_a", result="AC", user="usausamikann"):
    return {
        "id": number, "epoch_second": second, "problem_id": problem,
        "contest_id": "abc100", "user_id": user,
        "result": result, "point": 100, "language": "C++ 20",
        "length": 123, "execution_time": 1,
    }


class PaginationTests(unittest.TestCase):
    def test_more_than_one_page_and_same_second_boundary(self):
        rows = [sub(i, 1000 + i) for i in range(1, 503)]
        # Two different submissions at the boundary share a second.
        rows[500]["epoch_second"] = rows[499]["epoch_second"]

        calls = []
        def page(cursor):
            calls.append(cursor)
            return [x for x in rows if x["epoch_second"] > cursor][:500]

        obtained = get_new_history([], page)
        self.assertEqual([x["id"] for x in obtained], list(range(1, 503)))
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1], rows[499]["epoch_second"])

    def test_incremental_overlap_is_idempotent(self):
        old = [sub(1, 100), sub(2, 101)]
        new = sub(3, 101, "abc100_b", "WA")
        def page(cursor):
            self.assertEqual(cursor, 100)
            return [x for x in old + [new] if x["epoch_second"] > cursor]
        result = get_new_history(old, page)
        self.assertEqual([x["id"] for x in result], [1, 2, 3])
        self.assertEqual(result, get_new_history(result, lambda _: []))

    def test_stalled_pagination_fails_loudly(self):
        rows = [sub(i, 1) for i in range(1, 501)]
        with self.assertRaisesRegex(RuntimeError, "Cannot paginate"):
            get_new_history([], lambda _: rows)

    def test_api_is_validated(self):
        with self.assertRaises(ValueError):
            get_new_history([], lambda _: {"message": "error"})
        with self.assertRaises(ValueError):
            validate_submission(sub(3, 100, user="someone_else"), "usausamikann")


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            "abc100_a": {"contest_id": "abc100", "name": "First", "index": "A",
                         "difficulty": 100, "is_experimental": False},
            "abc100_b": {"contest_id": "abc100", "name": "Second", "index": "B",
                         "difficulty": 200, "is_experimental": False},
        }

    def test_solved_problem_with_later_wa_stays_solved(self):
        history = [
            sub(1, 100, "abc100_a", "WA"),
            sub(2, 101, "abc100_a", "AC"),
            sub(3, 102, "abc100_a", "WA"),
            sub(4, 103, "abc100_b", "TLE"),
        ]
        data = build_views(history, self.catalog)
        summary = data["summary"]
        self.assertEqual(summary["submission_count"], 4)
        self.assertEqual(summary["attempted_problem_count"], 2)
        self.assertEqual(summary["solved_problem_count"], 1)
        self.assertEqual(summary["unresolved_problem_count"], 1)
        self.assertEqual(summary["abc_index"]["A"], {"attempted": 1, "solved": 1})
        self.assertEqual(summary["abc_index"]["B"], {"attempted": 1, "solved": 0})
        first = data["problems"][0]
        self.assertTrue(first["solved"])
        self.assertEqual(first["first_ac_epoch"], 101)
        self.assertEqual(first["last_result"], "WA")
        self.assertTrue(data["recent"][0]["submission_url"].endswith("/4"))

    def test_sharding_and_second_run_no_change(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            history = [sub(1, 1615376323), sub(2, 1738240287, "abc100_b")]
            changed = save_data(directory, history, self.catalog, "usausamikann")
            self.assertTrue(changed)
            self.assertEqual(load_history(directory), history)
            self.assertEqual(save_data(directory, history, self.catalog, "usausamikann"), [])
            self.assertTrue((directory / "submissions" / "2021.json").exists())
            self.assertTrue((directory / "submissions" / "2025.json").exists())
            self.assertEqual(json.loads((directory / "summary.json").read_text())["submission_count"], 2)

    def test_json_writes_are_deterministic(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            self.assertTrue(write_json_if_changed(path, {"z": 1, "a": 2}))
            self.assertFalse(write_json_if_changed(path, {"a": 2, "z": 1}))

    def test_canonical_abc_contest_wins_over_adt_reprint(self):
        class Client:
            def __init__(self):
                self.responses = [
                    [{"id": "abc460_a", "contest_id": "abc460", "name": "Mod While Positive"},
                     {"id": "abc460_a", "contest_id": "adt_easy_20260915_2",
                      "name": "Mod While Positive"}],
                    [{"problem_id": "abc460_a", "contest_id": "abc460",
                      "problem_index": "A"},
                     {"problem_id": "abc460_a", "contest_id": "adt_easy_20260915_2",
                      "problem_index": "B"}],
                    {"abc460_a": {"difficulty": -914, "is_experimental": False}},
                ]
            def get(self, _):
                return self.responses.pop(0)
        submissions = [sub(1, 100, "abc460_a")]
        submissions[0]["contest_id"] = "abc460"
        metadata = fetch_catalog({"abc460_a"}, Client(), submissions)
        self.assertEqual(metadata["abc460_a"]["contest_id"], "abc460")
        self.assertEqual(metadata["abc460_a"]["index"], "A")
        result = build_views(submissions, metadata)
        self.assertEqual(result["summary"]["abc_index"]["A"]["solved"], 1)
        self.assertIn("/contests/abc460/tasks/abc460_a",
                      result["problems"][0]["url"])

    def test_submitted_abc_contest_beats_arc_prefix(self):
        from scripts.sync import preferred_contest
        result = preferred_contest("arc058_a", {"arc058", "abc042"},
                                   ["abc042"])
        self.assertEqual(result, "abc042")

    def test_catalog_restricts_to_attempted_problems(self):
        class Client:
            def __init__(self):
                self.responses = [
                    [{"id": "abc100_a", "contest_id": "abc100", "name": "First"},
                     {"id": "abc100_b", "contest_id": "abc100", "name": "Second"}],
                    [{"problem_id": "abc100_a", "contest_id": "abc100",
                      "problem_index": "A"}],
                    {"abc100_a": {"difficulty": 100, "is_experimental": False}},
                ]
            def get(self, _):
                return self.responses.pop(0)
        result = fetch_catalog({"abc100_a"}, Client())
        self.assertEqual(list(result), ["abc100_a"])
        self.assertEqual(result["abc100_a"]["index"], "A")
        self.assertEqual(result["abc100_a"]["difficulty"], 100)


if __name__ == "__main__":
    unittest.main()
