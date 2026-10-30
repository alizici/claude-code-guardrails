import os
import subprocess
import unittest

from helpers import SHIELD, Project

CODE = """def fee(amount):
    # SHIELD: fee-rule
    if amount <= 0:
        raise ValueError("amount must be positive")
    return round(amount * 0.02, 2)
    # SHIELD-END: fee-rule
"""

TEST = """import unittest
from fees import fee

class FeeRule(unittest.TestCase):
    def test_two_percent(self):
        self.assertEqual(fee(100), 2.0)

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            fee(0)
"""

LOCKS = {
    "runners": {"unit": {"run": "python3 -m unittest {tests}", "cwd": "app", "timeout": 60}},
    "locks": [{"name": "fee-rule", "file": "app/fees.py", "region": "fee-rule", "reason": "money",
               "tests": {"unit": ["test_fees.FeeRule"]}, "fingerprint": ""}],
}


class ShieldTests(unittest.TestCase):
    def setUp(self):
        self.p = Project(shield=LOCKS)
        self.p.write("app/fees.py", CODE)
        self.p.write("app/test_fees.py", TEST)
        r = self.p.run(SHIELD, "approve", "fee-rule", "--reason", "initial lock of the fee rule")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.p.commit_all("base").returncode, 0)
        subprocess.run(["sh", os.path.join(self.p.root, "guardrails", "install_git_hooks.sh")], cwd=self.p.root,
                       capture_output=True, check=True)

    def tearDown(self):
        self.p.cleanup()

    def break_rule(self):
        self.p.write("app/fees.py", CODE.replace("0.02", "0.03"))

    def test_clean_tree_passes(self):
        self.assertEqual(self.p.run(SHIELD, "check").returncode, 0)

    def test_whitespace_only_change_is_not_a_change(self):
        self.p.write("app/fees.py", CODE.replace("    return", "    \n    return"))
        self.assertEqual(self.p.run(SHIELD, "check").returncode, 0)

    def test_code_outside_the_region_is_free(self):
        self.p.write("app/fees.py", "import math\n" + CODE)
        self.assertEqual(self.p.run(SHIELD, "check").returncode, 0)

    def test_change_inside_the_region_is_caught(self):
        self.break_rule()
        r = self.p.run(SHIELD, "check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("CHANGED  fee-rule", r.stdout)

    def test_deleted_markers_are_caught(self):
        self.p.write("app/fees.py", "def fee(amount):\n    return amount\n")
        r = self.p.run(SHIELD, "check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DELETED  fee-rule", r.stdout)

    def test_pre_commit_blocks_unapproved_change(self):
        self.break_rule()
        r = self.p.commit_all("sneaky")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Commit stopped", r.stdout + r.stderr)

    def test_cherry_pick_cannot_skip_the_shield(self):
        # an unapproved change is committed on a side branch with the hooks bypassed...
        self.p.git("checkout", "-q", "-b", "side")
        self.break_rule()
        self.p.git("add", "-A")
        self.p.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "bypass")
        sha = self.p.git("rev-parse", "HEAD").stdout.strip()
        self.p.git("checkout", "-q", "main")
        self.p.write("README.md", "main moved on\n")  # so the cherry-pick creates a new commit
        self.assertEqual(self.p.commit_all("unrelated").returncode, 0)
        # ...and cherry-picked (pre-commit doesn't run here): reference-transaction stops it
        r = self.p.git("cherry-pick", sha, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CHANGED  fee-rule", r.stdout + r.stderr)

    def test_approve_requires_a_reason(self):
        self.break_rule()
        r = self.p.run(SHIELD, "approve", "fee-rule", "--reason", "x")
        self.assertEqual(r.returncode, 1)

    def test_approved_change_passes_and_is_logged(self):
        self.break_rule()
        r = self.p.run(SHIELD, "approve", "fee-rule", "--reason", "fee raised to 3% per the new contract")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.p.run(SHIELD, "check").returncode, 0)
        self.assertIn("fee raised to 3%", self.p.read("docs/SHIELD_LOG.md"))

    def test_gate_runs_lock_tests_for_unverified_locks(self):
        self.break_rule()
        self.p.run(SHIELD, "approve", "fee-rule", "--reason", "fee raised to 3% per the new contract")
        r = self.p.run(SHIELD, "gate")  # approved, but the tests still expect 2%
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stdout)

    def test_gate_passes_with_good_tests(self):
        r = self.p.run(SHIELD, "gate")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("verified", self.p.run(SHIELD, "list").stdout)


if __name__ == "__main__":
    unittest.main()
