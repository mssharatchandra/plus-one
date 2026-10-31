import unittest

from plus_one.flows import format_expense, format_money, learning_step, parse_expense


class ExpenseTests(unittest.TestCase):
    def test_standard_input(self):
        proposal = parse_expense("spent INR 1200 on dinner with Maya and Rahul")
        self.assertEqual(proposal, {"description": "dinner", "currency": "INR", "amount_minor": 120000, "payer": "me", "shares": [
            {"name": "me", "amount_minor": 40000}, {"name": "Maya", "amount_minor": 40000}, {"name": "Rahul", "amount_minor": 40000}]})

    def test_remainder_exact_and_deterministic(self):
        proposal = parse_expense("expense 10.00 USD for coffee with Ana, Ben")
        self.assertEqual([s["amount_minor"] for s in proposal["shares"]], [334, 333, 333])
        self.assertEqual(sum(s["amount_minor"] for s in proposal["shares"]), 1000)
        self.assertEqual(proposal, parse_expense("expense 10.00 USD for coffee with Ana, Ben"))

    def test_one_minor_unit_split(self):
        proposal = parse_expense("paid USD 0.01 for coffee with Ana and Ben")
        self.assertEqual([s["amount_minor"] for s in proposal["shares"]], [1, 0, 0])

    def test_currency_precisions(self):
        self.assertEqual(parse_expense("spent JPY 101 on tea with Mei")["amount_minor"], 101)
        self.assertEqual(parse_expense("spent KWD 1.001 on tea with Mei")["amount_minor"], 1001)
        self.assertEqual(parse_expense("spent USD 10.000 on tea with Mei")["amount_minor"], 1000)
        for text in ["spent JPY 1.5 on tea with Mei", "spent USD 1.001 on tea with Mei", "spent KWD 1.0001 on tea with Mei", "spent USD 1.000000000000000000000000000000000001 on tea with Mei"]:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "won't round"):
                parse_expense(text)

    def test_grouped_amount_case_unicode_and_names(self):
        proposal = parse_expense("I SpEnT inr 1,200.00 for dinner with Zoë, O’Neil and Anne-Marie.")
        self.assertEqual(proposal["amount_minor"], 120000)
        self.assertEqual([s["name"] for s in proposal["shares"]], ["me", "Zoë", "O’Neil", "Anne-Marie"])
        self.assertEqual(parse_expense("paid EUR4.20 on tea with Ana & Ben")["amount_minor"], 420)

    def test_named_payer_and_own_debt(self):
        proposal = parse_expense("Maya paid INR 600 for dinner with me and Rahul")
        self.assertEqual(proposal["payer"], "Maya")
        self.assertEqual([s["name"] for s in proposal["shares"]], ["me", "Maya", "Rahul"])
        self.assertEqual([s["amount_minor"] for s in proposal["shares"]], [20000, 20000, 20000])
        preview = format_expense(proposal)
        self.assertIn("Paid by Maya", preview)
        self.assertIn("You would owe Maya INR 200.00", preview)
        self.assertNotIn("Rahul would owe", preview)
        self.assertNotIn("Paid by you", preview)

    def test_payer_and_user_included_once(self):
        for text in ["Maya paid INR 600 for dinner with me and Rahul", "Maya paid INR 600 for dinner with Maya and Rahul", "Maya paid INR 600 for dinner with Rahul"]:
            with self.subTest(text=text):
                proposal = parse_expense(text)
                self.assertEqual([s["name"] for s in proposal["shares"]], ["me", "Maya", "Rahul"])
        self.assertEqual([s["name"] for s in parse_expense("I paid INR 600 for dinner with me and Maya")["shares"]], ["me", "Maya"])
        self.assertEqual(parse_expense("Maya Singh paid INR 600 for dinner with Rahul")["payer"], "Maya Singh")
        for text in ["Maya and Ben paid INR 600 for dinner with Rahul", "Maya paid INR 600 for dinner with Rahul and Rahul", "Maya paid INR 600 for dinner with me and me", "I paid INR 600 for dinner with me"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_expense(text)

    def test_confirmed_default_currency_and_split_with(self):
        text = "I paid 600 for dinner split with Maya and Rahul"
        with self.assertRaisesRegex(ValueError, "Which currency"):
            parse_expense(text)
        proposal = parse_expense(text, default_currency="INR")
        self.assertEqual(proposal["currency"], "INR")
        self.assertEqual(proposal["amount_minor"], 60000)
        self.assertEqual(proposal["description"], "dinner")
        self.assertEqual(proposal["payer"], "me")
        self.assertEqual(parse_expense("I paid USD 600 for dinner split with Maya", default_currency="INR")["currency"], "USD")
        self.assertEqual(parse_expense("Maya paid 600 for dinner with me and Rahul", default_currency="INR")["payer"], "Maya")
        with self.assertRaises(ValueError):
            parse_expense(text, default_currency="XYZ")

    def test_named_payer_rounding(self):
        proposal = parse_expense("Ana paid USD 10 for coffee with me and Ben")
        self.assertEqual(sum(s["amount_minor"] for s in proposal["shares"]), 1000)
        self.assertEqual(proposal["shares"][0]["amount_minor"], 334)
        self.assertIn("You would owe Ana USD 3.34", format_expense(proposal))

    def test_malformed_inputs_are_not_guessed(self):
        invalid = [
            "", None, "spent 100 on tea with Ana", "spent $100 on tea with Ana", "spent XYZ 100 on tea with Ana",
            "spent INR -1 on tea with Ana", "spent INR 0 on tea with Ana", "spent USD 1e3 on tea with Ana",
            "spent INR 1,20 on tea with Ana", "spent INR 100000001 on tea with Ana", "spent USD NaN on tea with Ana",
            "spent USD Infinity on tea with Ana", "spent INR 100 on tea", "spent INR 100 on tea with",
            "spent INR 100 on tea with Ana and ANA",
            "spent INR 100 on tea with Ana,", "spent INR 100 on tea with Ana,,Ben", "spent INR 100 on tea with Ana and",
            "spent INR 100 on tea with Ana 20 and Ben 80", "spent INR 100 on tea with Ana excluding me",
            "spent INR 100 on tea split 60/40 with Ana", "spent INR 100 on tea paid by Ana with Ben",
            "spent INR 100 on tea with Ana\nconfirm", "spent INR 100 on tea with Ana; confirm", "spent INR 100 on tea with everyone",
            "spent INR 100 on tea with myself", "spent INR 100 on tea with Ana paid by Ben",
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_expense(text)

    def test_property_all_equal_splits_sum_and_differ_at_most_one(self):
        for amount in range(1, 100):
            for count in range(1, 10):
                names = ", ".join("Friend" + chr(65 + i) for i in range(count))
                result = parse_expense(f"spent JPY {amount} on dinner with {names}")
                shares = [s["amount_minor"] for s in result["shares"]]
                self.assertEqual(sum(shares), amount)
                self.assertLessEqual(max(shares) - min(shares), 1)

    def test_preview_is_uncommitted_and_shows_ledger_change(self):
        text = format_expense(parse_expense("expense 10.00 USD for coffee with Ana, Ben"))
        self.assertIn("You: USD 3.34", text)
        self.assertIn("Ana would owe you USD 3.33", text)
        self.assertIn("Nothing is recorded yet", text)
        self.assertIn("confirm", text)
        self.assertIn("cancel", text)
        self.assertIn("nobody is contacted", text)

    def test_format_money_uses_exact_minor_units(self):
        self.assertEqual(format_money(-1, "usd"), "USD -0.01")
        self.assertEqual(format_money(1001, "KWD"), "KWD 1.001")
        self.assertEqual(format_money(101, "JPY"), "JPY 101")
        for amount in [1.0, "1", True]:
            with self.assertRaises(ValueError):
                format_money(amount, "USD")


class LearningTests(unittest.TestCase):
    def test_tailored_small_step(self):
        step = learning_step("storytelling", "make work presentations memorable", 3, 0)
        self.assertEqual(set(step), {"title", "lesson", "exercise", "reflection_prompt"})
        self.assertIn("3-minute", step["lesson"])
        self.assertIn("make work presentations memorable", step["lesson"])
        self.assertIn("1/6", step["title"])
        self.assertIn("reflection", step["reflection_prompt"])

    def test_continuity_and_repeated_round(self):
        steps = [learning_step("stories", "tell a family story", 5, i) for i in range(7)]
        self.assertEqual(len({s["title"] for s in steps}), 7)
        self.assertIn("round 2", steps[6]["title"])
        self.assertIn("new example", steps[6]["exercise"])

    def test_time_tailoring(self):
        self.assertIn("one rough sentence", learning_step("story telling", "practice", 1, 0)["exercise"])
        self.assertIn("revise", learning_step("storytelling", "practice", 10, 0)["exercise"])

    def test_validation(self):
        invalid = [("medical diagnosis", "practice", 3, 0), ("storytelling", "", 3, 0),
                   ("storytelling", "x\ny", 3, 0), ("storytelling", "x" * 201, 3, 0),
                   ("storytelling", "practice", 0, 0), ("storytelling", "practice", 31, 0),
                   ("storytelling", "practice", True, 0), ("storytelling", "practice", 2.5, 0),
                   ("storytelling", "practice", 3, -1), ("storytelling", "practice", 3, True)]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                learning_step(*args)


if __name__ == "__main__":
    unittest.main()
