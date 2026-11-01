#!/usr/bin/env python3
"""Sets up the guardrails in a project: a starter guardrails.json and shield.json for the project's stack,
then the code shield's git hooks. Existing files are never overwritten.

    python3 init.py [--root DIR] [--no-git-hooks]

The root is the git repository around the working directory (or the working directory). Stacks are
detected from their manifest files: Flutter/Dart (pubspec.yaml), Python (pyproject.toml, setup.py,
requirements.txt), Node (package.json with a test script), Swift (Package.swift). Each one adds its
generated-file patterns, a test gate and test/production paths for the role agents. Review the result:
the gates are a starting point, not a guess about your CI.
"""

import argparse
import json
import os
import subprocess
import sys

sys.dont_write_bytecode = True
import _paths  # noqa: E402  (next to this script)

BLOCK = [
    {
        "pattern": "\\bgit\\s+push\\b[^;&|\\n]*(?:--force(?![-\\w])|\\s-f\\b)",
        "reason": "Force push is closed to the agent (history isn't rewritten); ask the user if it's really needed.",
        "examples": {
            "match": ["git push --force origin main", "git push -f origin main", "git push origin main --force"],
            "no_match": ["git push origin main --force-with-lease", "git push -q origin main && git worktree remove --force wt"],
        },
    },
    {
        "pattern": "\\bgit\\s+commit\\b[^;&|\\n]*--no-verify\\b",
        "reason": "--no-verify skips the code shield's commit hook; locked code can't be committed without approval.",
        "examples": {
            "match": ["git commit --no-verify -m x", "git commit -q -m x --no-verify"],
            "no_match": ["git commit -m x && git push origin main --no-verify"],
        },
    },
    {
        "pattern": "\\bgit\\s+(?:reset\\s+--hard|clean\\s+-[a-zA-Z]*f|checkout\\s+--\\s+\\.)",
        "reason": "This throws away uncommitted work. Ask the user first.",
        "examples": {
            "match": ["git reset --hard HEAD~1", "git clean -fd", "git checkout -- ."],
            "no_match": ["git reset --soft HEAD~1", "git checkout -- src/a.py"],
        },
    },
]

ASK = [
    {
        "pattern": "(?:shield\\.py|guardrails-shield)\\b.*\\bapprove\\b",
        "reason": "Approving a change to locked critical code is the user's decision: check the reason and that the lock tests passed.",
        "examples": {
            "match": ["python3 guardrails/shield.py approve discount-rule --reason \"x\"",
                      "guardrails-shield approve discount-rule --reason \"x\""],
            "no_match": ["python3 guardrails/shield.py test discount-rule", "grep approve guardrails/shield.py",
                         "guardrails-shield test discount-rule"],
        },
    }
]


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def exists(root, *names):
    return any(os.path.isfile(os.path.join(root, n)) for n in names)


def detect(root):
    """[(stack name, settings)] for every stack found at the project root."""
    stacks = []
    if exists(root, "pubspec.yaml"):
        try:
            with open(os.path.join(root, "pubspec.yaml"), encoding="utf-8") as f:
                flutter = "sdk: flutter" in f.read()
        except OSError:
            flutter = False
        runner = "flutter test" if flutter else "dart test"
        stacks.append(("Flutter/Dart" if flutter else "Dart", {
            "generated": ["**/*.g.dart", "**/*.freezed.dart", "**/*.mocks.dart", "**/*.gr.dart",
                          "**/*.config.dart", "**/l10n/generated/**"],
            "regenerate": "dart run build_runner build --delete-conflicting-outputs",
            "gate": {"name": "dart tests", "on": ["lib/**", "test/**", "pubspec.yaml"], "run": runner + " --reporter compact",
                     "timeout": 600},
            "tests": ["test/**", "integration_test/**", "**/*_test.dart"],
            "code": ["lib/**", "bin/**"],
            "runner": ("dart", {"run": runner + " {tests}", "cwd": ".", "timeout": 600}),
        }))
    if exists(root, "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"):
        stacks.append(("Python", {
            "generated": ["**/*_pb2.py", "**/*_pb2_grpc.py"],
            "regenerate": "",
            "gate": {"name": "python tests", "on": ["**/*.py", "pyproject.toml"], "run": "python3 -m pytest -q",
                     "timeout": 600},
            "tests": ["tests/**", "**/tests/**", "**/test_*.py", "**/*_test.py"],
            "code": ["src/**", "**/src/**"],
            "runner": ("pytest", {"run": "python3 -m pytest -q {tests}", "cwd": ".", "timeout": 600}),
        }))
    pkg = read_json(os.path.join(root, "package.json")) if exists(root, "package.json") else None
    if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) and pkg["scripts"].get("test"):
        stacks.append(("Node", {
            "generated": [],
            "regenerate": "",
            "gate": {"name": "npm test", "on": ["src/**", "lib/**", "test/**", "tests/**", "package.json"],
                     "run": "npm test --silent", "timeout": 600},
            "tests": ["test/**", "tests/**", "**/__tests__/**", "**/*.test.*", "**/*.spec.*"],
            "code": ["src/**", "lib/**"],
            "runner": ("npm", {"run": "npm test --silent -- {tests}", "cwd": ".", "timeout": 600}),
        }))
    if exists(root, "Package.swift"):
        stacks.append(("Swift", {
            "generated": [],
            "regenerate": "",
            "gate": {"name": "swift tests", "on": ["Sources/**", "Tests/**", "Package.swift"], "run": "swift test",
                     "timeout": 900},
            "tests": ["Tests/**"],
            "code": ["Sources/**"],
            "runner": ("swift", {"run": "swift test --filter {tests}", "cwd": ".", "timeout": 900}),
        }))
    return stacks


