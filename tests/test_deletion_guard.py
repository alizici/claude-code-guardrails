import unittest

from helpers import DELETION, Project

CODE = """class Account {
  int balance = 0;

  void withdraw(int amount) {
    if (amount > balance) throw StateError('insufficient funds');
    balance -= amount;
  }
}
"""


class DeletionGuardTests(unittest.TestCase):
    def setUp(self):
        self.p = Project(config={})
        self.p.write("lib/account.dart", CODE)
        self.p.hook(DELETION, "turn", {"session_id": "s"})

    def tearDown(self):
        self.p.cleanup()

    def before(self, old, new, tool="Edit", tid="t"):
        ti = {"file_path": self.p.path("lib/account.dart")}
        ti.update({"content": new} if tool == "Write" else {"old_string": old, "new_string": new})
        _, out = self.p.hook(DELETION, "before", {"session_id": "s", "tool_name": tool, "tool_use_id": tid,
                                                  "tool_input": ti})
        return out["hookSpecificOutput"]["permissionDecision"] if out else None

    def test_selftest(self):
        r = self.p.run(DELETION, "selftest")
        self.assertEqual(r.stdout.strip().splitlines()[-1], "ok", r.stdout)

    def test_dropping_a_guard_clause_is_denied(self):
        line = "    if (amount > balance) throw StateError('insufficient funds');\n"
        self.assertEqual(self.before(line, ""), "deny")

    def test_editing_a_line_is_allowed(self):
        self.assertIsNone(self.before("balance -= amount;", "balance = balance - amount;"))

    def test_rewriting_with_write_is_denied(self):
        self.assertEqual(self.before(None, "class Account {}\n", tool="Write"), "deny")

    def test_declared_deletion_is_allowed(self):
        r = self.p.run(DELETION, "allow", self.p.path("lib/account.dart"), "--line", "insufficient funds",
                       "--reason", "check moved into a validator")
        self.assertEqual(r.returncode, 0, r.stdout)
        line = "    if (amount > balance) throw StateError('insufficient funds');\n"
        self.assertIsNone(self.before(line, ""))

    def test_shell_deletion_blocks_the_turn_and_can_be_restored(self):
        self.p.write("lib/account.dart", CODE.replace(
            "    if (amount > balance) throw StateError('insufficient funds');\n", ""))
        _, out = self.p.hook(DELETION, "gate", {"session_id": "s"})
        self.assertEqual(out["decision"], "block")
        self.assertIn("insufficient funds", out["reason"])
        self.assertEqual(self.p.run(DELETION, "restore", "1", self.p.path("lib/account.dart")).returncode, 0)
        self.assertIn("insufficient funds", self.p.read("lib/account.dart"))

    def test_snapshots_never_touch_the_branch(self):
        self.assertNotEqual(self.p.git("rev-parse", "-q", "--verify", "HEAD", check=False).returncode, 0)
        self.assertIn("refs/guardrails/", self.p.git("for-each-ref").stdout)


if __name__ == "__main__":
    unittest.main()
