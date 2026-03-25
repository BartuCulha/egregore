# Onboarding Redesign: Orient First, Collect Later

**Date:** 2026-03-25
**Author:** kaan
**Status:** Approved
**Branch:** dev/kaan/onboarding-flow-redesign

## Problem

The current onboarding flow asks "who are you?" and "what do you care about?" before the person has any idea what Egregore is or how it works. This creates friction and collects information without context — users can't meaningfully answer "what brings you here?" when they don't yet understand what "here" is.

## Thesis

Orient people first. Let them understand the system by using it. Collect profile information naturally during real work, not in a separate form-filling phase.

## New State Machine

```
VERIFY → ORIENT → FIRST_TODO → [user works] → FIRST_HANDOFF → normal usage
                                                                    ↓
                                                      profile collected progressively
```

Replaces the current 7-state machine:
```
VERIFY → WELCOME → HARVEST_IDENTITY → HARVEST_CONNECTION → CONSENT → ORIENT → COMPLETE
```

## States

### VERIFY (unchanged — identical to current implementation)

Implementation: `.claude/commands/onboarding.md` → State: VERIFY

Checks tokens, memory symlink, local/connected mode. Silent — user sees nothing if everything passes.

- `GITHUB_TOKEN` in `.env` — if missing, runs `bash bin/github-auth.sh`. If still missing → HALT: "GitHub auth failed. Run `bash bin/github-auth.sh` manually."
- `EGREGORE_API_KEY` in `.env` — if missing AND `api_url` is set → HALT: "Missing API key. Ask your team admin for it, then add `EGREGORE_API_KEY=ek_...` to `.env`." If `api_url` is empty → skip (local mode).
- `memory/` symlink — if missing → clone memory repo from `egregore.json → memory_repo`, create symlink. If clone fails → HALT with git error.
- Detects local vs connected mode from `egregore.json → api_url`. Stores result for entire flow.

Exit: → ORIENT (all checks pass) or HALT (any check fails)

### ORIENT (replaces WELCOME + old ORIENT)

Three lines. Thesis, mechanic, prompt. No feature tour — just enough framing to make the first action meaningful.

**Output:**

> "An organization should be able to think across sessions, across people, across time. Egregore is how {org_name} does that."
>
> "You declare what you're working on (`/todo`), do the work, then capture what you learned (`/handoff`). That's the core loop — everything else builds on it."
>
> "What are you working on right now?"

No questions. No choices. Context, then action.

**State update:** `onboarding.phase = "orient"`, `onboarding.started_at = {ISO timestamp}`

Exit: → FIRST_TODO (user describes their work)

### FIRST_TODO

User describes what they're working on. Claude:

1. Creates the todo item — if graph is connected, uses `/todo` command (creates Todo node). If graph is offline/local mode, creates a local note in `.egregore/notes/` as fallback. The todo is the vehicle, not the goal — what matters is the user declared work.
2. Asks name via AskUserQuestion:
   ```
   header: "Name"
   question: "What should we call you here? Your GitHub name is {github_name}."
   options:
     - label: "{github_name}"
       description: "Use my GitHub name"
     - label: "Something else"
       description: "I go by a different name"
   ```
   If "Something else" → user provides custom name via freeform input. **Validation:** 1-30 chars, alphanumeric + spaces + hyphens only. If invalid, re-prompt: "Names can be 1-30 characters with letters, numbers, spaces, or hyphens."
3. Creates the working branch: `dev/{name}/{topic-slug}`
4. Shows technical orientation (onboarding only):

```
  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
  What just happened:

  main ← stable releases
    └─ develop ← where work integrates
         └─ dev/{name}/{slug} ← your branch (just created)

  Your changes live on your branch. When you /save,
  it creates a PR to develop. Others can see it there.
  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
```

**State update:** `display_name`, `name`, `onboarding.phase = "first_todo"`

**API calls (connected mode only):**
- `POST /api/user/ensure` with github_username, display_name

Exit: → user works normally. Claude assists as usual.

### FIRST_HANDOFF

**Trigger detection:** This state activates when the user signals session-end intent. Detection rules (in priority order):

1. **Explicit command:** User runs `/wrap`, `/handoff`, or `/save` — always triggers.
2. **Session-end phrases:** "I'm done", "wrapping up", "gotta go", "signing off", "that's it for today" — triggers when the phrase refers to the session, not a subtask.
3. **Disambiguation:** "I'm done with this file" or "done with that bug" = subtask completion, NOT a session-end trigger. Only trigger when the user is clearly ending the session. When ambiguous, ask: "Done for the session, or just this task?"
4. **`/save` during onboarding:** Runs the normal `/save` flow (push branch, create PR) AND triggers FIRST_HANDOFF. The save and the handoff are complementary — save pushes code, handoff captures context.

