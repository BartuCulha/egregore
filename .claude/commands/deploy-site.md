Deploy the marketing site from local `site/` → `Curve-Labs/egregore-site` (auto-deploys to egregore.xyz via Netlify). **Maintainer only.**

## Step 1: Verify maintainer

```bash
git config user.name
```

Map to short name. If not oz: **"Only the maintainer can deploy to production. Use `/deploy-preview` to see your changes. Ask oz to deploy when ready."** Stop here.

## Step 2: Deploy

Run the deploy script and show formatted output. Pass through any arguments from $ARGUMENTS (e.g. `dry`).

```bash
bash bin/deploy-site.sh $ARGUMENTS
```

### Format the output

Based on the script's stdout/stderr, show the user a clean summary:

**Normal deploy:**
```
Deploying site to egregore.xyz...
  Source: local site/
  Target: Curve-Labs/egregore-site (main)

  {N} files changed
  Pushed ({commit_sha})

Netlify will auto-deploy in ~30s.
```

**No changes:**
```
Site is already up to date — nothing to deploy.
```

**Dry run:**
```
Dry run — changes that would be deployed:

  Source: local site/
  Target: Curve-Labs/egregore-site (main)

  {diff output from script}

Run /deploy-site to push these changes.
```

**Error:**
Show the error message from the script. Common issues:
- Missing GITHUB_TOKEN → "Run onboarding first or check your .env"
- site/ directory not found → "The site/ directory doesn't exist"
- Push failed → "Push failed — check your permissions on Curve-Labs/egregore-site"

## Arguments

- `dry` or `--dry-run` — show what would change without pushing