def unique(items):
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def guard_config(stacks):
    generated = unique([g for _, s in stacks for g in s["generated"]] + ["docs/SHIELD_LOG.md"])
    regenerate = " · ".join([s["regenerate"] for _, s in stacks if s["regenerate"]] + ["shield.py approve (shield log)"])
    tests = unique([t for _, s in stacks for t in s["tests"]]) or ["tests/**", "**/tests/**", "**/test/**", "**/*_test.*",
                                                                    "**/test_*.py"]
    code = unique([c for _, s in stacks for c in s["code"]]) or ["src/**", "**/src/**", "lib/**", "**/lib/**"]
    gates = [dict(s["gate"]) for _, s in stacks]
    gates.append({"name": "code shield", "on": ["**"], "run": "python3 \"$GUARDRAILS_BIN/shield.py\" gate", "timeout": 600})
    return {
        "_info": [
            "Guard config for the code-guardrails plugin. Keys starting with '_' are ignored.",
            "Detected: %s. The gates and paths are a starting point: check them against your project." % (
                ", ".join(n for n, _ in stacks) or "no known stack"),
            "Validate after every change with the plugin's check command (last line must be 'ok').",
            "Gate exit 0 = pass; 3, 126, 127, missing tool and timeout = warning only; anything else = failing (the turn can't end).",
            "$GUARDRAILS_BIN in a gate command is the folder of the guardrails scripts, wherever the plugin is installed.",
        ],
        "generated": {"paths": generated, "regenerate": regenerate},
        "block": BLOCK,
        "ask": ASK,
        "ask_paths": [
            {"glob": "shield.json", "reason": "The lock list: loosening it or editing a fingerprint by hand is the user's decision."},
            {"glob": ".claude/agents/**", "reason": "Role definitions: changing what a role may do is the user's decision."},
        ],
        "roles": {
            "card-auditor": {"write": [], "note": "Read-only: report in the final message."},
            "test-writer": {"write": tests, "note": "The test writer only writes tests. Production code is the implementer's job."},
            "implementer": {"write": code + ["docs/OPEN_QUESTIONS.md"],
                            "note": "The implementer doesn't touch tests. If a test looks wrong, stop and report it."},
            "reviewer": {"write": [], "note": "Read-only: the reviewer judges the work, it doesn't fix it."},
        },
        "gates": gates,
        "deletion_guard": {"rewrite": "deny", "important_deletion": "deny"},
    }


def shield_config(stacks):
    return {
        "_info": [
            "CODE SHIELD lock list. Each lock: a file (and optionally a region between '// SHIELD: <name>' and "
            "'// SHIELD-END: <name>' in any comment style), a reason, the tests that protect it and the approved fingerprint.",
            "The fingerprint is never written by hand: 'shield.py approve <name> --reason \"...\"' writes it and logs the change "
            "to docs/SHIELD_LOG.md. New lock: add it here with an empty fingerprint, mark the region, then approve.",
            "{tests} in a runner command is replaced by the lock's test names.",
        ],
        "runners": {name: cfg for name, cfg in (s["runner"] for _, s in stacks)},
        "locks": [],
    }


def write_new(path, data):
    if os.path.exists(path):
        print(f"kept       : {path} (already exists, not changed)")
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"created    : {path}")
    return True


def default_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.getcwd()


def main():
    ap = argparse.ArgumentParser(description="Set up the guardrails in a project.")
    ap.add_argument("--root", help="project root (default: the git repository around the working directory)")
    ap.add_argument("--no-git-hooks", action="store_true", help="don't install the code shield's git hooks")
    a = ap.parse_args()
    root = os.path.realpath(a.root or default_root())
    if os.path.realpath(_paths.PACKAGE_ROOT) == root:
        print("this is the guardrails package itself, not a project to set up")
        return 1
    stacks = detect(root)
    print(f"project    : {root}")
    print(f"stack      : {', '.join(n for n, _ in stacks) or 'none detected (generic config)'}")
    write_new(os.path.join(root, "guardrails.json"), guard_config(stacks))
    write_new(os.path.join(root, "shield.json"), shield_config(stacks))
    is_git = subprocess.run(["git", "-C", root, "rev-parse", "--git-dir"], capture_output=True).returncode == 0
    if not is_git:
        print("git        : not a git repository: turn snapshots, the deletion guard and the shield's commit hooks need git")
    elif a.no_git_hooks:
        print("git hooks  : skipped (--no-git-hooks)")
    else:
        r = subprocess.run(["sh", os.path.join(_paths.SCRIPTS, "install_git_hooks.sh")], cwd=root,
                           capture_output=True, text=True)
        for line in (r.stdout + r.stderr).strip().splitlines():
            print(f"git hooks  : {line}")
        if r.returncode != 0:
            return 1
    print("next       : review guardrails.json (gates, generated paths, roles), then run the check command")
    return 0


if __name__ == "__main__":
    sys.exit(main())
