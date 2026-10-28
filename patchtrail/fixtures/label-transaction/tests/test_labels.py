import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from src.labels import update_label


class LabelTransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="patchtrail-label-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "labels.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.executescript("""
                CREATE TABLE samples(id INTEGER PRIMARY KEY, label TEXT NOT NULL);
                CREATE TABLE history(id INTEGER PRIMARY KEY, sample_id INTEGER NOT NULL,
                                     old_label TEXT NOT NULL, new_label TEXT NOT NULL, note TEXT NOT NULL);
                INSERT INTO samples VALUES (1, 'ham');
                CREATE TRIGGER reject_history BEFORE INSERT ON history
                WHEN NEW.note = 'reject'
                BEGIN SELECT RAISE(ABORT, 'injected history failure'); END;
            """)

    def state(self):
        with sqlite3.connect(self.database) as connection:
            return (connection.execute("SELECT label FROM samples WHERE id = 1").fetchone()[0],
                    connection.execute("SELECT old_label, new_label, note FROM history").fetchall())

    def test_history_failure_rolls_back_the_label(self):
        with self.assertRaises(sqlite3.IntegrityError):
            update_label(self.database, 1, "spam", "reject")
        self.assertEqual(self.state(), ("ham", []))

    def test_success_persists_label_and_history_together(self):
        self.assertEqual(update_label(self.database, 1, "spam", "reviewed"),
                         {"changed": True, "label": "spam"})
        self.assertEqual(self.state(), ("spam", [("ham", "spam", "reviewed")]))

    def test_identical_label_is_a_noop(self):
        self.assertEqual(update_label(self.database, 1, "ham"), {"changed": False, "label": "ham"})
        self.assertEqual(self.state(), ("ham", []))

    def test_unknown_sample_is_rejected(self):
        with self.assertRaises(LookupError):
            update_label(self.database, 999, "spam")
        self.assertEqual(self.state(), ("ham", []))
