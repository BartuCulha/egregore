# Instance Context — Curve Labs Internal

This is **Curve Labs' internal Egregore** (`curve-labs-core`) — the canonical instance where the product is built, tested, and dogfooded.

## What this repo is
- The **internal development hub** for Egregore the product
- Has its own Neo4j instance, memory repo (`curve-labs-memory`), and API key
- Active team: oz (maintainer), cem, pali, renc
- Managed repos: `lace`, `egregore-site`

## What this repo is NOT
- NOT the public template. The public repo is `Curve-Labs/egregore-core`
- `egregore-core` is a clean template that new orgs fork via `npx create-egregore`
- Changes flow one-way: internal → public via `/sync-public` or `/release`. Never reverse.

## Framework changes
When working on framework code (bin/, .claude/commands/, CLAUDE.md, skills/), remember:
- This code ships to **every Egregore user** when released
- `egregore.md` is the soul doc template — don't put Curve Labs-specific content there
- Instance-specific context belongs here (`.egregore/context.md` — gitignored)
- Test here first, then `/release` → `/sync-public`

## Release flow
1. Work on `curve-labs-core` (this repo) on working branches
2. PR to develop, merge
3. `/release` merges develop → main, tags, notifies team
4. `/sync-public` pushes framework files to `egregore-core` (public template)
5. New orgs get the changes when they `npx create-egregore`
6. Existing orgs get them via `/update` (auto-applies upstream framework updates)
