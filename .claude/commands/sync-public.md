Sync this repo to public distribution repos. One-way: curve-labs-core → egregore-core (and egregore-labs/egregore when ready).

Uses a `public` branch in curve-labs-core that maintains OSS-safe content. Pushes to configured remotes.

Arguments: $ARGUMENTS (Optional: "dry" for preview, "diff" to show current delta)

## When to invoke

User says: "sync public", "push to oss", "update public repo", "sync egregore-core"
Also triggered by `/release` after merging develop → main.

## How it works

```
curve-labs-core (develop/main)
    ↓ rsync through .syncignore → public branch worktree
    ↓ safety review (3 checkpoints)
    ↓ commit with real message
    ↓ git push to remotes
egregore-core / egregore-labs/egregore
```

The `public` branch lives in curve-labs-core. It tracks egregore-core's history (compatible for push). Content is filtered through `.syncignore` on every sync.

## CRITICAL: This is the last gate before code goes public

Every sync goes through 3 mandatory review checkpoints using AskUserQuestion. No auto-pushing. The user must confirm:
1. The file-level diff (what's changing)
2. A safety scan (gray-zone files flagged)
3. The commit message and push targets

## Step 0: Setup check

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
SYNCIGNORE="$REPO_ROOT/.syncignore"

if [ ! -f "$SYNCIGNORE" ]; then
  echo "Error: .syncignore not found."
  exit 1
fi

# Verify public branch exists
if ! git show-ref --verify --quiet refs/heads/public 2>/dev/null; then
  echo "Error: 'public' branch not found. Create it first:"
  echo "  git fetch upstream main && git branch public upstream/main"
  exit 1
fi

# Verify at least one push target
REMOTES=""
git remote get-url upstream &>/dev/null && REMOTES="$REMOTES upstream"
git remote get-url oss &>/dev/null && REMOTES="$REMOTES oss"
if [ -z "$REMOTES" ]; then
  echo "Error: no push targets. Add remotes:"
  echo "  git remote add upstream git@github.com:Curve-Labs/egregore-core.git"
  echo "  git remote add oss git@github.com:egregore-labs/egregore.git"
  exit 1
fi
```

## Step 1: Create temporary worktree for public branch

```bash
PUBLIC_WT="$REPO_ROOT/.claude/worktrees/_sync-public"

# Clean up any leftover from previous sync
if [ -d "$PUBLIC_WT" ]; then
  git worktree remove "$PUBLIC_WT" --force 2>/dev/null || rm -rf "$PUBLIC_WT"
  git worktree prune 2>/dev/null
fi

git worktree add "$PUBLIC_WT" public --quiet
```

## Step 2: Rsync filtered content into public worktree

```bash
# Build --exclude flags and collect patterns for cleanup
EXCLUDE_FLAGS=""
EXCLUDED_PATTERNS=()
while IFS= read -r line; do
  line="$(echo "$line" | sed 's/#.*//' | xargs)"
  [ -z "$line" ] && continue
  EXCLUDE_FLAGS="$EXCLUDE_FLAGS --exclude='$line'"
  EXCLUDED_PATTERNS+=("$line")
done < "$SYNCIGNORE"

# Sync: source is repo root, target is public worktree
# --delete removes files from public that don't exist in source
# NOTE: --delete does NOT remove pre-existing files that match exclude patterns.
# Those are cleaned up separately below.
eval rsync -a --delete $EXCLUDE_FLAGS "$REPO_ROOT/" "$PUBLIC_WT/"

# Clean up pre-existing excluded files from destination.
# rsync --delete skips excluded files — if a file existed before being added to
# .syncignore, it stays in the destination. We remove those manually.
# CRITICAL: skip .git — deleting it breaks the worktree.
cd "$PUBLIC_WT"
for pattern in "${EXCLUDED_PATTERNS[@]}"; do
  case "$pattern" in .git|.git/) continue ;; esac
  for f in $pattern; do
    [ -e "$f" ] && rm -rf "$f"
  done
done

