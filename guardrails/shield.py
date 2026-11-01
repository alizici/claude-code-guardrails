#!/usr/bin/env python3
"""Code shield: catches unapproved changes to, and deletions of, critical code (locked files and regions).

Why: an AI agent sometimes deletes or rewrites something critical while doing something else, and the
system breaks in a way nobody notices until much later. In one real case a revert script silently
deleted a decoder function; it was only spotted when someone read the diff.

The lock list is shield.json: each lock has a fingerprint (sha256 of the normalized content), a reason
and the tests that protect it. A region lock covers the lines BETWEEN two markers, in any comment style:
    // SHIELD: <name>          # SHIELD: <name>
    ...                        ...
    // SHIELD-END: <name>      # SHIELD-END: <name>

Commands (from the project root; installed as a plugin, the script is in the plugin's folder and the
messages below show its full path):
    python3 guardrails/shield.py check              working tree: 0 clean, 1 unapproved change or deletion
    python3 guardrails/shield.py check --index      what is about to be committed (git pre-commit hook)
    python3 guardrails/shield.py refs prepared      new commits entering a branch or HEAD (git
                                                    reference-transaction hook; stdin "<old> <new> <ref>")
    python3 guardrails/shield.py gate               end-of-turn gate: check + tests of unverified locks
    python3 guardrails/shield.py test [name ...]    run lock tests (all locks if none given); passing marks them verified
    python3 guardrails/shield.py approve <name ...> --reason "..."   record an intentional change (USER APPROVAL)
    python3 guardrails/shield.py list               locks and their status

Exit: 0 clean, 1 violation or failing test, 3 infrastructure missing (a hook treats 3 as a warning only).
Which fingerprint was verified by tests is kept under .git/shield/ and never committed.

Why two git hooks: pre-commit only runs on `git commit`. A conflict-free cherry-pick, revert, rebase, am,
merge or `commit -n` skips it, and in practice a cherry-pick once committed an unapproved lock change.
reference-transaction runs on every ref update and can stop it at 'prepared'.
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
import _paths  # noqa: E402  (next to this script)

ROOT = _paths.project_root()
LOCKS = "shield.json"
LOG = os.path.join("docs", "SHIELD_LOG.md")
COMMENT = r"(?://|#|--|;|<!--|/\*|\*)"
START = re.compile(r"^\s*" + COMMENT + r"\s*SHIELD:\s*([A-Za-z0-9_.-]+)\s*(?:-->|\*/)?\s*$")
END = re.compile(r"^\s*" + COMMENT + r"\s*SHIELD-END:\s*([A-Za-z0-9_.-]+)\s*(?:-->|\*/)?\s*$")
MARKER = re.compile(COMMENT + r"\s*SHIELD(?:-END)?:\s*([A-Za-z0-9_.-]+)")
OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# Files where a SHIELD marker is an example, not a lock (override with "scan_exclude" in shield.json).
DEFAULT_SCAN_EXCLUDE = ["guardrails/*", "*.md", "tests/*", "**/tests/*", "**/test/*"]


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def read(path, index):
    """The file's text; None if missing. index=True: the staged version (what gets committed)."""
    if index:
        r = git("show", f":{path}", check=False)
        return r.stdout if r.returncode == 0 else None
    full = os.path.join(ROOT, path)
    if not os.path.isfile(full):
        return None
    with open(full, encoding="utf-8", errors="replace") as f:
        return f.read()


def load(index=False):
    text = read(LOCKS, index)
    if text is None:
        if index:
            text = read(LOCKS, False)
        if text is None:
            print(f"shield: {LOCKS} not found", file=sys.stderr)
            sys.exit(3)
    return json.loads(text)


