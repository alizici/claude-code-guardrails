#!/usr/bin/env python3
"""Deletion guard: line-level protection against accidental deletions, plus a dated, numbered
version of the working tree on every turn.

The problem it solves: an AI agent "cleans up" and one important line disappears (a guard clause,
a test, a function). Nobody notices until something breaks. No stamps are written into files
(the agent forgets them, they create noise and conflicts); a version is a snapshot of the working
tree at the start and end of each turn, stored as git refs that never touch your branches.

Events (.claude/settings.json):
  turn    UserPromptSubmit        Snapshot of the working tree: version N with a date, under
                                  refs/guardrails/<tree>/<N>. Never touches the branch, HEAD or
                                  index (uses a temporary index). No change, no new version.
  before  PreToolUse Edit|Write|MultiEdit
                                  Computes exactly which lines the edit deletes. Rewriting an existing
                                  file with Write while dropping lines, and deleting important lines
                                  without declaring it first, are denied.
  after   PostToolUse Edit|Write|MultiEdit
                                  Shows the agent every line the edit deleted, so an accident is
                                  seen right away.
  gate    Stop                    Diffs against the start-of-turn snapshot (so deletions done via Bash
                                  or scripts count too). Undeclared important lines lost: the turn
                                  can't end. Otherwise a short summary for the user.
Manual:
  allow <path> [--line TEXT ...] [--all] --reason TEXT   declare an intentional deletion (valid until
                                                         the next user prompt)
  history [path]            versions: number, date, label (with a path: only versions where it changed)
  show <N> <path>           the file as it was in version N
  restore <N> <path>        put the file back to version N (a new snapshot is taken first)
  selftest                  end-to-end test in a temporary repository; last line is 'ok'

Important line: class, function, getter, test, guard (throw, assert, raise), YAML/JSON key, and any
non-empty line in files matching `all_lines_important` (docs, translations). A line replaced by a
similar line counts as "changed", not deleted; text that still exists elsewhere in the file or in
the turn counts as "moved".

A hook is a guardrail, not a security boundary. No infrastructure problem ever blocks a tool or a
turn; the script never exits with 2.
"""

import datetime
import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
import _paths  # noqa: E402  (next to this script)

CONFIG_FILE = _paths.CONFIG
DEFAULTS = {
    "watched": ["**/*.dart", "**/*.py", "**/*.swift", "**/*.kt", "**/*.kts", "**/*.ts", "**/*.tsx",
                "**/*.js", "**/*.jsx", "**/*.go", "**/*.rs", "**/*.java", "**/*.sh", "**/*.md",
                "**/*.arb", "**/*.yaml", "**/*.yml", "**/*.json"],
    "excluded": ["**/*.g.dart", "**/*.freezed.dart", "**/*.mocks.dart", "**/pubspec.lock",
                 "**/package-lock.json", "**/Package.resolved", "**/build/**", "**/.dart_tool/**",
                 "**/node_modules/**"],
    "all_lines_important": ["**/*.md", "**/*.arb"],
    # Deleting these exact lines is never "important" (e.g. a stub body an implementer replaces).
    "allowed_deletions": ["throw UnimplementedError();", "raise NotImplementedError", "fatalError(\"TODO\")"],
    "rewrite": "deny",
    "important_deletion": "deny",
    "similarity": 0.6,
    "max_blocks": 2,
    "summary_lines": 15,
}
IDENTITY = {"GIT_AUTHOR_NAME": "deletion-guard", "GIT_AUTHOR_EMAIL": "deletion-guard@local",
            "GIT_COMMITTER_NAME": "deletion-guard", "GIT_COMMITTER_EMAIL": "deletion-guard@local"}
ALLOW_TTL = 6 * 3600  # s; upper bound. The real limit is the session's turn start: a new prompt closes earlier declarations

