import unittest
from src.batches import add_batch


class BatchExpiryTests(unittest.TestCase):
    def test_newer_stock_does_not_extend_old_expiry(self):
        old = [{"name": "spinach", "quantity": 200, "expires_on": "2026-10-29"}]
        actual = add_batch(old, "Spinach", 100, "2026-11-07")
        self.assertEqual(actual, [
            {"name": "spinach", "quantity": 200, "expires_on": "2026-10-29"},
            {"name": "spinach", "quantity": 100, "expires_on": "2026-11-07"},
        ])
        self.assertEqual(old[0]["quantity"], 200)
        self.assertEqual(old[0]["expires_on"], "2026-10-29")

    def test_identical_expiry_can_merge(self):
        old = [{"name": "spinach", "quantity": 200, "expires_on": "2026-10-29"}]
        self.assertEqual(add_batch(old, " spinach ", 50, "2026-10-29"),
                         [{"name": "spinach", "quantity": 250, "expires_on": "2026-10-29"}])

    def test_unrelated_ingredient_is_preserved(self):
        old = [{"name": "rice", "quantity": 100, "expires_on": "2027-01-01"}]
        self.assertEqual(add_batch(old, "spinach", 50, "2026-10-29")[0], old[0])

    def test_invalid_new_batch_is_rejected_before_changes(self):
        old = [{"name": "rice", "quantity": 100, "expires_on": "2027-01-01"}]
        with self.assertRaises(ValueError):
            add_batch(old, "rice", 0, "2027-01-01")
        with self.assertRaises(ValueError):
            add_batch(old, "rice", 50, "invalid-date")
        self.assertEqual(old[0]["quantity"], 100)