# Clean up runtime artifacts
rm -f .DS_Store .egregore-worktree-pid .egregore-worktree-tty .egregore-worktree-active 2>/dev/null
rm -rf .claude/worktrees 2>/dev/null
cd "$REPO_ROOT"
```

**If `$ARGUMENTS` is "dry"**: Add `--dry-run -v` to rsync. Show what would change, then clean up worktree and stop.

## Step 3: Stage and diff

```bash
cd "$PUBLIC_WT"
git add -A
DIFF_STAT=$(git diff --cached --stat)
DIFF_FILES=$(git diff --cached --name-only)
ADDED=$(git diff --cached --name-only --diff-filter=A)
DELETED=$(git diff --cached --name-only --diff-filter=D)
MODIFIED=$(git diff --cached --name-only --diff-filter=M)
```

If empty: "Already up to date." Clean up worktree and stop.

**If `$ARGUMENTS` is "diff"**: Show diff, clean up worktree, stop.

## Step 4: CHECKPOINT 1 — Review file changes

Show the full diff summary organized by type:

```
New files:
  + .github/workflows/shellcheck.yml
  + bin/lib/config.sh

Deleted files:
  - .claude/commands/eval.md
  - .claude/commands/hosting.md

Modified files:
  M bin/worktree.sh        | 220 +++----
  M bin/session-start.sh   | 1111 ++-----
  M CLAUDE.md              | 12 +-

Total: X added, Y deleted, Z modified
```

Then use AskUserQuestion:

```
header: "Changes"
question: "These files will go to the public repo. Review the list above — anything that shouldn't be there?"
options:
  - label: "Looks good"
    description: "All files are safe for public"
  - label: "Hold on"
    description: "I see something that shouldn't be public — let me check"
```

**If "Hold on"**: Stop. Let the user investigate. They can re-run `/sync-public` after fixing.
**If "Looks good"**: Continue.

## Step 5: CHECKPOINT 2 — Safety scan for gray-zone content

Automatically scan the staged changes for potential issues. Run these checks on the public worktree:

```bash
cd "$PUBLIC_WT"

# 1. Hardcoded secrets or tokens
SECRETS=$(grep -rn "gho_\|ghp_\|sk-\|ek_\|Bearer \|password\s*=" --include="*.sh" --include="*.js" --include="*.json" --include="*.md" . 2>/dev/null | grep -v node_modules | grep -v ".git/" | head -10)

# 2. Internal org references that should be parameterized
INTERNAL_REFS=$(grep -rn "Curve-Labs\|curve-labs\|curvelabs\|oguzhan\|cemdagdelen\|fcdagdelen" --include="*.sh" --include="*.js" --include="*.json" . 2>/dev/null | grep -v node_modules | grep -v ".git/" | grep -v CLAUDE.md | head -10)

# 3. Files that look internal (hardcoded IPs, internal URLs, etc.)
INTERNAL_URLS=$(grep -rn "railway\.app\|supabase\.co\|xgksfrirtdumacvmfkzj\|neo4j+s://" --include="*.sh" --include="*.js" --include="*.json" --include="*.md" . 2>/dev/null | grep -v node_modules | grep -v ".git/" | head -10)

# 4. .env or state files that shouldn't be committed
LEAKED_FILES=""
[ -f ".env" ] && LEAKED_FILES="$LEAKED_FILES .env"
[ -f ".egregore-state.json" ] && LEAKED_FILES="$LEAKED_FILES .egregore-state.json"
[ -f ".egregore-session-id" ] && LEAKED_FILES="$LEAKED_FILES .egregore-session-id"
[ -f "egregore.json" ] && LEAKED_FILES="$LEAKED_FILES egregore.json"
```

**If ANY issues found**, show them grouped:

```
⚠ Safety scan found items to review:

Possible secrets:
  bin/graph.sh:14: Bearer $API_KEY    ← (probably safe — variable, not literal)

Internal references:
  CLAUDE.md:3: Curve-Labs/egregore-site   ← update to egregore-labs?
  bin/lib/git-sync.sh:32: Curve-Labs/egregore-core.git  ← default upstream

Internal URLs:
  (none found)

Leaked files:
  (none found)