# Line classifiers. Heuristic on purpose: they flag lines whose loss usually breaks behavior.
TYPE_DECL = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:export|default|public|private|protected|internal|open|"
                       r"abstract|sealed|base|final|interface|mixin|static|data|enum)\s+)*"
                       r"(?:class|mixin|enum|extension(?:\s+type)?|typedef|interface|struct|protocol|trait|type)\s+\w")
TEST_CALL = re.compile(r"^\s*(?:test|testWidgets|group|blocTest|goldenTest|it|describe|func\s+test\w*)\s*(?:<[^>]*>)?\s*\(")
GETTER = re.compile(r"^\s*(?:static\s+)?(?:[\w<>?,\[\]. ]+\s+)?(?:get|set)\s+[A-Za-z_$][\w$]*\s*(?:\(|=>|\{|$)")
GUARD = re.compile(r"\bthrow\b|\bassert\s*\(|\brethrow\b|\braise\b|\bguard\b.*\belse\b|\bprecondition\s*\(")
CONTROL = {"if", "for", "while", "switch", "catch", "return", "await", "else", "do", "assert", "super", "this",
           "throw", "print", "new", "yield", "on", "try", "finally", "case", "var", "final", "const", "let"}
FUNC = re.compile(r"^\s*(?:(?:export|async|static|external|factory|const|late|covariant|abstract|public|private|"
                  r"protected|override)\s+)*(?:function\s+)?(?:[\w$<>?,\[\]. ]+?\s+)?([A-Za-z_$][\w$]*(?:\.[\w$]+)?)"
                  r"\s*(?:<[^()]*>)?\s*\(")
PY_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+\w+")
SWIFT_KT = re.compile(r"^\s*(?:@\w+\s+)*(?:(?:public|private|internal|open|fileprivate|override|static|final|data|"
                      r"sealed|abstract|suspend|inline|mutating)\s+)*(?:func|fun|init|deinit)\b")
SH_FUNC = re.compile(r"^\s*(?:function\s+)?[\w-]+\s*\(\)\s*\{")
YAML_KEY = re.compile(r"^\s*(?:-\s+)?[\"']?[\w./@-]+[\"']?\s*:(?:\s|$)")
JSON_KEY = re.compile(r'^\s*"[^"]+"\s*:')
C_LIKE = (".dart", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".kts", ".swift", ".go", ".rs")

ROOT = STATE = REF_PREFIX = None


# ---------------------------------------------------------------- setup

def setup(root):
    """Sets the root and state folder; False if it isn't a git repository."""
    global ROOT, STATE, REF_PREFIX
    ROOT = os.path.realpath(root)
    r = subprocess.run(["git", "-C", ROOT, "rev-parse", "--absolute-git-dir", "--git-common-dir"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    git_dir, common = r.stdout.split("\n")[:2]
    common = os.path.realpath(common if os.path.isabs(common) else os.path.join(ROOT, common))
    STATE = os.path.join(git_dir, "deletion-guard")
    os.makedirs(STATE, exist_ok=True)
    key = "main" if os.path.realpath(git_dir) == common else os.path.basename(git_dir)
    REF_PREFIX = "refs/guardrails/%s" % re.sub(r"[^\w.-]", "_", key)
    return True


def git(*args, env=None, raw=False):
    e = dict(os.environ, **(env or {}))
    r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, env=e, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args[:2]), r.stderr.decode("utf-8", "replace").strip()[:300]))
    return r.stdout if raw else r.stdout.decode("utf-8", "replace").strip()


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(ROOT, CONFIG_FILE), encoding="utf-8") as f:
            raw = json.load(f)
        cfg.update({k: v for k, v in (raw.get("deletion_guard") or {}).items() if not k.startswith("_")})
        # files regenerated by tools are not deletions; the list lives in the guard config (single source)
        paths = (raw.get("generated") or {}).get("paths") or []
        cfg["excluded"] = list(cfg["excluded"]) + [p for p in paths if isinstance(p, str) and p]
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


