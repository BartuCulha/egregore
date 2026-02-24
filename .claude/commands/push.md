Push current branch to remote.

## Before anything else

Check `git branch --show-current`. If on `develop`, `main`, or `master`:
  → "You're on {branch}. Run /branch to create a working branch first."
  → Stop. Do not push.

## What to do

1. Push current branch to origin
2. Set upstream if first push

## Example

```
> /push

Pushing feature/2026-01-20-mcp-authentication...

  git push -u origin feature/2026-01-20-mcp-authentication
  ✓ Pushed

Branch is now on GitHub.
Run /pr when ready for review.
```

## Next

Run `/pr` when ready for review.