**If the user closes the terminal without triggering FIRST_HANDOFF:** Session state remains at `phase = "first_todo"`. On next session launch, `session-start.sh` detects `onboarding_complete: false` with `phase = "first_todo"`. Claude says: "Welcome back! Last time you were working on {todo text}. Before we continue — let's capture what you did last session so it's not lost." Then runs the FIRST_HANDOFF flow for the previous session's work, using git log and branch name as context.

Claude intercepts:

> "Before you close out — this is the part that makes the system work. A handoff captures what you did so the organization remembers it. Not a summary for a manager — a briefing for the next session, or the next person."

Runs the handoff flow. During this, collects role naturally:

> "One thing that helps route handoffs: what's your role? Engineering, design, research, operations?"

After the handoff completes, shows technical orientation:

```
  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
  What just happened:

  memory/handoffs/
    └─ 2026-03-25-{slug}.md    ← your handoff (just created)

  memory/people/
    └─ {username}.md            ← your profile (just created)

  This lives in the shared memory repo. Anyone who
  starts a session sees your handoff in /activity.
  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
```

**Completion actions (run steps 1-5 in parallel, then gate on verification before 6-8):**

1. Creates `memory/people/{username}.md`:
   ```markdown
   # {display_name}
   GitHub: {github_username}
   Role: {role}
   Focus: (not yet collected)
   Work style: (not yet collected)
   Joined: {YYYY-MM-DD}
   ```
2. Updates `egregore.md` Members section — append after `## Members` heading:
   ```markdown
   ### {display_name}
   {role_label}. Joined {YYYY-MM-DD}.
   ```
3. Commits + pushes memory repo: `cd memory && git add -A && git commit -m "Add {username}" && git push && cd -`
4. MERGE Person node in Neo4j (connected mode only) — same two-step approach as current implementation (check name uniqueness, then MERGE on github field)
5. Sync to Supabase (connected mode only) — `POST /api/user/ensure` with all available fields

**Verification gate (after steps 1-3):**
```bash
PEOPLE_FILE="memory/people/${USERNAME}.md"
if [ -f "$PEOPLE_FILE" ] && grep -q '^Role:' "$PEOPLE_FILE" 2>/dev/null; then
  echo "people_file:ok"
else
  echo "people_file:missing"
fi
```
- `people_file:ok` → proceed to steps 6-8
- `people_file:missing` → set `onboarding.phase = "first_todo"` (retry next session). Tell user: "Almost done, but your profile didn't save. Try `/handoff` again next session — your answers are saved."

6. Sets `onboarding_complete: true`, sets consent defaults (see Consent Defaults below)
7. Shell alias via `bin/ensure-shell-function.sh`
8. Telemetry: `onboarding_complete` event (fire-and-forget)

**State update:** `onboarding_complete = true`, `onboarding.phase = "complete"`, `onboarding.completed_at`

> "You're in. From now on, just type `{alias}` in any terminal to launch."

## Consent Defaults

Between onboarding start and explicit consent collection, the system needs working defaults. These flags are read immediately by `bin/telemetry.sh` and `bin/transcript-archive.sh`.

**Policy: default to all-on.** Set these in `.egregore-state.json` during FIRST_TODO:
```json
{
  "session_tracking": true,
  "transcript_sharing": true,
  "telemetry": true,
  "contact_preference": "all",
  "consent_collected": false
}
```

`consent_collected: false` signals that these are defaults, not explicit choices. When consent is later collected (see Progressive Profile Collection), `consent_collected` flips to `true` and the individual flags update to the user's actual preferences.

## Progressive Profile Collection

Everything beyond name and role is collected inline during the first few sessions, when contextually relevant:

| Data | Collected when | How |
|------|---------------|-----|
| **Name** | FIRST_TODO | AskUserQuestion with github_name as default |
| **Role** | FIRST_HANDOFF | AskUserQuestion: Engineering/Design/Research/Operations |
| **Interests/focus** | First `/quest` or `/activity` (2nd+ session) | "What areas interest you most?" with options derived from `egregore.md → Collaboration` |
| **Work style** | First `/ask` or collaboration (2nd+ session) | "Do you prefer async or real-time?" |
| **Consent** | Second session, first command that shares data | "Quick privacy check — Egregore tracks sessions and shares handoffs with the team by default. You can change this anytime via `/telemetry`. All good?" |

### `profile_fields_collected` schema

Array of field name strings in `.egregore-state.json`:

```json
{
  "profile_fields_collected": ["name", "role"]
}
```

Valid field names: `"name"`, `"role"`, `"focus"`, `"work_style"`, `"consent"`

Commands check this before asking profile questions:
```bash
COLLECTED=$(jq -r '.profile_fields_collected // [] | join(",")' .egregore-state.json 2>/dev/null)
```

If the needed field is not in the list, ask inline. After collecting, append to the array and update the people file + graph node.

### Progressive collection hooks — implementation scope

The hooks in `/quest`, `/activity`, `/ask` are **separate PRs** from the core onboarding redesign. The core PR delivers: VERIFY → ORIENT → FIRST_TODO → FIRST_HANDOFF with name + role collection and consent defaults.

