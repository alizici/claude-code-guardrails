"""Where the project is, and how to call the guardrails scripts from it.

The scripts run from one of two places:

  copied     guardrails/ was copied into the project (the original setup). The project is the
             folder that contains guardrails/, exactly as before.
  plugin     the scripts live in Claude Code's plugin folder (installed with /plugin install). The
             project is the nearest folder with a guardrails.json, looked up from the folder Claude
             Code runs in (CLAUDE_PROJECT_DIR), then from the working directory. No guardrails.json:
             no project, and every hook does nothing. guardrails.json is the per-project opt-in.

GUARDRAILS_ROOT overrides both (tests, git hooks).
"""

import os
import subprocess

CONFIG = "guardrails.json"
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(SCRIPTS)
# A plugin package has a manifest next to the scripts; a copy in a project doesn't.
PLUGIN_MANIFEST = os.path.join(PACKAGE_ROOT, ".claude-plugin", "plugin.json")
# Where the git hooks find shield.py when the scripts aren't copied into the project (plugin install).
SHIELD_POINTER = "guardrails-shield-path"


def is_plugin():
    return os.path.isfile(PLUGIN_MANIFEST)


def find_up(start):
    """The nearest folder at or above `start` that has a guardrails.json; None if there is none."""
    if not start or not os.path.isdir(start):
        return None
    d = os.path.realpath(start)
    while True:
        if os.path.isfile(os.path.join(d, CONFIG)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def project_root(cwd=None):
    """The project root, or None when there is no project to guard (plugin install, no guardrails.json)."""
    explicit = os.environ.get("GUARDRAILS_ROOT")
    if explicit:
        return os.path.realpath(explicit)
    if not is_plugin():
        return os.path.realpath(PACKAGE_ROOT)
    for start in (os.environ.get("CLAUDE_PROJECT_DIR"), cwd, os.getcwd()):
        found = find_up(start)
        if found:
            return found
    return None


def yields_to_copy(root):
    """Installed as a plugin, in a project that also has its own copy of guardrails/ wired into its
    .claude settings: the copy's hooks do the work. Running both would check every tool call twice."""
    copy = os.path.join(root, "guardrails")
    if not is_plugin() or os.path.realpath(copy) == os.path.realpath(SCRIPTS):
        return False
    if not os.path.isfile(os.path.join(copy, "guard.py")):
        return False
    for name in ("settings.json", "settings.local.json"):
        try:
            with open(os.path.join(root, ".claude", name), encoding="utf-8") as f:
                if "guardrails/guard.py" in f.read():
                    return True
        except OSError:
            pass
    return False


def command(script, root):
    """How to run one of the scripts from the project root, for messages the agent will act on:
    `python3 guardrails/shield.py` when copied into the project, an absolute path when installed as a plugin."""
    path = os.path.join(SCRIPTS, script)
    if root:
        rel = os.path.relpath(path, root)
        if not rel.startswith(".."):
            return "python3 " + rel.replace(os.sep, "/")
    return 'python3 "%s"' % path


def git_common_dir(root):
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--git-common-dir"], capture_output=True,
                           text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    d = r.stdout.strip()
    return os.path.realpath(d if os.path.isabs(d) else os.path.join(root, d))


def write_shield_pointer(root):
    """Plugin install: tell the git hooks where shield.py is. The plugin folder changes with every update,
    so this is refreshed on every prompt; the hooks read it at commit time."""
    if not root or not is_plugin() or os.path.isfile(os.path.join(root, "guardrails", "shield.py")):
        return
    common = git_common_dir(root)
    if not common:
        return
    path = os.path.join(common, SHIELD_POINTER)
    target = os.path.join(SCRIPTS, "shield.py")
    try:
        with open(path, encoding="utf-8") as f:
            if f.read().strip() == target:
                return
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(target + "\n")
    except OSError:
        pass
