"""check_result on piped test commands: a cut-off summary is never a pass (seen in a real pytest | head run)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notebook  # noqa: E402

COLLECT_ERROR = ("___ ERROR collecting tests/test_cart.py ___\n"
                 "ImportError while importing test module 'tests/test_cart.py'.\n")


class PipedChecks(unittest.TestCase):
    def test_head_cut_collection_error_is_a_failure(self):
        cmd = 'cd "x" && cat .pytest.ini pyproject.toml; python -m pytest 2>&1 | head -5'
        self.assertIs(notebook.check_result(cmd, True, COLLECT_ERROR), False)

    def test_head_without_a_summary_is_unknown(self):
        self.assertIsNone(notebook.check_result("python -m pytest 2>&1 | head -3", True, "a\nb\n"))

    def test_tail_with_a_pass_summary_is_a_pass(self):
        self.assertIs(notebook.check_result("python -m pytest | tail -5", True, "4 passed in 0.02s\n"), True)

    def test_tail_with_a_collection_error_is_a_failure(self):
        out = "Interrupted: 1 error during collection\n1 error in 0.16s\n"
        self.assertIs(notebook.check_result("python -m pytest 2>&1 | tail -20", True, out), False)


if __name__ == "__main__":
    unittest.main()