def read_state():
    try:
        with open(os.path.join(STATE, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(d):
    path = os.path.join(STATE, "state.json")
    tmp = path + ".%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def log(text):
    try:
        with open(os.path.join(STATE, "log.txt"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), text))
    except OSError:
        pass


def glob_match(rel, patterns):
    for p in patterns:
        if fnmatch.fnmatch(rel, p) or (p.startswith("**/") and fnmatch.fnmatch(rel, p[3:])):
            return True
    return False


def relative(path):
    if not path:
        return None
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(ROOT, path))
    if not (full == ROOT or full.startswith(ROOT + os.sep)):
        return None
    return os.path.relpath(full, ROOT).replace(os.sep, "/")


def watched(rel, cfg):
    return bool(rel) and not rel.startswith(".git/") and glob_match(rel, cfg["watched"]) and not glob_match(rel, cfg["excluded"])


def is_text(data):
    return b"\0" not in data[:8192]


def read_file(full):
    try:
        with open(full, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return data.decode("utf-8", "replace") if is_text(data) else None


def now():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


# ---------------------------------------------------------------- versions

def snapshot(label, session=""):
    """Snapshot of the working tree (temporary index; .gitignore respected). No change: the last version."""
    d = read_state()
    env = {"GIT_INDEX_FILE": os.path.join(STATE, "snapshot.index")}
    git("add", "-A", "--", ".", env=env)
    tree = git("write-tree", env=env)
    last = d.get("last") or {}
    if last.get("tree") == tree and last.get("commit"):
        return last
    n = int(d.get("counter", 0)) + 1
    when = now()
    changed = (git("diff-tree", "-r", "--name-status", "--no-renames", last["tree"], tree)
               if last.get("tree") else "(first snapshot)")
    message = "version %d · %s · %s%s\n\n%s" % (n, when, label, " · session %s" % session[:8] if session else "",
                                                changed[:20000])
    args = ["commit-tree", tree, "-m", message] + (["-p", last["commit"]] if last.get("commit") else [])
    commit = git(*args, env=IDENTITY)
    git("update-ref", "%s/%06d" % (REF_PREFIX, n), commit)
    d = read_state()  # another call may have written in between
    d["counter"] = max(n, int(d.get("counter", 0)))
    d["last"] = {"tree": tree, "commit": commit, "version": n, "time": when}
    write_state(d)
    return d["last"]


def version_commit(n):
    return git("rev-parse", "--verify", "%s/%06d^{commit}" % (REF_PREFIX, int(n)))


def content(commit, rel):
    try:
        data = git("cat-file", "-p", "%s:%s" % (commit, rel), raw=True)
    except RuntimeError:
        return None
    return data.decode("utf-8", "replace") if is_text(data) else None


# ---------------------------------------------------------------- line analysis

def is_important(line, rel, cfg):
    s = line.strip()
    if not s:
        return False
    if s in cfg["allowed_deletions"]:
        return False
    if glob_match(rel, cfg["all_lines_important"]):
        return bool(re.search(r"\w", s))
    ext = os.path.splitext(rel)[1]
    if ext in C_LIKE:
        if s.startswith(("//", "*", "/*")):
            return False
        if TYPE_DECL.match(line) or TEST_CALL.match(line) or GETTER.match(line) or GUARD.search(s):
            return True
        if ext in (".swift", ".kt", ".kts") and SWIFT_KT.match(line):
            return True
        m = FUNC.match(line)
        if m and m.group(1).split(".")[0] not in CONTROL:
            before = line[:m.start(1)]
            if "=" not in before and not s.endswith(";"):
                return True
        return False
    if ext == ".py":
        return bool(PY_DEF.match(line)) or bool(re.search(r"\braise\b|\bassert\b", s)) or bool(
            re.match(r"^\s*def\s+test_", line))
    if ext in (".yaml", ".yml"):
        return not s.startswith("#") and bool(YAML_KEY.match(line))
    if ext in (".json", ".arb"):
        return bool(JSON_KEY.match(line))
    if ext == ".sh":
        return bool(SH_FUNC.match(line))
    return False


def deleted_lines(old, new, rel, cfg, moved=None):
    """[(old line no, text, important)]. Lines replaced by a similar line, or moved, don't count."""
    a, b = old.splitlines(), new.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=len(a) * len(b) > 25_000_000)
    candidates, added = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "delete":
            candidates += [(i, a[i]) for i in range(i1, i2)]
        elif op == "insert":
            added += b[j1:j2]
        elif op == "replace":
            added += b[j1:j2]
            paired = set()
            if (i2 - i1) * (j2 - j1) <= 250_000:
                # The most similar pair matches first. Matching in order let one line grab its
                # neighbour's replacement and leave the neighbour "deleted".
                threshold, pairs = cfg["similarity"], []
                for i in range(i1, i2):
                    for j in range(j1, j2):
                        q = difflib.SequenceMatcher(None, a[i].strip(), b[j].strip())
                        if q.real_quick_ratio() >= threshold and q.quick_ratio() >= threshold:
                            ratio = q.ratio()
                            if ratio >= threshold:
                                pairs.append((-ratio, abs((i - i1) - (j - j1)), i, j))
                taken = set()
                for _, _, i, j in sorted(pairs):
                    if i not in paired and j not in taken:
                        paired.add(i)
                        taken.add(j)
            candidates += [(i, a[i]) for i in range(i1, i2) if i not in paired]
    remaining = {}
    for t in added:
        remaining[t.strip()] = remaining.get(t.strip(), 0) + 1
    for t, count in (moved or {}).items():
        remaining[t] = remaining.get(t, 0) + count
    out = []
    for i, t in candidates:
        k = t.strip()
        if not re.search(r"\w", k):
            continue  # empty or punctuation only: the compiler catches it
        if remaining.get(k):
            remaining[k] -= 1
            continue  # moved
        out.append((i + 1, t, is_important(t, rel, cfg)))
    return out


