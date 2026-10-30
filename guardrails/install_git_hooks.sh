#!/bin/sh
# Installs the code shield's git hooks (pre-commit and reference-transaction). An existing hook that isn't
# ours is backed up, never overwritten.
#   pre-commit:            an unapproved critical change can't be committed (shield.py check --index)
#   reference-transaction: paths that skip pre-commit (cherry-pick, revert, rebase, am, merge, commit -n)
#                          can't bring an unapproved lock into a branch either (shield.py refs)
# Remove: rm .git/hooks/pre-commit .git/hooks/reference-transaction
set -e
root="$(git rev-parse --show-toplevel)"
hooks="$(git rev-parse --git-path hooks)"
case "$hooks" in /*) ;; *) hooks="$root/$hooks" ;; esac
mkdir -p "$hooks"
backup() {
    if [ -f "$1" ] && ! grep -q "code shield" "$1"; then
        cp "$1" "$1.before-shield"
        echo "existing hook backed up: $1.before-shield"
    fi
}

target="$hooks/pre-commit"
backup "$target"
cat > "$target" <<'HOOK'
#!/bin/sh
# code shield (guardrails/install_git_hooks.sh): an unapproved critical change can't be committed.
s="$(git rev-parse --show-toplevel)/guardrails/shield.py"
[ -f "$s" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
python3 "$s" check --index
r=$?
# 3 = infrastructure missing (the shield itself is broken): warn, don't block the commit.
[ $r -eq 3 ] && { echo "code shield: check could not run (infrastructure), commit not blocked" >&2; exit 0; }
exit $r
HOOK
chmod +x "$target"
echo "installed: $target"

target="$hooks/reference-transaction"
backup "$target"
cat > "$target.new" <<'HOOK'
#!/bin/sh
# code shield (guardrails/install_git_hooks.sh): an unapproved lock can't enter a branch through a path
# that skips pre-commit.
[ "$1" = prepared ] || exit 0
input="$(cat)"
# only lines that move HEAD or a local branch to a commit: no python for AUTO_MERGE, deletes, symbolic HEAD
printf '%s\n' "$input" | grep -Eq '^[^ ]+ [0-9a-f]*[1-9a-f][0-9a-f]* (HEAD|refs/heads/.+)$' || exit 0
s="$(git rev-parse --show-toplevel 2>/dev/null)/guardrails/shield.py"
[ -f "$s" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
printf '%s\n' "$input" | python3 "$s" refs "$1"
r=$?
[ $r -eq 3 ] && { echo "code shield: ref check could not run (infrastructure), not blocked" >&2; exit 0; }
exit $r
HOOK
chmod +x "$target.new"
mv -f "$target.new" "$target"
echo "installed: $target"
