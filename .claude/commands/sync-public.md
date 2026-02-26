Sync this repo to the public egregore-core repo. One-way: curve-labs-core → egregore-core.

Arguments: $ARGUMENTS (Optional: "dry" for dry-run, "diff" to just show what changed)

## What this does

Rsync everything from this repo (curve-labs-core) to the sibling egregore-core repo, excluding personal/generated files. Then optionally commit and push.

The two repos should be identical software — only memory and personal state differ.

## Prerequisites

Both repos must exist as siblings:
```
../curve-labs-core/   ← you are here (development)
../egregore-core/     ← public distribution
```

## Step 1: Validate

```bash
# Check egregore-core exists
CORE_DIR="$(cd "$(dirname "$(git rev-parse --show-toplevel)")"/egregore-core 2>/dev/null && pwd)"
if [ -z "$CORE_DIR" ]; then
  echo "Error: ../egregore-core not found. Clone it first."
  exit 1
fi
```

If `$ARGUMENTS` is empty or not provided, assume full sync (not dry-run).

## Step 2: Check for upstream contributions

Before overwriting, check if egregore-core has commits that aren't in curve-labs-core. These would be contributions from other instances (via PRs or direct pushes) that we'd destroy.

```bash
cd "$CORE_DIR"
git fetch origin main --quiet

# Find commits on egregore-core main that we DON'T have
# Compare framework paths only — the ones /sync-public manages
THEIR_CHANGES=$(git log origin/main --oneline --diff-filter=ACMR -- bin/ .claude/commands/ CLAUDE.md skills/ .gitignore README.md .syncignore --not --remotes=upstream 2>/dev/null || git log origin/main --oneline -20 2>/dev/null)
```

**Then compare:** Check if any files on egregore-core differ from curve-labs-core in ways that egregore-core is NEWER (i.e., changes we haven't pulled back yet).

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"

# Diff framework files between the two repos
# Files where egregore-core has changes NOT in curve-labs-core
UPSTREAM_ONLY=""
for f in $(git diff --name-only HEAD -- bin/ .claude/commands/ CLAUDE.md skills/ 2>/dev/null); do
  if [ -f "$CORE_DIR/$f" ] && [ -f "$REPO_ROOT/$f" ]; then
    # Check if egregore-core version differs from what we'd overwrite
    if ! diff -q "$CORE_DIR/$f" "$REPO_ROOT/$f" >/dev/null 2>&1; then
      # Check git blame — was the egregore-core version modified outside our sync?
      LAST_MSG=$(git -C "$CORE_DIR" log -1 --format="%s" -- "$f" 2>/dev/null || echo "")
      if echo "$LAST_MSG" | grep -qvE "^(Auto-update Egregore|Sync from curve-labs)"; then
        UPSTREAM_ONLY="$UPSTREAM_ONLY\n  $f (last: $LAST_MSG)"
      fi
    fi
  fi
done
```

**If upstream-only changes are found, STOP and warn:**

```
⚠ egregore-core has changes not in curve-labs-core:

  bin/some-script.sh (last: Fix timeout in graph queries)
  .claude/commands/ask.md (last: Add retry logic to /ask)

These would be overwritten by /sync-public. Options:
  1. Cherry-pick them into curve-labs-core first, then re-run /sync-public
  2. Run /sync-public force to overwrite anyway

Aborting.
```

**If `$ARGUMENTS` is "force"**: Skip this check and proceed anyway (same as current behavior).

## Step 3: Rsync

Excludes are read from `.syncignore` at the repo root (single source of truth). If the file is missing, abort with an error.

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
SYNCIGNORE="$REPO_ROOT/.syncignore"

if [ ! -f "$SYNCIGNORE" ]; then
  echo "Error: .syncignore not found. Cannot sync without exclude list."
  exit 1
fi

# Build --exclude flags from .syncignore (skip comments and blank lines)
EXCLUDE_FLAGS=""
while IFS= read -r line; do
  line="$(echo "$line" | sed 's/#.*//' | xargs)"
  [ -z "$line" ] && continue
  EXCLUDE_FLAGS="$EXCLUDE_FLAGS --exclude='$line'"
done < "$SYNCIGNORE"

eval rsync -av --delete $EXCLUDE_FLAGS "$REPO_ROOT/" "$CORE_DIR/"
```

**If `$ARGUMENTS` is "dry"**: Add `--dry-run` to rsync. Show what would change, don't actually sync.

## Step 4: Show diff

```bash
cd "$CORE_DIR"
git status
git diff --stat
```

**If `$ARGUMENTS` is "diff"**: Stop here. Just show the diff, don't commit.

## Step 5: Commit and push

```bash
cd "$CORE_DIR"
git add -A
git commit -m "Auto-update Egregore framework: $(date +%Y-%m-%d)"
git push origin main
# Keep develop in sync — cloners pull from develop
# Use --ff-only: if develop has unmerged work, fail gracefully rather than clobber it
git checkout develop && git merge main --ff-only && git push origin develop && git checkout main || echo "Warning: develop has diverged from main — merge manually"
```

## Output

### Full sync
```
> /sync-public

Checking egregore-core for upstream contributions...
  ✓ No upstream-only changes detected

Syncing curve-labs-core → egregore-core...

  rsync: 14 files changed, 3 deleted

  Changed:
    .claude/commands/activity.md
    .claude/commands/ask.md
    bin/graph.sh
    CLAUDE.md
    ...

  Committing to egregore-core...
    ✓ Committed: "Auto-update Egregore framework: 2026-02-26"
    ✓ Pushed to origin/main
    ✓ develop synced

Done. egregore-core is up to date.
```

### Upstream changes detected
```
> /sync-public

Checking egregore-core for upstream contributions...

  ⚠ egregore-core has changes not in curve-labs-core:

    bin/graph.sh (last: Fix timeout in graph queries)

  These would be overwritten. Cherry-pick first, or run /sync-public force.
  Aborting.
```

### Dry run
```
> /sync-public dry

Dry run — showing what would change:

  Would update: .claude/commands/activity.md
  Would update: bin/graph.sh
  Would delete: old-file.md

  3 files would change. Run /sync-public to apply.
```

### Diff only
```
> /sync-public diff

Current diff between repos:

  .claude/commands/activity.md | 12 ++---
  bin/graph.sh                 |  3 +-

  2 files differ.
```

## Rules

- **Excludes live in `.syncignore`** at the repo root. Edit that file to add/remove private paths. Never hardcode excludes in this command.
- **Always sync everything not in `.syncignore`** — commands, bin scripts, CLAUDE.md, README.md, settings.json, start scripts, `.env.example`, etc.
- The `--delete` flag ensures files removed from curve-labs-core are also removed from egregore-core
- Always show the diff before committing — **review it for anything that shouldn't be public**
- Always check for upstream contributions before syncing — never silently overwrite external changes
- Commit message includes the date for traceability
- When adding new private directories or files to the repo, **always add them to `.syncignore`**