def save(data):
    with open(os.path.join(ROOT, LOCKS), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize(lines):
    """Whitespace and blank-line differences don't count as a change; everything else (comments too) does."""
    return "\n".join(s.rstrip() for s in lines if s.strip())


def region(text, name):
    """Lines between the markers; None if markers are missing or broken."""
    inside, lines = False, []
    for s in text.splitlines():
        m = START.match(s)
        if m and m.group(1) == name:
            if inside:
                return None  # nested or two starts: broken
            inside = True
            continue
        m = END.match(s)
        if m and m.group(1) == name:
            if not inside:
                return None
            return lines
        if inside:
            lines.append(s)
    return None  # no end marker


def fingerprint(lock, index=False):
    """(fingerprint or None, problem text or None)."""
    text = read(lock["file"], index)
    if text is None:
        return None, "file missing (deleted or moved)"
    if lock.get("region"):
        lines = region(text, lock["region"])
        if lines is None:
            return None, f"markers for region '{lock['region']}' are missing or broken"
    else:
        lines = text.splitlines()
    return hashlib.sha256(normalize(lines).encode("utf-8")).hexdigest()[:16], None


def verified_path():
    r = git("rev-parse", "--git-dir", check=False)
    gitdir = r.stdout.strip() if r.returncode == 0 else ".git"
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(ROOT, gitdir)
    d = os.path.join(gitdir, "shield")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "verified.json")