```

Then use AskUserQuestion:

```
header: "Safety"
question: "Review the flagged items above. Are they safe for the public repo?"
multiSelect: false
options:
  - label: "All safe"
    description: "These are expected (variables, not secrets; refs will be updated later)"
  - label: "Fix needed"
    description: "I need to fix something before syncing"
  - label: "Skip scan"
    description: "I've reviewed manually, proceed"
```

**If "Fix needed"**: Stop. User fixes and re-runs.
**If "All safe" or "Skip scan"**: Continue.

**If NO issues found**: Show `✓ Safety scan clean — no secrets, no internal refs, no leaked files.` and skip the question.

## Step 6: CHECKPOINT 3 — Commit message and push targets

Look at what merged to develop/main since the last public sync:

```bash
LAST_PUBLIC_DATE=$(git log public -1 --format="%ai" | cut -d' ' -f1)
RECENT_PRS=$(git log --merges --format="%s" --since="$LAST_PUBLIC_DATE" develop | sed 's/Merge pull request #\([0-9]*\) from .*/PR #\1/' | head -5)
```

Draft the commit message:
- If there's 1 PR: use its title
- If there are multiple: summarize, list PRs in the body
- If the user provided a message in `$ARGUMENTS` (not "dry"/"diff"): use that

Show the commit message and push targets, then use AskUserQuestion:

```
header: "Publish"
question: "Commit and push to public repos?"
options:
  - label: "Push"
    description: "Commit and push to all configured remotes"
    preview: "{commit message}\n\nPush targets:\n  → Curve-Labs/egregore-core (upstream)\n  → egregore-labs/egregore (oss)"
  - label: "Edit message"
    description: "I want to change the commit message"
  - label: "Cancel"
    description: "Abort — don't push anything"
```

**If "Cancel"**: Clean up worktree and stop. Changes are NOT committed.
**If "Edit message"**: Ask for new message, then show this checkpoint again.
**If "Push"**: Commit and push.

## Step 7: Commit and push

```bash
cd "$PUBLIC_WT"
git commit -m "$COMMIT_MESSAGE"

# Push to each configured remote
if git remote get-url upstream &>/dev/null; then
  git push upstream public:main --quiet && echo "  ✓ Pushed to Curve-Labs/egregore-core"
fi

if git remote get-url oss &>/dev/null; then
  git push oss public:main --quiet && echo "  ✓ Pushed to egregore-labs/egregore"
fi
```

## Step 8: Clean up worktree

```bash
cd "$REPO_ROOT"
git worktree remove "$PUBLIC_WT" --force 2>/dev/null
git worktree prune 2>/dev/null
```

## Output

### Full sync (all 3 checkpoints)
```
> /sync-public

Syncing to public repos...

  New files:
    + bin/lib/config.sh
    + bin/lib/identity.sh

  Deleted files:
    - .claude/commands/eval.md
    - .claude/commands/hosting.md

  Modified files:
    M bin/worktree.sh  | 220 +++----
    M CLAUDE.md        | 12 +-

  Total: 2 added, 2 deleted, 2 modified

  ✓ Changes reviewed

  ⚠ Safety scan:
    CLAUDE.md:3: Curve-Labs/egregore-site  ← known, will update later

  ✓ Flagged items acknowledged

  Commit: "feat: OSS hardening — worktree fix, exclude internal commands"
  Push to: Curve-Labs/egregore-core, egregore-labs/egregore

  ✓ Committed to public branch
  ✓ Pushed to Curve-Labs/egregore-core
  ✓ Pushed to egregore-labs/egregore

Done. Public repos updated.
```

### Dry run
```
> /sync-public dry

Dry run — would change:
  bin/worktree.sh (modified)
  .claude/commands/eval.md (deleted)

  2 files would change.
```

## Rules

- **3 checkpoints are mandatory** — never skip. Even if the diff looks clean.
- **`.syncignore` is the single source of truth** for what gets excluded
- **Never commit generic "Sync from" messages** — use real descriptions
- **Safety scan runs every time** — it catches things .syncignore doesn't (hardcoded strings, internal refs)
- **The `public` branch lives in curve-labs-core** — do not delete it
- **Remotes**: `upstream` = Curve-Labs/egregore-core, `oss` = egregore-labs/egregore
- **If in doubt, cancel** — you can always re-run after investigating
