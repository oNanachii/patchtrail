import concurrent.futures
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest

from patchtrail.storage import RunNotFound, Store, StoreConflict


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-store-")
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "nested" / "runs.sqlite3"
        self.store = Store(self.database)

    def create(self, key="request-1", **kwargs):
        return self.store.create_run("quantity", "demo", key, kwargs or {"max_calls": 8})

    def advance(self, run_id, target):
        states = ["diagnosing", "awaiting_approval", "verifying", "succeeded"]
        for state in states[:states.index(target) + 1]:
            current = self.store.get_run(run_id)
            self.store.transition(run_id, {current["state"]}, state, expected_version=current["version"])
        return self.store.get_run(run_id)

    def test_create_defaults_and_idempotency_bind_entire_request(self):
        created = self.create()
        self.assertTrue(created["created"])
        self.assertEqual((created["state"], created["version"]), ("queued", 0))
        self.assertEqual(created["checkpoint"], {})
        self.assertIsNone(created["proposal"])
        self.assertIsNone(created["result"])
        again = self.create()
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], created["id"])
        for task, mode, limits in [("other", "demo", {"max_calls": 8}),
                                   ("quantity", "live", {"max_calls": 8}),
                                   ("quantity", "demo", {"max_calls": 9})]:
            with self.assertRaises(StoreConflict):
                self.store.create_run(task, mode, "request-1", limits)
        created["limits"]["max_calls"] = 999
        self.assertEqual(self.store.get_run(created["id"])["limits"], {"max_calls": 8})

    def test_identical_keys_racing_create_one_run(self):
        barrier = threading.Barrier(6)

        def create():
            barrier.wait()
            return self.create("same-key")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda _: create(), range(6)))
        self.assertEqual(len({row["id"] for row in rows}), 1)
        self.assertEqual(sum(row["created"] for row in rows), 1)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_find_request_returns_decoded_existing_run_without_internal_key(self):
        self.assertIsNone(self.store.find_request("not-created"))
        created = self.create("retry-key")
        found = self.store.find_request("retry-key")
        self.assertEqual(found, self.store.get_run(created["id"]))
        self.assertEqual(found["limits"], {"max_calls": 8})
        self.assertNotIn("request_key", found)
        self.assertNotIn("created", found)
        found["checkpoint"]["local"] = True
        self.assertEqual(self.store.find_request("retry-key")["checkpoint"], {})

    def test_version_race_allows_only_one_state_change(self):
        run = self.create()
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            try:
                self.store.transition(run["id"], {"queued"}, "diagnosing", expected_version=0)
                return "accepted"
            except StoreConflict:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: claim(), range(2)))
        self.assertCountEqual(outcomes, ["accepted", "conflict"])
        self.assertEqual(self.store.get_run(run["id"])["version"], 1)
        self.assertEqual(len(self.store.events(run["id"])), 1)

    def test_state_machine_cannot_skip_approval_or_reopen_terminal(self):
        run = self.create()
        with self.assertRaises(StoreConflict):
            self.store.transition(run["id"], {"queued"}, "succeeded")
        finished = self.advance(run["id"], "succeeded")
        for target in ("queued", "diagnosing", "succeeded"):
            with self.assertRaises(StoreConflict):
                self.store.transition(run["id"], {"succeeded"}, target,
                                      {"checkpoint": {}}, expected_version=finished["version"])
        self.assertEqual(self.store.get_run(run["id"])["state"], "succeeded")

    def test_checkpoint_updates_require_active_state_and_matching_version(self):
        run = self.create()
        updated = self.store.transition(run["id"], {"queued"}, "queued", {"checkpoint": {"phase": "ready"}}, 0)
        self.assertEqual(updated["version"], 1)
        self.assertEqual(updated["checkpoint"], {"phase": "ready"})
        with self.assertRaises(StoreConflict):
            self.store.transition(run["id"], {"queued"}, "queued", {"checkpoint": {}}, 0)
        with self.assertRaises(StoreConflict):
            self.store.transition(run["id"], {"queued"}, "queued", {"result": {}})
        with self.assertRaises(ValueError):
            self.store.transition(run["id"], {"queued"}, "diagnosing", {"limits": {}})

    def test_transition_and_state_event_roll_back_together(self):
        run = self.create()
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events "
                               "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transition(run["id"], {"queued"}, "diagnosing")
        after = self.store.get_run(run["id"])
        self.assertEqual((after["state"], after["version"]), ("queued", 0))
        self.assertEqual(self.store.events(run["id"]), [])

    def test_event_idempotency_is_per_run_kind_and_call(self):
        one, two = self.create("one"), self.create("two")
        first = self.store.append_event(one["id"], "tool_result", {"a": 1, "b": 2}, "call-1")
        again = self.store.append_event(one["id"], "tool_result", {"b": 2, "a": 1}, "call-1")
        self.assertEqual(first, again)
        with self.assertRaises(StoreConflict):
            self.store.append_event(one["id"], "tool_result", {"a": 9}, "call-1")
        self.store.append_event(one["id"], "tool_requested", {"a": 1}, "call-1")
        self.store.append_event(two["id"], "tool_result", {"a": 1}, "call-1")
        self.assertEqual(len(self.store.events(one["id"])), 2)
        self.assertEqual(len(self.store.events(two["id"])), 1)

    def test_event_cursor_uses_global_ids_without_cross_run_leakage(self):
        one, two = self.create("one"), self.create("two")
        first = self.store.append_event(one["id"], "note", {"n": 1})
        other = self.store.append_event(two["id"], "note", {"n": 2})
        last = self.store.append_event(one["id"], "note", {"n": 3})
        self.assertLess(first["id"], other["id"])
        self.assertLess(other["id"], last["id"])
        self.assertEqual(self.store.events(one["id"], after=first["id"]), [last])

    def test_restart_interrupts_active_only_and_preserves_checkpoint(self):
        queued = self.create("queued")
        diagnosing = self.create("diagnosing")
        awaiting = self.create("awaiting")
        verifying = self.create("verifying")
        finished = self.create("finished")
        self.advance(diagnosing["id"], "diagnosing")
        self.store.transition(diagnosing["id"], {"diagnosing"}, "diagnosing", {"checkpoint": {"tool": "read"}})
        self.advance(awaiting["id"], "awaiting_approval")
        self.advance(verifying["id"], "verifying")
        self.advance(finished["id"], "succeeded")
        restarted = Store(self.database)
        self.assertCountEqual(restarted.recover_active(), [diagnosing["id"], verifying["id"]])
        self.assertEqual(restarted.get_run(queued["id"])["state"], "queued")
        self.assertEqual(restarted.get_run(awaiting["id"])["state"], "awaiting_approval")
        self.assertEqual(restarted.get_run(finished["id"])["state"], "succeeded")
        recovered = restarted.get_run(diagnosing["id"])
        self.assertEqual(recovered["checkpoint"], {"tool": "read"})
        self.assertEqual(recovered["stop_reason"], "process_restart")
        self.assertEqual(restarted.recover_active(), [])
        resumed = restarted.transition(diagnosing["id"], {"interrupted"}, "queued", expected_version=recovered["version"])
        self.assertEqual(resumed["state"], "queued")
        verifying_row = restarted.get_run(verifying["id"])
        self.assertEqual(restarted.transition(verifying["id"], {"interrupted"}, "verifying",
                                             expected_version=verifying_row["version"])["state"], "verifying")

    def test_recovery_is_one_transaction_if_event_recording_fails(self):
        first, second = self.create("one"), self.create("two")
        self.advance(first["id"], "diagnosing")
        self.advance(second["id"], "diagnosing")
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TRIGGER reject_recovery BEFORE INSERT ON events "
                               "WHEN NEW.payload LIKE '%process_restart%' "
                               "BEGIN SELECT RAISE(ABORT, 'injected recovery failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.recover_active()
        self.assertEqual(self.store.get_run(first["id"])["state"], "diagnosing")
        self.assertEqual(self.store.get_run(second["id"])["state"], "diagnosing")

    def test_missing_runs_fail_consistently(self):
        for action in [lambda: self.store.get_run("missing"),
                       lambda: self.store.transition("missing", {"queued"}, "diagnosing"),
                       lambda: self.store.append_event("missing", "note", {}),
                       lambda: self.store.events("missing")]:
            with self.assertRaises(RunNotFound):
                action()

    def test_query_caps_order_and_json_validation(self):
        rows = [self.create(f"run-{index}") for index in range(101)]
        self.assertEqual(len(self.store.list_runs(10000)), 100)
        self.assertEqual(self.store.list_runs(1)[0]["id"], rows[-1]["id"])
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                "INSERT INTO events(run_id, kind, payload, created_at) VALUES (?, 'note', ?, 'fixture')",
                [(rows[0]["id"], json.dumps({"n": number})) for number in range(1001)],
            )
        events = self.store.events(rows[0]["id"])
        self.assertEqual(len(events), 1000)
        tail = self.store.events(rows[0]["id"], events[-1]["id"])
        self.assertEqual([event["payload"]["n"] for event in tail], [1000])
        with self.assertRaises(ValueError):
            self.store.create_run("task", "demo", "bad-number", {"cost": float("nan")})


if __name__ == "__main__":
    unittest.main()
