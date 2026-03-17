Merge develop into main. Maintainer only.

## What to do

1. **Resolve project dir**: find the main repo (works from worktree or main)
2. **Verify maintainer**: Only oz can release
3. **Show summary**: What's on develop since last release
4. **Confirm** with the user
5. **Merge** develop → main (no fast-forward)
6. **Tag** the release
7. **Sync public** repo (egregore-core)
8. **Notify** team
9. **Clean up** merged working branches

## Step 0: Resolve project directory

All git operations in this command use `git -C "$MAIN_DIR"` so `/release` works from anywhere — worktree or main repo.

```bash
if [ -f .git ]; then
  # In a worktree — resolve main project dir from .git file
  WT_GITDIR=$(sed 's/^gitdir: //' .git)
  MAIN_DIR=$(cd "$WT_GITDIR/../../.." && pwd)
else
  MAIN_DIR=$(pwd)
fi
```

**Every git command below must use `git -C "$MAIN_DIR"`**. Never use bare `git` — that operates on the worktree, not the main repo.

## Step 1: Verify maintainer

```bash
git -C "$MAIN_DIR" config user.name
```

Map to short name. If not oz: **"Only the maintainer can release. Ask oz to run /release."** Stop here.

## Step 2: Show what's on develop

```bash
git -C "$MAIN_DIR" fetch origin --quiet
git -C "$MAIN_DIR" log origin/main..origin/develop --oneline --no-merges
```

Also check for open PRs to develop:
```bash
gh pr list --base develop --state open --json number,title,author
```

If there are open PRs, warn: **"⚠ {N} open PRs to develop. Consider merging or closing them first."**

If develop is identical to main: **"Nothing to release — develop and main are in sync."** Stop here.

## Step 3: Confirm

Show the commit list and ask:
```
Release to main?

  abc1234 Update save command for develop workflow (oz)
  def5678 Add session-start script (oz)
  ghi9012 Fix onboarding for new orgs (cem)

  3 commits. Proceed? (y/n)
```

## Step 4: Merge

```bash
git -C "$MAIN_DIR" checkout main --quiet
git -C "$MAIN_DIR" pull origin main --quiet
git -C "$MAIN_DIR" merge develop --no-ff -m "Release: $(date +%Y-%m-%d)"
```

**If merge conflicts occur** (non-zero exit code): abort and return to develop:
```bash
git -C "$MAIN_DIR" merge --abort
git -C "$MAIN_DIR" checkout develop --quiet
```
Tell the user:
> Merge conflict between main and develop. This usually means a hotfix was applied directly to main.
> Resolve by: `git checkout develop && git merge main`, fix conflicts, then retry `/release`.

Stop here — do NOT push, tag, or sync.

**If merge succeeds**, push and return to develop immediately:
```bash
git -C "$MAIN_DIR" push origin main
git -C "$MAIN_DIR" checkout develop --quiet
```

Returning to develop right after push ensures the main repo is never left on main.

## Step 5: Tag

```bash
git -C "$MAIN_DIR" tag "release/$(date +%Y-%m-%d)"
git -C "$MAIN_DIR" push origin --tags
```

If a tag for today already exists, append a counter: `release/2026-02-07-2`

## Step 6: Sync public repo

Run the `/sync-public` flow to update egregore-core with the new main.

## Step 7: Notify team

```bash
bash bin/notify.sh group "New release on main: [summary of changes]. Run /pull to update."
```

## Step 8: Clean up

Delete merged `dev/*` remote branches:
```bash
git -C "$MAIN_DIR" branch -r --merged origin/main | grep 'origin/dev/' | sed 's|origin/||' | xargs -I{} git -C "$MAIN_DIR" push origin --delete {}
```

## Example

```
> /release

Checking permissions...
  ✓ Maintainer: oz

Fetching latest...

Changes on develop since last release:
  abc1234 Update save command for develop workflow (oz)
  def5678 Add session-start script (oz)
  ghi9012 Fix onboarding for new orgs (cem)

  3 commits ready to release.

  ⚠ 1 open PR to develop: #16 "Bot analytics" by ali
  (This PR will NOT be included — only merged PRs are released.)

Release to main? (y/n)
> y

  Merging develop → main...
    ✓ Merged (no-ff)
    ✓ Pushed to origin/main
    ✓ Tagged: release/2026-02-07

  Syncing to egregore-core...
    ✓ Synced and pushed

  Notifying team...
    ✓ Sent to Egregore channel

  Cleaning up...
    ✓ Deleted 2 merged dev/* branches

Done. Main is updated. Team notified.
```

## Rules

- **Only oz can release** — enforced by checking git config user.name
- **Never fast-forward** — `--no-ff` creates a merge commit for clear release history
- **Always tag** — releases are traceable
- **Always sync public** — egregore-core stays up to date with main
- **Always notify** — team knows when main changes
- **Always use `git -C "$MAIN_DIR"`** — never bare `git`, so it works from worktrees
- **Always return to develop after merge** — main repo must never be left on main