def declarations(rel=None, session=None):
    """Declarations since the session's turn start."""
    d = read_state()
    s = (d.get("sessions") or {}).get(str(session or "")) or {}
    limit = max(float(d.get("allow_since", 0)), float(s.get("allow_since", 0)), time.time() - ALLOW_TTL)
    out = []
    try:
        with open(os.path.join(STATE, "allowed.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    k = json.loads(line)
                except ValueError:
                    continue
                if k.get("at", 0) > limit and (rel is None or k.get("path") == rel):
                    out.append(k)
    except OSError:
        pass
    return out


def declared(text, rel, items):
    s = text.strip()
    for k in items:
        if k.get("path") != rel:
            continue
        if k.get("all"):
            return True
        if any(len(p.strip()) >= 3 and p.strip() in s for p in k.get("lines") or []):
            return True
    return False


def line_list(records, limit):
    lines = ["  %s:%d  %s" % (rel, no, (t.strip()[:150] + ("…" if len(t.strip()) > 150 else "")))
             for rel, no, t in records[:limit]]
    if len(records) > limit:
        lines.append("  … and %d more lines" % (len(records) - limit))
    return "\n".join(lines)


def cli():
    """This script as the agent should call it from the project root."""
    return _paths.command("deletion_guard.py", ROOT)


def allow_command(rel, sample):
    return ('%s allow %s --line "%s" --reason "<why>"'
            % (cli(), rel, sample.strip()[:40].replace('"', '\\"')))


# ---------------------------------------------------------------- events

def simulate(tool, ti, old):
    if tool == "Write":
        return ti.get("content") if isinstance(ti.get("content"), str) else None
    edits = ti.get("edits") if tool == "MultiEdit" else [ti]
    text = old
    for e in edits or []:
        o, n = e.get("old_string"), e.get("new_string")
        if not isinstance(o, str) or not isinstance(n, str) or not o or o not in text:
            return None  # the tool will fail anyway
        text = text.replace(o, n) if e.get("replace_all") else text.replace(o, n, 1)
    return text


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def on_before(event):
    tool, ti = event.get("tool_name"), event.get("tool_input") or {}
    if tool not in ("Edit", "Write", "MultiEdit"):
        return None
    cfg = load_config()
    rel = relative(ti.get("file_path"))
    if not watched(rel, cfg):
        return None
    full = os.path.join(ROOT, rel)
    if not os.path.isfile(full):
        return None
    old = read_file(full)
    if old is None:
        return None
    new = simulate(tool, ti, old)
    if new is None:
        return None
    dels = deleted_lines(old, new, rel, cfg)
    pending = os.path.join(STATE, "pending")
    os.makedirs(pending, exist_ok=True)
    key = re.sub(r"[^\w-]", "_", str(event.get("tool_use_id") or rel))
    with open(os.path.join(pending, key + ".json"), "w", encoding="utf-8") as f:
        json.dump({"path": rel, "deleted": dels, "at": time.time()}, f, ensure_ascii=False)
    if not dels:
        return None
    items = declarations(rel, event.get("session_id"))
    undeclared = [(rel, no, t) for no, t, _ in dels if not declared(t, rel, items)]
    if tool == "Write" and undeclared and cfg["rewrite"] == "deny":
        return deny("Deletion guard: rewriting the existing file %s with Write deletes %d lines:\n%s\n"
                    "Make the change with Edit. If you really are rewriting the file from scratch, declare it first:\n"
                    '  %s allow %s --all --reason "<why>"'
                    % (rel, len(undeclared), line_list(undeclared, cfg["summary_lines"]), cli(), rel))
    important = [(rel, no, t) for no, t, imp in dels if imp and not declared(t, rel, items)]
    if important and cfg["important_deletion"] == "deny":
        return deny("Deletion guard: this edit deletes %d important lines without a declaration:\n%s\n"
                    "If this is a mistake, fix old_string/new_string. If it's intentional, declare it first "
                    "(it's reported to the user at the end of the turn):\n  %s\n(--all for every deletion in the file)"
                    % (len(important), line_list(important, cfg["summary_lines"]), allow_command(rel, important[0][2])))
    return None


def on_after(event):
    ti = event.get("tool_input") or {}
    rel = relative(ti.get("file_path"))
    key = re.sub(r"[^\w-]", "_", str(event.get("tool_use_id") or rel))
    path = os.path.join(STATE, "pending", key + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        os.remove(path)
    except (OSError, ValueError):
        return None
    dels = rec.get("deleted") or []
    if not dels:
        return None
    cfg = load_config()
    lines = [(rec["path"], no, t) for no, t, _ in dels]
    important = sum(1 for _, _, imp in dels if imp)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
            "Deletion guard: this edit deleted %d lines (%d important, declared):\n%s\n"
            "If any of these went by mistake, put it back now."
            % (len(dels), important, line_list(lines, cfg["summary_lines"]))}}


def on_turn(event):
    sid = str(event.get("session_id") or "")
    started = time.time()  # new prompt: the previous turn is closed, so are its declarations
    s = snapshot("turn start", sid)
    d = read_state()
    sessions = {k: v for k, v in (d.get("sessions") or {}).items() if v.get("at", 0) > time.time() - 7 * 86400}
    sessions[sid] = {"turn_start": s["commit"], "turn_start_version": s["version"], "at": time.time(),
                     "block": None, "allow_since": started}
    d["sessions"] = sessions
    write_state(d)
    pending = os.path.join(STATE, "pending")
    for name in os.listdir(pending) if os.path.isdir(pending) else []:
        p = os.path.join(pending, name)
        if os.path.getmtime(p) < time.time() - 86400:
            os.remove(p)
    return None


def turn_analysis(start, end, cfg):
    """Between two versions: {rel: [(no, text, important)]}. Moving lines between files isn't a deletion."""
    lines = git("diff-tree", "-r", "--name-status", "--no-renames", start, end).splitlines()
    changed, added_text = [], {}
    for line in lines:
        status, _, rel = line.partition("\t")
        if not watched(rel, cfg):
            continue
        if status.startswith("A"):
            for t in (content(end, rel) or "").splitlines():
                added_text[t.strip()] = added_text.get(t.strip(), 0) + 1
        elif status[:1] in ("M", "D", "T"):
            changed.append((status[:1], rel))
    # first collect every file's own additions, so a line moved to another file isn't a deletion
    pairs = []
    for status, rel in changed:
        old = content(start, rel)
        if old is None:
            continue
        new = "" if status == "D" else (content(end, rel) or "")
        pairs.append((rel, old, new))
        old_count = {}
        for t in old.splitlines():
            old_count[t.strip()] = old_count.get(t.strip(), 0) + 1
        for t in new.splitlines():
            k = t.strip()
            if old_count.get(k):
                old_count[k] -= 1
            else:
                added_text[k] = added_text.get(k, 0) + 1
    out = {}
    for rel, old, new in pairs:
        elsewhere = dict(added_text)
        for t in new.splitlines():  # its own additions are already counted inside deleted_lines()
            k = t.strip()
            if elsewhere.get(k):
                elsewhere[k] -= 1
        dels = deleted_lines(old, new, rel, cfg, moved=elsewhere)
        if dels:
            out[rel] = dels
    return out


def on_gate(event):
    sid = str(event.get("session_id") or "")
    cfg = load_config()
    d = read_state()
    session = (d.get("sessions") or {}).get(sid) or {}
    start = session.get("turn_start")
    start_version = session.get("turn_start_version")
    end = snapshot("turn end", sid)
    if not start or start == end["commit"]:
        return None
    analysis = turn_analysis(start, end["commit"], cfg)
    items = declarations(None, sid)
    total = sum(len(v) for v in analysis.values())
    undeclared = [(rel, no, t) for rel, dels in sorted(analysis.items()) for no, t, imp in dels
                  if imp and not declared(t, rel, items)]
    declared_files = len({k.get("path") for k in items})
    summary = ("Deletion guard · version %d (%s) · turn start version %s · %d lines deleted in %d files, "
               "%d important undeclared%s"
               % (end["version"], end["time"][:16], start_version, total, len(analysis), len(undeclared),
                  " · declarations in %d files" % declared_files if declared_files else ""))
    d = read_state()
    session = (d.get("sessions") or {}).get(sid) or session
    if undeclared and cfg["important_deletion"] == "deny":
        sig = hashlib.sha1(json.dumps(undeclared, ensure_ascii=False).encode()).hexdigest()
        block = session.get("block") or {"sig": None, "count": 0}
        if block.get("sig") != sig and block.get("count", 0) < cfg["max_blocks"]:
            session["block"] = {"sig": sig, "count": block.get("count", 0) + 1}
            d.setdefault("sessions", {})[sid] = session
            write_state(d)
            first = undeclared[0]
            return {"decision": "block", "reason":
                    "Deletion guard: %d important lines disappeared this turn without a declaration "
                    "(turn start version %s):\n%s\n"
                    "If they went by mistake, put them back; to restore a file to the turn start:\n"
                    "  %s restore %s %s\n"
                    "If you deleted them on purpose, declare it:\n  %s\n"
                    "If you didn't delete these lines, tell the user."
                    % (len(undeclared), start_version, line_list(undeclared, cfg["summary_lines"]),
                       cli(), start_version, first[0], allow_command(first[0], first[2]))}
        log("gate: no progress or max blocks reached, warning: %s" % summary)
        return {"systemMessage": summary + " — WARNING: undeclared important deletions remain; see "
                                           "`deletion_guard.py history`."}
    session["block"] = None
    d.setdefault("sessions", {})[sid] = session
    write_state(d)
    return {"systemMessage": summary} if total or declared_files else None


# ---------------------------------------------------------------- manual

def cmd_allow(args):
    if not args:
        return "usage: allow <path> [--line TEXT ...] [--all] --reason TEXT", 1
    rel = relative(args[0])
    if not rel:
        return "path is not inside the project: %s" % args[0], 1
    lines, everything, reason, i = [], False, "", 1
    while i < len(args):
        if args[i] == "--all":
            everything = True
        elif args[i] == "--line" and i + 1 < len(args):
            lines.append(args[i + 1])
            i += 1
        elif args[i] == "--reason" and i + 1 < len(args):
            reason = args[i + 1]
            i += 1
        i += 1
    if not reason.strip() or (not everything and not lines):
        return "a reason and at least one --line (or --all) are required", 1
    rec = {"at": time.time(), "time": now(), "path": rel, "lines": lines, "all": everything, "reason": reason}
    with open(os.path.join(STATE, "allowed.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return "declaration saved: %s (%s) · valid until the end of the turn" % (
        rel, "all lines" if everything else ", ".join(lines)), 0


def cmd_history(args):
    last = read_state().get("last") or {}
    if not last.get("commit"):
        return "no versions yet", 0
    cmd = ["log", "--format=%s", last["commit"]] + (["--", relative(args[0]) or args[0]] if args else [])
    return git(*cmd) or "no versions for this file", 0


def cmd_show(args):
    if len(args) != 2:
        return "usage: show <N> <path>", 1
    text = content(version_commit(args[0]), relative(args[1]) or args[1])
    return (text, 0) if text is not None else ("%s is not in version %s" % (args[1], args[0]), 1)


def cmd_restore(args):
    if len(args) != 2:
        return "usage: restore <N> <path>", 1
    rel = relative(args[1])
    if not rel:
        return "path is not inside the project: %s" % args[1], 1
    try:
        data = git("cat-file", "-p", "%s:%s" % (version_commit(args[0]), rel), raw=True)
    except RuntimeError:
        return "%s is not in version %s" % (rel, args[0]), 1
    before = snapshot("before restore")
    full = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return "%s restored to version %s (the previous state is kept as version %s)" % (rel, args[0], before["version"]), 0


# ---------------------------------------------------------------- selftest

def selftest():
    """End-to-end test in a temporary repository."""
    problems = []
    root = tempfile.mkdtemp(prefix="deletion_guard_")

    def write(rel, text):
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(text)

    try:
        subprocess.run(["git", "init", "-q", root], check=True)
        setup(root)
        money = ("class Money {\n  final int minor;\n  Money(this.minor);\n\n  Money add(Money o) {\n"
                 "    if (o.minor < 0) throw ArgumentError();\n    audit(o);\n    return Money(minor + o.minor);\n  }\n\n"
                 "  int get total => minor;\n}\n")
        write("lib/money.dart", money)
        write("test/money_test.dart", "void main() {\n  test('adds', () {\n    expect(1, 1);\n  });\n}\n")
        sid = "selftest"
        on_turn({"session_id": sid})

        # 1. Edit that drops a guard clause: denied
        ev = {"session_id": sid, "tool_name": "Edit", "tool_use_id": "t1", "tool_input": {
            "file_path": os.path.join(root, "lib/money.dart"),
            "old_string": "    if (o.minor < 0) throw ArgumentError();\n", "new_string": ""}}
        r = on_before(ev)
        if not (r and r["hookSpecificOutput"]["permissionDecision"] == "deny"):
            problems.append("deleting a guard clause was not denied")

        # 2. Edit that only changes a line (similar): allowed
        ev = {"session_id": sid, "tool_name": "Edit", "tool_use_id": "t2", "tool_input": {
            "file_path": os.path.join(root, "lib/money.dart"),
            "old_string": "  int get total => minor;", "new_string": "  int get total => minor + 0;"}}
        if on_before(ev):
            problems.append("changing a line was treated as a deletion")

        # 3. Write that rewrites the file and drops lines: denied
        ev = {"session_id": sid, "tool_name": "Write", "tool_use_id": "t3", "tool_input": {
            "file_path": os.path.join(root, "lib/money.dart"), "content": "class Money {}\n"}}
        r = on_before(ev)
        if not (r and r["hookSpecificOutput"]["permissionDecision"] == "deny"):
            problems.append("rewriting a file with Write was not denied")

        # 4. Declared deletion: allowed
        msg, code = cmd_allow([os.path.join(root, "lib/money.dart"), "--line", "throw ArgumentError()",
                               "--reason", "validation moved to the constructor"])
        if code:
            problems.append("allow failed: " + msg)
        ev = {"session_id": sid, "tool_name": "Edit", "tool_use_id": "t4", "tool_input": {
            "file_path": os.path.join(root, "lib/money.dart"),
            "old_string": "    if (o.minor < 0) throw ArgumentError();\n", "new_string": ""}}
        if on_before(ev):
            problems.append("a declared deletion was denied")

        # 5. Deletion via shell (no hook saw it): the Stop gate blocks
        on_turn({"session_id": sid})  # new turn: earlier declarations close
        write("test/money_test.dart", "void main() {\n}\n")
        r = on_gate({"session_id": sid})
        if not (r and r.get("decision") == "block" and "test('adds'" in r["reason"]):
            problems.append("a test deleted outside the edit tools did not block the turn")

        # 6. Restore brings it back
        start_version = read_state()["sessions"][sid]["turn_start_version"]
        msg, code = cmd_restore([str(start_version), os.path.join(root, "test/money_test.dart")])
        if code or "test('adds'" not in (read_file(os.path.join(root, "test/money_test.dart")) or ""):
            problems.append("restore did not bring the file back: " + msg)

        # 7. Moving a function to another file is not a deletion
        on_turn({"session_id": sid})
        write("lib/money.dart", money.replace("  int get total => minor;\n", ""))
        write("lib/money_ext.dart", "extension MoneyX on Money {\n  int get total => minor;\n}\n")
        r = on_gate({"session_id": sid})
        if r and r.get("decision") == "block":
            problems.append("a line moved to another file was treated as a deletion")

        # 8. Versions don't touch the branch
        if subprocess.run(["git", "-C", root, "rev-parse", "-q", "--verify", "HEAD"],
                          capture_output=True).returncode == 0:
            problems.append("snapshots created a commit on the branch")
    except Exception as e:  # noqa: BLE001
        problems.append("selftest crashed: %r" % e)
    for p in problems:
        print("PROBLEM: " + p)
    print("ok" if not problems else "%d problems" % len(problems))
    return 1 if problems else 0


def main():
    sub = sys.argv[1] if len(sys.argv) > 1 else ""
    if sub == "selftest":
        return selftest()
    manual = {"allow": cmd_allow, "history": cmd_history, "show": cmd_show, "restore": cmd_restore}
    if sub in manual:
        root = _paths.project_root()
        if root is None:
            print("no guardrails.json in this folder or above it")
            return 1
        if not setup(root):
            print("not a git repository")
            return 1
        msg, code = manual[sub](sys.argv[2:])
        print(msg)
        return code
    events = {"turn": on_turn, "before": on_before, "after": on_after, "gate": on_gate}
    if sub not in events:
        print("usage: deletion_guard.py turn|before|after|gate|allow|history|show|restore|selftest", file=sys.stderr)
        return 1
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    root = _paths.project_root(event.get("cwd") if isinstance(event.get("cwd"), str) else None)
    # guardrails.json is the opt-in: no config, no snapshots and no checks in this project
    if root is None or not os.path.isfile(os.path.join(root, CONFIG_FILE)) or _paths.yields_to_copy(root):
        return 0
    try:
        if not setup(root):
            return 0
        out = events[sub](event)
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — never block on our own failure
        try:
            log("error in %s: %r" % (sub, e))
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
