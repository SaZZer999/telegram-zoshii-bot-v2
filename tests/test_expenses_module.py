import ast
import os
import subprocess
import sys
import unittest
from decimal import Decimal


_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_EXPENSES_PATH = os.path.join(_REPO_ROOT, "expenses.py")


class TestExpensesModuleHasNoBotDependency(unittest.TestCase):
    """Static + subprocess proof that expenses.py never imports bot.py — the
    hard constraint of the bot.py/expenses.py split (bot.py imports
    expenses.py, so the reverse would be a cycle).
    """

    def test_source_contains_no_import_of_bot(self):
        with open(_EXPENSES_PATH, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="expenses.py")
        imported_names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.append(node.module)
        self.assertNotIn("bot", imported_names)

    def test_expenses_imports_standalone_without_bot(self):
        """expenses.py must be importable on its own, in a FRESH interpreter
        that never touches bot.py — proves there's no hidden runtime
        dependency (e.g. a lazy `import bot` inside a function body). Run in
        a subprocess rather than checking sys.modules in-process, since by
        the time this file runs under `unittest discover`, other test files
        in the same process have almost certainly already imported bot.
        """
        result = subprocess.run(
            [sys.executable, "-c", "import sys; assert 'bot' not in sys.modules; import expenses; assert 'bot' not in sys.modules; print('OK')"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("OK", result.stdout)


class TestExpensesPureHelpers(unittest.TestCase):
    """Narrow smoke tests that the moved pure helpers work correctly when
    used directly via `expenses.<name>`, independent of bot.py — the full
    behavioral coverage for these already lives in tests/test_expenses_v1.py,
    tests/test_expenses_reports.py, and tests/test_expense_delete.py (which
    exercise them through `bot.webhook()`); this just confirms the module
    boundary itself didn't silently change their behavior.
    """

    def setUp(self):
        import expenses
        self.expenses = expenses

    def test_parse_expense_amount_exact_decimal(self):
        self.assertEqual(self.expenses._parse_expense_amount("86,40 zł"), Decimal("86.40"))
        self.assertIsNone(self.expenses._parse_expense_amount("0"))
        self.assertIsNone(self.expenses._parse_expense_amount("-5"))

    def test_validate_expense_category_fallback(self):
        category, was_defaulted = self.expenses._validate_expense_category("Спорт")
        self.assertEqual(category, self.expenses.DEFAULT_EXPENSE_CATEGORY)
        self.assertTrue(was_defaulted)
        category, was_defaulted = self.expenses._validate_expense_category("Продукти")
        self.assertEqual(category, "Продукти")
        self.assertFalse(was_defaulted)

    def test_format_expense_amount(self):
        self.assertEqual(self.expenses._format_expense_amount(Decimal("86.40")), "86,40 zł")

    def test_expense_command_gate(self):
        self.assertTrue(self.expenses._expense_command_gate("86 zł"))
        self.assertFalse(self.expenses._expense_command_gate("Яка сьогодні погода?"))

    def test_short_zloty_marker_z_recognized_like_zl_and_pln(self):
        # Case 3/4 — "z" is recognized by the same shared gate/amount
        # machinery as "zł"/"zl"/"pln", with the same resulting Decimal.
        self.assertTrue(self.expenses._expense_command_gate("Купив молоко за 10 z"))
        self.assertEqual(self.expenses._parse_expense_amount("10 zl"), Decimal("10.00"))
        self.assertEqual(self.expenses._parse_expense_amount("10 PLN"), Decimal("10.00"))
        self.assertEqual(self.expenses._parse_expense_amount("10 z"), Decimal("10.00"))
        self.assertEqual(self.expenses._parse_expense_amount("10,50 z"), Decimal("10.50"))
        self.assertEqual(self.expenses._parse_expense_amount("10.50 z"), Decimal("10.50"))

        # Case 5 — a "z" immediately followed by another number is never an
        # amount+currency pair (e.g. "2 z 3", some unrelated count).
        self.assertFalse(self.expenses._EXPENSE_AMOUNT_RE.search("2 z 3"))

        # Case 6 — "z" that's just the start of another word never matches.
        self.assertFalse(self.expenses._EXPENSE_AMOUNT_RE.search("10 zebra"))

    def test_expense_delete_command_gate_requires_expense_word(self):
        self.assertTrue(self.expenses._expense_delete_command_gate("Видали витрату за булочку 4 zł"))
        self.assertFalse(self.expenses._expense_delete_command_gate("Видали булочку"))

    def test_expense_report_gate(self):
        self.assertEqual(self.expenses._expense_report_gate("🧾 Останні витрати"), "recent")
        self.assertEqual(self.expenses._expense_report_gate("📊 Цей місяць"), "monthly")
        self.assertIsNone(self.expenses._expense_report_gate("Що приготувати?"))

    def test_format_recent_expenses_empty(self):
        self.assertEqual(self.expenses._format_recent_expenses([]), "Витрат поки немає.")

    def test_format_expense_month_summary_empty(self):
        summary = {"total": Decimal("0"), "by_category": {}}
        text = self.expenses._format_expense_month_summary(summary, 2026, 7)
        self.assertIn("Витрат за цей місяць поки немає.", text)

    def test_validate_expense_router_result_unresolved_blocks(self):
        router_result = {
            "intent": "create_expense", "amount": "10", "currency": "PLN", "category": "Продукти",
            "description": "Тест", "expense_date": "2026-07-01", "unresolved_fragments": ["щось"],
        }
        kind, payload = self.expenses._validate_expense_router_result(router_result)
        self.assertEqual(kind, "unresolved")
        self.assertEqual(payload, ["щось"])


class TestFormatExpensesHub(unittest.TestCase):
    """Expenses Hub V1 — pure formatter for the "💸 Витрати" read-only
    dashboard (today's total, this month's total, last 5 expenses, add-
    expense examples)."""

    def setUp(self):
        import expenses
        self.expenses = expenses

    def _recent(self):
        return [
            {"description": "Інтернет", "category": "Дім і рахунки", "amount": Decimal("120.00")},
            {"description": "Кава", "category": "Кафе / ресторани", "amount": Decimal("14.00")},
            {"description": "", "category": "Продукти", "amount": Decimal("86.40")},
        ]

    def test_hub_shows_header_and_totals(self):
        text = self.expenses._format_expenses_hub(Decimal("134.00"), Decimal("1240.00"), self._recent())
        self.assertIn("💸 Витрати", text)
        self.assertIn("Сьогодні: 134,00 zł", text)
        self.assertIn("Цього місяця: 1240,00 zł", text)

    def test_hub_lists_recent_expenses_newest_first_with_clean_labels(self):
        text = self.expenses._format_expenses_hub(Decimal("220.40"), Decimal("220.40"), self._recent())
        lines = text.splitlines()
        self.assertIn("1. Інтернет — 120,00 zł", lines)
        self.assertIn("2. Кава — 14,00 zł", lines)
        # Falls back to category when description is blank — same
        # convention _format_recent_expenses/_format_expense_delete_list use.
        self.assertIn("3. Продукти — 86,40 zł", lines)
        idx1 = lines.index("1. Інтернет — 120,00 zł")
        idx2 = lines.index("2. Кава — 14,00 zł")
        idx3 = lines.index("3. Продукти — 86,40 zł")
        self.assertLess(idx1, idx2)
        self.assertLess(idx2, idx3)

    def test_hub_shows_add_expense_examples(self):
        text = self.expenses._format_expenses_hub(Decimal("0"), Decimal("0"), [])
        self.assertIn("Щоб додати витрату, напиши, наприклад:", text)
        self.assertIn("Кава 14 zł", text)
        self.assertIn("Запиши 120 zł за інтернет", text)
        self.assertIn("Biedronka 86,40 zł — продукти", text)

    def test_hub_handles_no_expenses_gracefully(self):
        text = self.expenses._format_expenses_hub(Decimal("0"), Decimal("0"), [])
        self.assertIn("Сьогодні: 0,00 zł", text)
        self.assertIn("Цього місяця: 0,00 zł", text)
        self.assertIn("Останніх витрат ще немає.", text)
        self.assertNotIn("Останні витрати:", text)


class TestCleanExpenseDescription(unittest.TestCase):
    """V1.4.2 live bug: Gemini sometimes returns the WHOLE raw command as
    the expense description instead of a clean name — _clean_expense_
    description is the single Python-side safety net that strips a leading
    command verb, any amount+currency span, and a leftover leading
    preposition before anything is stored."""

    def setUp(self):
        import expenses
        self.clean = expenses._clean_expense_description

    # 8/1. "Запиши 120 zł за інтернет" => "інтернет"
    def test_zapysy_za_strips_verb_amount_and_preposition(self):
        self.assertEqual(self.clean("Запиши 120 zł за інтернет"), "інтернет")

    # 9/2. "Запиши 120 zł на інтернет" => "інтернет"
    def test_zapysy_na_strips_verb_amount_and_preposition(self):
        self.assertEqual(self.clean("Запиши 120 zł на інтернет"), "інтернет")

    # 10/3. "120 zł за інтернет" => "інтернет" (no leading verb at all)
    def test_bare_amount_and_preposition_strips_to_clean_name(self):
        self.assertEqual(self.clean("120 zł за інтернет"), "інтернет")

    # 11/4. "Інтернет 120 zł" => "Інтернет" (amount trails the name)
    def test_trailing_amount_strips_to_clean_name(self):
        self.assertEqual(self.clean("Інтернет 120 zł"), "Інтернет")

    # 12/5. Already-clean Gemini output (the normal/expected case) passes
    # through unchanged — no over-stripping.
    def test_already_clean_description_is_unchanged(self):
        self.assertEqual(self.clean("Biedronka"), "Biedronka")

    # 13/6. "Кава 14 zł" => "Кава"
    def test_simple_trailing_amount_strips_cleanly(self):
        self.assertEqual(self.clean("Кава 14 zł"), "Кава")

    # 7. "запиши 39 злотих за інтернет" — "злотих" currency word recognized
    # by the description cleaner (independent of whether the numeric amount
    # parser itself is extended for it).
    def test_zloty_word_currency_is_recognized_and_stripped(self):
        self.assertEqual(self.clean("запиши 39 злотих за інтернет"), "інтернет")

    # "Додай" is also a recognized leading command verb, with an optional
    # "витрату" noun right after it.
    def test_doday_verb_with_vytratu_noun_is_stripped(self):
        self.assertEqual(self.clean("Додай витрату 50 zł на бензин"), "бензин")

    def test_blank_and_non_string_inputs_are_safe(self):
        self.assertEqual(self.clean(""), "")
        self.assertEqual(self.clean(None), "")
        self.assertEqual(self.clean("   "), "")


class TestExpensesPendingStateSharedWithBot(unittest.TestCase):
    """Confirms the re-export design: bot.<name> and expenses.<name> are the
    exact same objects (not copies), so state mutated through one module is
    immediately visible through the other — this is what keeps every
    pre-existing test that reads/writes bot.pending_expense (etc.) correct.
    """

    def test_pending_dicts_are_shared_by_identity(self):
        from unittest.mock import MagicMock
        sys.modules.setdefault('database', MagicMock())
        sys.modules.setdefault('groq', MagicMock())
        os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
        os.environ.setdefault('GROQ_API_KEY', 'test_groq_key')
        os.environ.setdefault('GEMINI_API_KEY', 'test_gemini_key')
        os.environ.setdefault('ALLOWED_USER_IDS', '')
        import bot
        import expenses
        self.assertIs(bot.pending_expense, expenses.pending_expense)
        self.assertIs(bot.pending_expense_delete, expenses.pending_expense_delete)
        self.assertIs(bot.expense_delete_selection, expenses.expense_delete_selection)
        self.assertIs(bot.EXPENSES_KEYBOARD, expenses.EXPENSES_KEYBOARD)


if __name__ == "__main__":
    unittest.main()
