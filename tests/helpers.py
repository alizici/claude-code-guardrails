import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(REPO, "guardrails", "guard.py")
SHIELD = os.path.join(REPO, "guardrails", "shield.py")
DELETION = os.path.join(REPO, "guardrails", "deletion_guard.py")


class Project:
    """A throwaway git project with the guardrails installed (copied, so the real repo is never touched)."""

    def __init__(self, config=None, shield=None):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="guardrails_test_"))
        shutil.copytree(os.path.join(REPO, "guardrails"), os.path.join(self.root, "guardrails"))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        if config is not None:
            self.write("guardrails.json", json.dumps(config, indent=2))
        if shield is not None:
            self.write("shield.json", json.dumps(shield, indent=2))

    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, text):
        os.makedirs(os.path.dirname(self.path(rel)) or self.root, exist_ok=True)
        with open(self.path(rel), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as f:
            return f.read()

    def git(self, *args, check=True, stdin=None):
        return subprocess.run(["git", "-C", self.root, *args], capture_output=True, text=True,
                              check=check, input=stdin)

    def commit_all(self, message="commit"):
        self.git("add", "-A")
        return self.git("commit", "-q", "-m", message, check=False)

    def run(self, script, *args, event=None):
        env = {**os.environ, "GUARDRAILS_ROOT": self.root}
        r = subprocess.run([sys.executable, os.path.join(self.root, "guardrails", os.path.basename(script)), *args],
                           input=json.dumps(event) if event is not None else None, capture_output=True,
                           text=True, cwd=self.root, env=env)
        return r

    def hook(self, script, sub, event):
        r = self.run(script, sub, event=event)
        out = r.stdout.strip()
        return r.returncode, (json.loads(out) if out else None)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)
