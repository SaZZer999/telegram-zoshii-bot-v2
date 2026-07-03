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