Follow-up PRs (tracked as todos):
- PR 2: Add `focus` collection hook to `/quest` and `/activity`
- PR 3: Add `work_style` collection hook to `/ask`
- PR 4: Add `consent` collection to second-session startup

Each follow-up PR adds a ~5-line check at the top of the command: read `profile_fields_collected`, check if field is present, if not ask via AskUserQuestion, update state + people file. No shared interface needed — each command does its own check.

**Rule:** Never more than one profile question per session.

## Technical Orientation (onboarding only)

Visual explanations shown after key actions, only during onboarding (`onboarding_complete: false`).

**Rules:**
- Uses real paths/branch names from the current session — never dummy data
- Compact: 6-8 lines max inside the dotted frame
- Shown after the action completes, not before
- Dotted-line frame (`┄`) to visually separate from conversation
- Skipped once `onboarding_complete: true`

## Resumption

If session drops mid-onboarding, resume from saved phase:

- `phase = "orient"` → re-show the narration, ask what they're working on
- `phase = "first_todo"` → they have a name and branch. Claude says: "Welcome back! Last time you were working on {context from git log/branch}. Before we continue — let's capture what you did last session." Then runs FIRST_HANDOFF for the previous session's work.
- `phase = "complete"` → validate people file exists with Role field:
  ```bash
  USERNAME=$(jq -r '.github_username // empty' .egregore-state.json 2>/dev/null)
  PEOPLE_FILE="memory/people/${USERNAME}.md"
  HAS_ROLE=$(grep -c '^Role:' "$PEOPLE_FILE" 2>/dev/null || echo 0)
  ```
  - `HAS_ROLE > 0` → "You're already set up. Run `/me` to update your profile." Stop.
  - `HAS_ROLE = 0` → reset `onboarding.phase = "first_todo"`, resume from there.

Collected data (name, role, display_name) is preserved in state across sessions — never re-asked.

## Local Mode

All graph/API/Supabase calls skipped silently. The canonical records are:
- `memory/people/{username}.md` (person file)
- `memory/handoffs/{date}-{slug}.md` (handoff file)
- `.egregore-state.json` (local state)

These persist even if the API is down.

## What This Removes

- HARVEST_IDENTITY state (2-question form)
- HARVEST_CONNECTION state (2-question form)
- CONSENT state (multi-select form)
- The concept of a separate "information collection phase"

## What This Adds

- Technical orientation visuals (branch diagram, memory diagram)
- `profile_fields_collected` tracking in state
- `consent_collected` flag with safe defaults
- FIRST_HANDOFF interception logic (session-end intent detection with disambiguation)
- Verification gate before setting `onboarding_complete: true`

## Implementation Scope

**Core PR (this work):**
- Rewrite `.claude/commands/onboarding.md` with new state machine
- Update `bin/session-start.sh` to handle new phases
- Add `profile_fields_collected` and `consent_collected` to state schema

**Follow-up PRs (separate, tracked as todos):**
- PR 2: Progressive `focus` collection in `/quest` and `/activity`
- PR 3: Progressive `work_style` collection in `/ask`
- PR 4: Explicit consent collection on second session startup

## Migration

**Existing users** (`onboarding_complete: true`): Unaffected. No changes to their experience.

**Mid-onboarding users** (any old phase like `harvest_identity`, `harvest_connection`, `consent`):
- Reset to `phase = "orient"` — they go through the new flow
- Preserved from state: `display_name`, `name`, `github_username`, `github_name` (never re-asked)
- Preserved from state: any consent flags already set (`session_tracking`, `telemetry`, etc.) — NOT wiped. If they already consented in the old flow, those flags stay as-is and `consent_collected` is set to `true`.
- New fields added: `profile_fields_collected` populated with whatever data already exists (e.g., if name is set, includes `"name"`)

**Detection logic** (in `session-start.sh` or onboarding command):
```bash
PHASE=$(jq -r '.onboarding.phase // empty' .egregore-state.json 2>/dev/null)
OLD_PHASES="welcome harvest_identity harvest_connection consent"
if echo "$OLD_PHASES" | grep -qw "$PHASE"; then
  # Migrate: preserve existing data, reset phase
  jq '.onboarding.phase = "orient"' .egregore-state.json > .tmp && mv .tmp .egregore-state.json
  # Build profile_fields_collected from existing state
  FIELDS="[]"
  [ "$(jq -r '.display_name // empty' .egregore-state.json)" ] && FIELDS=$(echo "$FIELDS" | jq '. + ["name"]')
  [ "$(jq -r '.onboarding.harvest_rounds[0].answers.role // empty' .egregore-state.json)" ] && FIELDS=$(echo "$FIELDS" | jq '. + ["role"]')
  [ "$(jq -r '.onboarding.consent // empty' .egregore-state.json)" != "null" ] && FIELDS=$(echo "$FIELDS" | jq '. + ["consent"]')
  jq --argjson f "$FIELDS" '.profile_fields_collected = $f' .egregore-state.json > .tmp && mv .tmp .egregore-state.json
fi
```
