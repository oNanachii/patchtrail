"""Same-instance contention over real temporary SQLite transactions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from patchtrail.storage import Store, StoreConflict


class StoreContentionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-storage-contention-")
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "runs.sqlite3"
        self.store = Store(self.database)

    def create(self, key):
        return self.store.create_run("quantity-shortfall", "demo", key, {"max_calls": 8})

    def test_public_reads_wait_for_the_same_store_transaction_to_commit(self):
        for method in ("get_run", "find_request", "list_runs", "events"):
            with self.subTest(method=method):
                key = "serialized-read-" + method
                row = self.create(key)
                inside_transaction = threading.Event()
                permit_commit = threading.Event()
                reader_started = threading.Event()
                reader_finished = threading.Event()
                insert_event = self.store._insert_event

                def paused_insert(*args, **kwargs):
                    # The run UPDATE has happened on a real uncommitted connection.
                    inside_transaction.set()
                    if not permit_commit.wait(3):
                        raise TimeoutError("Test did not release the transaction")
                    return insert_event(*args, **kwargs)

                def read():
                    reader_started.set()
                    try:
                        if method == "get_run":
                            return self.store.get_run(row["id"])
                        if method == "find_request":
                            return self.store.find_request(key)
                        if method == "list_runs":
                            return next(run for run in self.store.list_runs() if run["id"] == row["id"])
                        return self.store.events(row["id"])
                    finally:
                        reader_finished.set()

                with patch.object(self.store, "_insert_event", side_effect=paused_insert):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        writer = pool.submit(self.store.transition, row["id"], {"queued"}, "diagnosing")
                        try:
                            self.assertTrue(inside_transaction.wait(3))
                            reader = pool.submit(read)
                            self.assertTrue(reader_started.wait(3))
                            self.assertFalse(reader_finished.wait(0.05),
                                             "A same-instance read overlapped an uncommitted operation")
                        finally:
                            permit_commit.set()
                        writer.result(timeout=3)
                        observed = reader.result(timeout=3)
                if method == "events":
                    self.assertEqual(len(observed), 1)
                    self.assertEqual(observed[0]["payload"]["to"], "diagnosing")
                else:
                    self.assertEqual((observed["state"], observed["version"]), ("diagnosing", 1))

    def test_concurrent_readers_and_versioned_writers_preserve_all_committed_updates(self):
        row = self.create("concurrent-counter")
        self.store.transition(row["id"], {"queued"}, "diagnosing", {"checkpoint": {"counter": 0}})
        barrier = threading.Barrier(6)

        def writer():
            barrier.wait(timeout=3)
            committed = 0
            for _ in range(200):
                current = self.store.get_run(row["id"])
                try:
                    self.store.transition(row["id"], {"diagnosing"}, "diagnosing",
                                          {"checkpoint": {"counter": current["checkpoint"]["counter"] + 1}},
                                          expected_version=current["version"])
                except StoreConflict:
                    continue  # Public read and CAS are deliberately separate operations.
                committed += 1
                if committed == 10:
                    return committed
            raise AssertionError("A writer could not complete its bounded CAS retries")

        def reader():
            barrier.wait(timeout=3)
            observations = 0
            for _ in range(30):
                current = self.store.get_run(row["id"])
                self.assertEqual(current["version"], current["checkpoint"]["counter"] + 1)
                listed = next(run for run in self.store.list_runs() if run["id"] == row["id"])
                self.assertGreaterEqual(listed["version"], current["version"])
                versions = [event["payload"]["version"] for event in self.store.events(row["id"])
                            if event["kind"] == "state_changed"]
                self.assertGreaterEqual(max(versions), current["version"])
                observations += 1
            return observations

        with ThreadPoolExecutor(max_workers=6) as pool:
            writers = [pool.submit(writer) for _ in range(4)]
            readers = [pool.submit(reader) for _ in range(2)]
            self.assertEqual(sum(future.result(timeout=10) for future in writers), 40)
            self.assertEqual(sum(future.result(timeout=10) for future in readers), 60)
        final = self.store.get_run(row["id"])
        self.assertEqual((final["checkpoint"]["counter"], final["version"]), (40, 41))
        versions = [event["payload"]["version"] for event in self.store.events(row["id"])]
        self.assertEqual(versions, list(range(1, 42)))

    def test_failed_state_event_rolls_back_before_waiting_reader_and_releases_operation(self):
        row = self.create("rollback-during-contention")
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TRIGGER reject_state_event BEFORE INSERT ON events "
                               "BEGIN SELECT RAISE(ABORT, 'injected real event rejection'); END")
        inside_transaction = threading.Event()
        permit_insert = threading.Event()
        reader_started = threading.Event()
        reader_finished = threading.Event()
        insert_event = self.store._insert_event

        def paused_insert(*args, **kwargs):
            inside_transaction.set()
            if not permit_insert.wait(3):
                raise TimeoutError("Test did not release the transaction")
            return insert_event(*args, **kwargs)  # SQLite trigger raises after the run UPDATE.

        def read_after_rollback():
            reader_started.set()
            try:
                return self.store.get_run(row["id"])
            finally:
                reader_finished.set()

        with patch.object(self.store, "_insert_event", side_effect=paused_insert):
            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(self.store.transition, row["id"], {"queued"}, "diagnosing")
                try:
                    self.assertTrue(inside_transaction.wait(3))
                    reader = pool.submit(read_after_rollback)
                    self.assertTrue(reader_started.wait(3))
                    self.assertFalse(reader_finished.wait(0.05))
                finally:
                    permit_insert.set()
                with self.assertRaises(sqlite3.IntegrityError):
                    writer.result(timeout=3)
                observed = reader.result(timeout=3)
        self.assertEqual((observed["state"], observed["version"]), ("queued", 0))
        self.assertEqual(self.store.events(row["id"]), [])
        with sqlite3.connect(self.database) as connection:
            connection.execute("DROP TRIGGER reject_state_event")
        next_row = self.store.transition(row["id"], {"queued"}, "diagnosing", expected_version=0)
        self.assertEqual(next_row["version"], 1)
        self.assertEqual(len(self.store.events(row["id"])), 1)


if __name__ == "__main__":
    unittest.main()
