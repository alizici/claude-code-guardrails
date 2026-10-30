import os
import unittest

from helpers import GUARD, Project

CONFIG = {
    "generated": {"paths": ["lib/**/*.g.dart"]},
    "block": [{"pattern": r"\bgit\s+push\b[^;&|\n]*(?:--force(?![-\w])|\s-f\b)", "reason": "no force push"}],
    "ask": [{"pattern": r"\bdeploy\.sh\b", "reason": "deploys need approval"}],
    "ask_paths": [{"glob": "secrets/**", "reason": "secrets are protected"}],
    "roles": {
        "test-writer": {"write": ["test/**"], "note": "tests only"},
        "reviewer": {"write": []},
    },
    "gates": [{"name": "tests", "on": ["lib/**"], "run": "test -f lib/ok", "timeout": 30}],
}


def decision(output):
    return output["hookSpecificOutput"]["permissionDecision"] if output else None


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.p = Project(CONFIG)

    def tearDown(self):
        self.p.cleanup()

    def protect(self, tool, tool_input, **extra):
        _, out = self.p.hook(GUARD, "protect", {"session_id": "s", "tool_name": tool, "tool_input": tool_input,
                                                "cwd": self.p.root, **extra})
        return out

    # --- protection ---------------------------------------------------------

    def test_generated_file_by_path_is_denied(self):
        out = self.protect("Edit", {"file_path": self.p.path("lib/model.g.dart")})
        self.assertEqual(decision(out), "deny")

    def test_generated_file_by_stamp_is_denied(self):
        self.p.write("lib/api.dart", "// GENERATED CODE - DO NOT MODIFY BY HAND\nclass Api {}\n")
        out = self.protect("Write", {"file_path": self.p.path("lib/api.dart"), "content": "x"})
        self.assertEqual(decision(out), "deny")

    def test_normal_file_is_allowed(self):
        self.assertIsNone(self.protect("Edit", {"file_path": self.p.path("lib/app.dart")}))

    def test_ask_path_asks(self):
        self.assertEqual(decision(self.protect("Write", {"file_path": self.p.path("secrets/key.txt")})), "ask")

    def test_guard_setup_asks(self):
        self.assertEqual(decision(self.protect("Edit", {"file_path": self.p.path("guardrails.json")})), "ask")
        self.assertEqual(decision(self.protect("Edit", {"file_path": self.p.path(".claude/settings.json")})), "ask")

    def test_blocked_command(self):
        self.assertEqual(decision(self.protect("Bash", {"command": "git push -f origin main"})), "deny")

    def test_force_with_lease_is_not_force_push(self):
        self.assertIsNone(self.protect("Bash", {"command": "git push --force-with-lease origin main"}))

    def test_ask_command(self):
        self.assertEqual(decision(self.protect("Bash", {"command": "./deploy.sh prod"})), "ask")

    def test_shell_redirect_into_generated_file_is_denied(self):
        out = self.protect("Bash", {"command": "echo x > lib/model.g.dart"})
        self.assertEqual(decision(out), "deny")

    def test_sed_in_place_on_protected_path_asks(self):
        self.assertEqual(decision(self.protect("Bash", {"command": "sed -i 's/a/b/' secrets/key.txt"})), "ask")

    def test_deleting_a_folder_that_contains_protected_files(self):
        os.makedirs(self.p.path("secrets"), exist_ok=True)
        self.assertEqual(decision(self.protect("Bash", {"command": "rm -rf secrets"})), "ask")

    def test_python_one_liner_writing_protected_file_asks(self):
        cmd = "python3 -c \"open('secrets/key.txt','w').write('x')\""
        self.assertEqual(decision(self.protect("Bash", {"command": cmd})), "ask")

    def test_quoted_separator_is_not_a_command_boundary(self):
        self.assertIsNone(self.protect("Bash", {"command": "echo 'a > secrets/key.txt'"}))

    # --- roles --------------------------------------------------------------

    def test_test_writer_cannot_touch_production_code(self):
        out = self.protect("Edit", {"file_path": self.p.path("lib/app.dart")},
                           agent_id="a1", agent_type="test-writer")
        self.assertEqual(decision(out), "deny")

    def test_test_writer_can_write_tests(self):
        self.assertIsNone(self.protect("Edit", {"file_path": self.p.path("test/app_test.dart")},
                                       agent_id="a1", agent_type="test-writer"))

    def test_reviewer_is_read_only_in_the_project(self):
        out = self.protect("Bash", {"command": "echo note > review.md"}, agent_id="a2", agent_type="reviewer")
        self.assertEqual(decision(out), "deny")

    def test_reviewer_may_write_outside_the_project(self):
        self.assertIsNone(self.protect("Bash", {"command": "echo note > /tmp/review.md"},
                                       agent_id="a2", agent_type="reviewer"))

    def test_main_session_has_no_role_limits(self):
        self.assertIsNone(self.protect("Edit", {"file_path": self.p.path("lib/app.dart")}))

    # --- end-of-turn gates --------------------------------------------------

    def turn(self):
        self.p.hook(GUARD, "turn", {"session_id": "s"})

    def stop(self):
        return self.p.hook(GUARD, "gates", {"session_id": "s"})[1]

    def test_failing_gate_blocks_the_turn(self):
        self.p.write("README.md", "x")
        self.p.commit_all()
        self.turn()
        self.p.write("lib/app.dart", "class App {}\n")  # changed with any tool, even a shell
        out = self.stop()
        self.assertEqual(out and out.get("decision"), "block")

    def test_passing_gate_lets_the_turn_end(self):
        self.p.write("README.md", "x")
        self.p.commit_all()
        self.turn()
        self.p.write("lib/app.dart", "class App {}\n")
        self.p.write("lib/ok", "")
        self.assertIsNone(self.stop())

    def test_untriggered_gate_does_not_run(self):
        self.p.write("README.md", "x")
        self.p.commit_all()
        self.turn()
        self.p.write("docs/notes.md", "x")
        self.assertIsNone(self.stop())

    def test_no_progress_means_no_endless_block(self):
        self.p.write("README.md", "x")
        self.p.commit_all()
        self.turn()
        self.p.write("lib/app.dart", "class App {}\n")
        self.assertEqual(self.stop().get("decision"), "block")
        second = self.stop()  # nothing changed since the block: warn, don't loop
        self.assertNotEqual(second and second.get("decision"), "block")
        self.assertIn("still failing", second["systemMessage"])

    def test_config_check_passes(self):
        r = self.p.run(GUARD, "check")
        self.assertEqual(r.stdout.strip().splitlines()[-1], "ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
