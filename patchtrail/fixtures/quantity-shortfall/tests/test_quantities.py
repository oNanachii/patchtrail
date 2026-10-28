import unittest
from src.quantities import shopping_shortfall


class ShoppingShortfallTests(unittest.TestCase):
    def test_existing_stock_can_still_be_insufficient(self):
        self.assertEqual(shopping_shortfall({"spinach": 150, "pasta": 250}, {"spinach": 100}),
                         {"spinach": 50, "pasta": 250})

    def test_sufficient_stock_and_zero_requirements_do_not_create_purchases(self):
        self.assertEqual(shopping_shortfall({"spinach": 150, "salt": 0}, {"spinach": 200}), {})

    def test_fractional_quantities_and_input_preservation(self):
        required, stock = {"milk": 1.5}, {"milk": 0.25}
        self.assertEqual(shopping_shortfall(required, stock), {"milk": 1.25})
        self.assertEqual(required, {"milk": 1.5})
        self.assertEqual(stock, {"milk": 0.25})

    def test_invalid_quantities_are_rejected(self):
        for invalid in [-1, float("nan"), float("inf"), True, "1"]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    shopping_shortfall({"milk": invalid}, {})
