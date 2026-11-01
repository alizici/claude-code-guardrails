"""The guardrails installed as a Claude Code plugin: the scripts live in the plugin's folder, the project
has only guardrails.json and shield.json. Hooks are run with the exact commands from hooks/hooks.json."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import REPO

PLUGIN_PARTS = [".claude-plugin", "guardrails", "bin", "hooks", "agents", "skills",
                # in the real plugin cache too; they must never be mistaken for the project's
                "guardrails.json", "shield.json"]
SCRUB = ("GUARDRAILS_ROOT", "GUARDRAILS_BIN", "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")

with open(os.path.join(REPO, "hooks", "hooks.json"), encoding="utf-8") as f:
    HOOKS = json.load(f)["hooks"]


def hook_commands(event, script):
    """The commands hooks.json runs for an event, for one script (guard.py or deletion_guard.py)."""
    out = []
    for group in HOOKS[event]:
        for h in group["hooks"]:
            if "/" + script + '"' in h["command"]:
                out.append(h["command"])
    return out


class PluginProject(unittest.TestCase):
    def setUp(self):
        self.plugin = os.path.realpath(tempfile.mkdtemp(prefix="guardrails_plugin_"))
        for part in PLUGIN_PARTS:
            src, dst = os.path.join(REPO, part), os.path.join(self.plugin, part)
            if os.path.isdir(src):
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, dst)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="guardrails_project_"))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        self.env = {k: v for k, v in os.environ.items() if k not in SCRUB}
        self.env.update({"CLAUDE_PLUGIN_ROOT": self.plugin, "CLAUDE_PROJECT_DIR": self.root})

    def tearDown(self):
        shutil.rmtree(self.plugin, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    # --- helpers --------------------------------------------------------------

    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)) or self.root, exist_ok=True)
        with open(self.path(rel), "w", encoding="utf-8") as f:
            f.write(text)

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", self.root, *args], capture_output=True, text=True, check=check)

    def hook(self, event_name, script, event):
        """Runs every hooks.json command of `event_name` for `script` like Claude Code does (a shell, the
        plugin's env vars); returns the parsed JSON output of the last one (None if it printed nothing)."""
        out = None
        for command in hook_commands(event_name, script):
            r = subprocess.run(["sh", "-c", command], input=json.dumps({"session_id": "s", "cwd": self.root, **event}),
                               capture_output=True, text=True, env=self.env, cwd=self.root)
            self.assertNotEqual(r.returncode, 2, "a hook must never exit with 2: " + r.stderr)
            out = json.loads(r.stdout) if r.stdout.strip() else None
        return out

    def protect(self, tool, tool_input, **extra):
        out = self.hook("PreToolUse", "guard.py", {"tool_name": tool, "tool_input": tool_input, **extra})
        return out["hookSpecificOutput"]["permissionDecision"] if out else None

    def bin(self, name, *args):
        env = {k: v for k, v in self.env.items() if k not in SCRUB}  # as typed in the agent's Bash tool
        return subprocess.run([os.path.join(self.plugin, "bin", name), *args], capture_output=True, text=True,
                              cwd=self.root, env=env)

    def init(self, *args):
        r = self.bin("guardrails-init", *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    # --- opt-in -----------------------------------------------------------------

    def test_does_nothing_without_guardrails_json(self):
        self.write("lib/model.g.dart", "// GENERATED CODE - DO NOT MODIFY BY HAND\n")
        self.assertIsNone(self.protect("Edit", {"file_path": self.path("lib/model.g.dart")}))
        self.assertIsNone(self.hook("UserPromptSubmit", "deletion_guard.py", {}))
        self.assertEqual(self.git("for-each-ref", "refs/guardrails").stdout.strip(), "",
                         "the deletion guard took snapshots in a project that didn't opt in")
        self.assertIsNone(self.hook("Stop", "guard.py", {}))

    def test_steps_aside_for_a_copy_wired_into_the_project(self):
        shutil.copytree(os.path.join(REPO, "guardrails"), self.path("guardrails"))
        self.write(".claude/settings.json", json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/guardrails/guard.py" protect'}]}]}}))
        self.write("guardrails.json", json.dumps({"generated": {"paths": ["lib/**/*.g.dart"]}}))
        self.assertIsNone(self.protect("Edit", {"file_path": self.path("lib/model.g.dart")}),
                          "the plugin checked a project whose own copy already does")

    # --- init -------------------------------------------------------------------

    def test_init_detects_flutter_and_never_overwrites(self):
        self.write("pubspec.yaml", "name: app\ndependencies:\n  flutter:\n    sdk: flutter\n")
        out = self.init("--no-git-hooks")
        self.assertIn("Flutter/Dart", out)
        with open(self.path("guardrails.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("**/*.g.dart", cfg["generated"]["paths"])
        self.assertTrue(any(g["run"].startswith("flutter test") for g in cfg["gates"]))
        self.assertIn("lib/**", cfg["roles"]["implementer"]["write"])
        self.write("guardrails.json", "{}")
        self.assertIn("kept", self.init("--no-git-hooks"))
        with open(self.path("guardrails.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{}")

    def test_init_output_passes_check(self):
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        self.init("--no-git-hooks")
        r = self.bin("guardrails-check")
        self.assertEqual(r.stdout.strip().splitlines()[-1], "ok", r.stdout + r.stderr)

    # --- guard ------------------------------------------------------------------

    def test_guard_protects_the_project_not_the_plugin(self):
        self.init("--no-git-hooks")
        self.write("lib/api.dart", "// GENERATED CODE - DO NOT MODIFY BY HAND\nclass Api {}\n")
        self.assertEqual(self.protect("Write", {"file_path": self.path("lib/api.dart"), "content": "x"}), "deny")
        self.assertEqual(self.protect("Edit", {"file_path": self.path("guardrails.json")}), "ask")
        self.assertIsNone(self.protect("Edit", {"file_path": self.path("lib/app.dart")}))
        # the plugin's own scripts are part of the guard's setup too
        own = os.path.join(self.plugin, "guardrails", "guard.py")
        self.assertEqual(self.protect("Edit", {"file_path": own}), "ask")

    def test_plugin_agent_names_map_to_roles(self):
        self.init("--no-git-hooks")
        decision = self.protect("Edit", {"file_path": self.path("lib/app.dart")},
                                agent_id="a1", agent_type="code-guardrails:test-writer")
        self.assertEqual(decision, "deny")
        self.assertIsNone(self.protect("Edit", {"file_path": self.path("test/app_test.dart")},
                                       agent_id="a1", agent_type="code-guardrails:test-writer"))

    def test_gates_get_the_project_root_and_the_scripts_folder(self):
        self.write("guardrails.json", json.dumps({"gates": [
            {"name": "env", "on": ["lib/**"], "timeout": 30,
             "run": '[ "$GUARDRAILS_ROOT" = "$(pwd -P)" ] && [ -f "$GUARDRAILS_BIN/shield.py" ] || '
                    '{ echo "GUARDRAILS_ROOT=$GUARDRAILS_ROOT GUARDRAILS_BIN=$GUARDRAILS_BIN"; exit 1; }'},
            {"name": "red", "on": ["red/**"], "run": "echo red test; exit 1", "timeout": 30}]}))
        self.hook("UserPromptSubmit", "guard.py", {})
        self.write("lib/app.dart", "class App {}\n")
        self.assertIsNone(self.hook("Stop", "guard.py", {}))
        # control: a failing gate does block, so the gate above really ran
        self.hook("UserPromptSubmit", "guard.py", {})
        self.write("red/x.txt", "x")
        out = self.hook("Stop", "guard.py", {})
        self.assertEqual((out or {}).get("decision"), "block")

    # --- deletion guard ----------------------------------------------------------

    def test_deletion_guard_works_and_points_to_the_plugin_scripts(self):
        self.init("--no-git-hooks")
        self.write("test/money_test.dart", "void main() {\n  test('adds', () {\n    expect(1, 1);\n  });\n}\n")
        self.hook("UserPromptSubmit", "deletion_guard.py", {})
        self.write("test/money_test.dart", "void main() {\n}\n")  # deleted outside the edit tools
        out = self.hook("Stop", "deletion_guard.py", {})
        self.assertEqual((out or {}).get("decision"), "block")
        self.assertIn(os.path.join(self.plugin, "guardrails", "deletion_guard.py"), out["reason"])
        r = self.bin("guardrails-deletion", "history")
        self.assertIn("version", r.stdout)

    # --- code shield ------------------------------------------------------------

    def test_shield_commit_hook_finds_the_plugin(self):
        self.init()  # with git hooks
        self.write("app/fees.py", "def fee(amount):\n    # SHIELD: fee-rule\n    return round(amount * 0.02, 2)\n"
                                  "    # SHIELD-END: fee-rule\n")
        self.write("app/test_fees.py", "import unittest\nfrom fees import fee\n\nclass FeeRule(unittest.TestCase):\n"
                                       "    def test_two_percent(self):\n        self.assertEqual(fee(100), 2.0)\n")
        with open(self.path("shield.json"), encoding="utf-8") as f:
            locks = json.load(f)
        locks["runners"] = {"unit": {"run": sys.executable + " -m unittest {tests}", "cwd": "app", "timeout": 60}}
        locks["locks"] = [{"name": "fee-rule", "file": "app/fees.py", "region": "fee-rule", "reason": "money",
                           "tests": {"unit": ["test_fees.FeeRule"]}, "fingerprint": ""}]
        self.write("shield.json", json.dumps(locks, indent=2))
        r = self.bin("guardrails-shield", "approve", "fee-rule", "--reason", "initial lock of the fee rule")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.plugin, "shield.json"), encoding="utf-8") as f:
            self.assertNotIn("fee-rule", f.read(), "the plugin's own shield.json was changed")
        self.git("add", "-A")
        self.assertEqual(self.git("commit", "-q", "-m", "lock", check=False).returncode, 0)
        self.write("app/fees.py", "def fee(amount):\n    # SHIELD: fee-rule\n    return round(amount * 0.03, 2)\n"
                                  "    # SHIELD-END: fee-rule\n")
        self.git("add", "-A")
        r = self.git("commit", "-q", "-m", "change the rule", check=False)
        self.assertNotEqual(r.returncode, 0, "an unapproved lock change was committed")
        self.assertIn("CHANGED", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
