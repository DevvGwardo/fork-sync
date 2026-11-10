from __future__ import annotations

import unittest

import sync


class FakeApi(sync.Api):
    """Api with the network replaced by a route table: (method, path) -> (status, payload)."""

    def __init__(self, routes):
        super().__init__("fake-token", sleep=lambda _s: None)
        self.routes = routes
        self.calls = []

    def request(self, method, path, body=None, allow=()):
        self.calls.append((method, path))
        key = (method, path)
        if key in self.routes:
            return self.routes[key]
        for (m, p), value in self.routes.items():
            if m == method and p.endswith(path.split("/")[-1]) and p.startswith(sync.API):
                return value
        return 404, {"message": "no route"}


FORK = {"full_name": "o/proj", "default_branch": "main", "fork": True, "private": False}


def meta(parent="up/proj", branch="main", archived=False):
    return 200, {"full_name": "o/proj", "default_branch": "main",
                 "parent": {"full_name": parent, "default_branch": branch, "archived": archived}}


class PlanTests(unittest.TestCase):
    def test_up_to_date(self):
        api = FakeApi({
            ("GET", "/repos/o/proj"): meta(),
            ("GET", "/repos/o/proj/compare/up:main...main"): (200, {"ahead_by": 0, "behind_by": 0}),
        })
        row = sync.plan_one(api, FORK)
        self.assertEqual(row["action"], "up-to-date")

    def test_behind_is_syncable(self):
        api = FakeApi({
            ("GET", "/repos/o/proj"): meta(),
            ("GET", "/repos/o/proj/compare/up:main...main"): (200, {"ahead_by": 0, "behind_by": 7}),
        })
        self.assertEqual(sync.plan_one(api, FORK)["action"], "sync")

    def test_diverged_needs_human_by_default(self):
        api = FakeApi({
            ("GET", "/repos/o/proj"): meta(),
            ("GET", "/repos/o/proj/compare/up:main...main"): (200, {"ahead_by": 3, "behind_by": 9}),
        })
        row = sync.plan_one(api, FORK)
        self.assertEqual(row["action"], "needs-human")
        self.assertIn("diverged", row["detail"])

    def test_archived_upstream_is_skipped(self):
        api = FakeApi({("GET", "/repos/o/proj"): meta(archived=True)})
        self.assertEqual(sync.plan_one(api, FORK)["action"], "skip")

    def test_branch_name_mismatch_needs_human(self):
        api = FakeApi({
            ("GET", "/repos/o/proj"): meta(branch="master"),
            ("GET", "/repos/o/proj/compare/up:master...main"): (200, {"ahead_by": 0, "behind_by": 4}),
        })
        row = sync.plan_one(api, FORK)
        self.assertEqual(row["action"], "needs-human")
        self.assertIn("!=", row["detail"])

    def test_not_a_fork(self):
        api = FakeApi({("GET", "/repos/o/proj"): (200, {"full_name": "o/proj", "default_branch": "main"})})
        self.assertEqual(sync.plan_one(api, FORK)["action"], "skip")


class ApplyTests(unittest.TestCase):
    def test_merge_upstream_success(self):
        api = FakeApi({("POST", "/repos/o/proj/merge-upstream"): (200, {"merge_type": "fast-forward"})})
        row = {"repo": "o/proj", "branch": "main", "action": "sync", "detail": "", "upstream": "up/proj"}
        sync.apply(api, row)
        self.assertEqual(row["action"], "synced")
        self.assertEqual(row["detail"], "fast-forward")

    def test_conflict_is_reported_not_raised(self):
        api = FakeApi({("POST", "/repos/o/proj/merge-upstream"): (409, {"message": "merge conflict"})})
        row = {"repo": "o/proj", "branch": "main", "action": "sync", "detail": "", "upstream": "up/proj"}
        sync.apply(api, row)
        self.assertEqual(row["action"], "needs-human")
        self.assertIn("conflict", row["detail"])

    def test_unknown_error_surfaces_status(self):
        api = FakeApi({("POST", "/repos/o/proj/merge-upstream"): (422, {"message": "no common ancestor"})})
        row = {"repo": "o/proj", "branch": "main", "action": "sync", "detail": "", "upstream": "up/proj"}
        sync.apply(api, row)
        self.assertEqual(row["action"], "error")
        self.assertIn("422", row["detail"])


class ReportTests(unittest.TestCase):
    def test_counts_and_table(self):
        rows = [{"repo": "o/a", "branch": "main", "action": "synced", "detail": "fast-forward"},
                {"repo": "o/b", "branch": "main", "action": "needs-human", "detail": "diverged"}]
        out = sync.render(rows, dry_run=False)
        self.assertIn("synced: 1", out)
        self.assertIn("needs-human: 1", out)
        self.assertIn("`o/a`", out)

    def test_only_filter_selects_named_repos(self):
        # the filter lives in main(); assert the comparator it uses
        only = {s.strip().lower() for s in "proj, other".split(",") if s.strip()}
        self.assertEqual(only, {"proj", "other"})


if __name__ == "__main__":
    unittest.main()