def verified():
    try:
        with open(verified_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_verified(d):
    with open(verified_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def unregistered_markers(data, index=False):
    """Region markers in the code that aren't in the lock list (warning)."""
    known = {k["region"] for k in data["locks"] if k.get("region")}
    skip = data.get("scan_exclude", DEFAULT_SCAN_EXCLUDE)
    r = git("grep", "-l", "--cached" if index else "--untracked", "-I", "SHIELD:", check=False)
    found = set()
    for path in r.stdout.split():
        if path == LOCKS or any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch("/" + path, "*/" + g.lstrip("*/"))
                                for g in skip):
            continue
        for m in MARKER.finditer(read(path, index) or ""):
            if m.group(1) not in known:
                found.add((path, m.group(1)))
    return sorted(found)


def check(data, index=False, quiet=False):
    """(violations, warnings). A violation is an unapproved change or deletion."""
    violations, warnings = [], []
    for k in data["locks"]:
        current, problem = fingerprint(k, index)
        where = k["file"] + (f" [{k['region']}]" if k.get("region") else "")
        if problem:
            violations.append(f"DELETED  {k['name']}: {where}: {problem}")
        elif current != k.get("fingerprint"):
            violations.append(f"CHANGED  {k['name']}: {where} (approved {k.get('fingerprint')}, now {current})")
    for path, name in unregistered_markers(data, index):
        warnings.append(f"UNREGISTERED {path}: marker '{name}' is not in the lock list")
    if not quiet:
        for w in warnings:
            print("warning: " + w)
        for v in violations:
            print(v)
    return violations, warnings


def guidance(violations):
    names = " ".join(sorted({v.split()[1].rstrip(":") for v in violations}))
    cli = _paths.command("shield.py", ROOT)
    print("\nLocked critical code was changed or deleted. If that wasn't intentional, REVERT it (look at git diff).")
    print("If it was: run the lock tests first, then approve with a reason (this asks the user):")
    print(f"  {cli} test {names}")
    print(f"  {cli} approve {names} --reason \"<what changed and why>\"")


def new_commits(lines):
    """Commits from reference-transaction input that enter HEAD or a local branch and aren't reachable
    from any local branch, tag or HEAD (before). Moving to an existing commit (reset, checkout, fast
    forward) isn't checked: that commit was checked when it came in."""
    tips = []
    for s in lines:
        p = s.split()
        if len(p) != 3 or not (p[2] == "HEAD" or p[2].startswith("refs/heads/")):
            continue
        if OID.match(p[1]) and p[1].strip("0") and p[1] not in tips:
            tips.append(p[1])
    if not tips:
        return []
    exclude = ["--branches", "--tags"]
    if git("rev-parse", "-q", "--verify", "HEAD^{commit}", check=False).returncode == 0:
        exclude.append("HEAD")
    return git("rev-list", "--reverse", *tips, "--not", *exclude).stdout.split()


LIST_DELETED = f"DELETED  lock list: {LOCKS} missing from the commit (all locks would be dropped)"


def check_commit(c):
    """A commit tree's violations (the pre-commit check, with a temporary index). A commit that touches
    neither a locked file nor the list (against any parent) has its parent's status: skipped. A commit
    that deletes the list is a violation: without it every later lock change would pass."""
    changed = set(git("diff-tree", "-r", "-m", "--root", "--no-commit-id", "--name-only", "-z", c).stdout.split("\0"))
    r = git("show", f"{c}:{LOCKS}", check=False)
    if r.returncode != 0:
        return [LIST_DELETED] if LOCKS in changed else []  # never existed: history before the shield
    if not changed & ({k["file"] for k in json.loads(r.stdout)["locks"]} | {LOCKS}):
        return []
    d = tempfile.mkdtemp(prefix="shield-")
    old = os.environ.get("GIT_INDEX_FILE")
    os.environ["GIT_INDEX_FILE"] = os.path.join(d, "index")
    try:
        git("read-tree", c)
        violations, _ = check(load(True), True, quiet=True)
    finally:
        if old is None:
            os.environ.pop("GIT_INDEX_FILE", None)
        else:
            os.environ["GIT_INDEX_FILE"] = old
        shutil.rmtree(d, True)
    return violations


def cmd_refs(state, lines):
    if state != "prepared":
        return 0
    found = []
    for c in new_commits(lines):
        violations = check_commit(c)
        if violations:
            print(f"commit {c[:10]}:")
            for v in violations:
                print("  " + v)
            found += violations
    if not found:
        return 0
    if LIST_DELETED in found:
        print("\nThe lock list isn't deleted; removing a lock means removing its entry (this asks the user).")
    if set(found) - {LIST_DELETED}:
        guidance([v for v in found if v != LIST_DELETED])
    print("\nRef update stopped (git reference-transaction: code shield). The commit came in a way that skips "
          "the commit hook (cherry-pick, revert, rebase, am, merge, --no-verify) or the lock isn't approved; "
          "commit again after approval.")
    return 1


def run_tests(data, locks):
    """Group the locks' tests by runner and run each runner once. (passed, infra missing)."""
    runners = data.get("runners") or {}
    groups = {}
    for k in locks:
        for runner, names in (k.get("tests") or {}).items():
            groups.setdefault(runner, [])
            for n in names:
                if n not in groups[runner]:
                    groups[runner].append(n)
    passed = True
    for runner, names in groups.items():
        if not names:
            continue
        cfg = runners.get(runner)
        if not cfg or not cfg.get("run"):
            print(f"shield: runner '{runner}' is not defined in {LOCKS}", file=sys.stderr)
            return False, True
        cmd = cfg["run"].replace("{tests}", " ".join(shlex.quote(n) for n in names))
        print(f"== lock tests ({runner}): {', '.join(names)}", flush=True)
        try:
            r = subprocess.run(["/bin/bash", "-c", cmd], cwd=os.path.join(ROOT, cfg.get("cwd", ".")),
                               capture_output=True, text=True, timeout=cfg.get("timeout", 600))
        except subprocess.TimeoutExpired:
            print(f"shield: runner '{runner}' timed out", file=sys.stderr)
            return False, True
        if r.returncode in (126, 127):
            print(f"shield: runner '{runner}' could not start: {r.stderr.strip()[-300:]}", file=sys.stderr)
            return False, True
        out = (r.stdout + r.stderr).strip().splitlines()
        print("\n".join(out[-40:]))
        if r.returncode != 0:
            passed = False
    return passed, False


def write_log(lines):
    path = os.path.join(ROOT, LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.isfile(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# Code shield log\n\n")
            f.write("Every intentional change to locked critical code (`shield.py approve`). "
                    "Historical record; the current lock list is `shield.json`.\n\n")
            f.write("| time (UTC) | lock | fingerprint | commit | reason |\n|---|---|---|---|---|\n")
        for s in lines:
            f.write(s + "\n")


def cmd_approve(data, names, reason, all_locks):
    if not reason or len(reason.strip()) < 10:
        print("shield: --reason is required (at least 10 characters): what changed, why, which test verified it")
        return 1
    locks = data["locks"] if all_locks else [k for k in data["locks"] if k["name"] in names]
    unknown = set(names) - {k["name"] for k in data["locks"]}
    if unknown:
        print(f"shield: no such lock: {', '.join(sorted(unknown))}")
        return 1
    head = git("rev-parse", "--short", "HEAD", check=False).stdout.strip() or "?"
    dirty = "+" if git("status", "--porcelain", check=False).stdout.strip() else ""
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    records, state = [], verified()
    for k in locks:
        current, problem = fingerprint(k)
        if problem:
            print(f"shield: {k['name']} can't be approved: {problem}")
            return 1
        if current == k.get("fingerprint"):
            continue
        records.append(f"| {when} | {k['name']} | {k.get('fingerprint') or '-'} → {current} | {head}{dirty} | {reason.strip()} |")
        k["fingerprint"] = current
        state.pop(k["name"], None)  # the new fingerprint isn't test-verified yet: the gate will run them
    if not records:
        print("shield: no changed locks, nothing to approve")
        return 0
    save(data)
    write_log(records)
    save_verified(state)
    print("\n".join(records))
    print(f"shield: {len(records)} lock(s) approved; the end-of-turn gate or 'shield.py test' will run their tests")
    return 0


def main():
    p = argparse.ArgumentParser(description="Code shield: catches unapproved changes to critical code")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--index", action="store_true", help="staged content (what gets committed)")
    sub.add_parser("gate")
    rf = sub.add_parser("refs")
    rf.add_argument("state")
    t = sub.add_parser("test")
    t.add_argument("names", nargs="*")
    a = sub.add_parser("approve")
    a.add_argument("names", nargs="*")
    a.add_argument("--reason", required=True)
    a.add_argument("--all", action="store_true")
    sub.add_parser("list")
    args = p.parse_args()

    if args.cmd == "check":
        data = load(args.index)
        violations, _ = check(data, args.index)
        if violations:
            guidance(violations)
            if args.index:
                print("\nCommit stopped (git pre-commit: code shield).")
            return 1
        print(f"shield: {len(data['locks'])} lock(s) clean")
        return 0

    if args.cmd == "refs":
        return cmd_refs(args.state, sys.stdin.read().splitlines())

    data = load()
    if args.cmd == "list":
        state = verified()
        for k in data["locks"]:
            current, problem = fingerprint(k)
            status = ("DELETED" if problem else "CHANGED" if current != k.get("fingerprint")
                      else "verified" if state.get(k["name"]) == current else "untested")
            where = k["file"] + (f" [{k['region']}]" if k.get("region") else "")
            print(f"{status:<10} {k['name']:<24} {where}")
        return 0

    if args.cmd == "approve":
        return cmd_approve(data, args.names, args.reason, args.all)

    if args.cmd == "test":
        chosen = [k for k in data["locks"] if not args.names or k["name"] in args.names]
        passed, infra = run_tests(data, chosen)
        if infra:
            return 3
        if passed:
            state = verified()
            for k in chosen:
                current, problem = fingerprint(k)
                if not problem and current == k.get("fingerprint"):
                    state[k["name"]] = current
            save_verified(state)
            print("shield: lock tests passed")
            return 0
        print("shield: lock tests FAILED")
        return 1

    if args.cmd == "gate":
        violations, _ = check(data)
        if violations:
            guidance(violations)
            return 1
        state = verified()
        pending = [k for k in data["locks"] if state.get(k["name"]) != k.get("fingerprint")]
        if not pending:
            print(f"shield: {len(data['locks'])} lock(s) clean and verified")
            return 0
        print(f"shield: unverified lock(s): {', '.join(k['name'] for k in pending)}")
        passed, infra = run_tests(data, pending)
        if infra:
            return 3
        if not passed:
            print("\nLock tests FAILED: critical behavior may be broken. Fix it or revert the change.")
            return 1
        for k in pending:
            state[k["name"]] = k.get("fingerprint")
        save_verified(state)
        print("shield: lock tests passed, locks verified")
        return 0
    return 0


if __name__ == "__main__":
    if ROOT is None:
        print("shield: no guardrails.json in this folder or above it (no project to check)", file=sys.stderr)
        sys.exit(3)
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"shield: {e}", file=sys.stderr)
        sys.exit(3)
