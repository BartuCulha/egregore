Analyze meetings from Granola. Adaptive approach — the agent reads the material, asks what matters, and decides how to analyze.

Arguments: $ARGUMENTS (Optional: "sync" for batch mode, "backfill" to re-process historical meetings, or search term to find a specific meeting)

## Usage

- `/meeting` — Interactive: list recent unprocessed meetings, pick one
- `/meeting sync` — Batch: process all unprocessed meetings from configured folders
- `/meeting [search]` — Find and process a specific meeting by title
- `/meeting backfill` — Re-process already-ingested meetings with richer extraction

## Orientation

Three principles, not fixed constraints:

1. **The user knows what matters** — ask before analyzing. "Just give me the action items" and "help me understand the political dynamics" should produce fundamentally different analyses.
2. **The material tells you how to read it** — let inputs shape approach. A 10-minute standup and a 90-minute strategy session call for different depth.
3. **The output schema is the only constraint** — everything else is yours to decide. The graph needs structure to index against. Everything between "load context" and "write files" is the free zone.

The output schema exists because the graph needs structure. Everything between "load context" and "write files" is yours.

You are the analyst. The meeting is your material. The user is your client. The graph is your institutional memory. Do good work.

## Intent Harvesting

**Before you touch the transcript, ask the user what they need.**

Use 2-3 quick AskUserQuestion prompts, adapted to context — not a fixed questionnaire. Examples:

- What matters most from this meeting?
- Full depth or quick extraction?
- Connect to prior threads or fresh analysis?
- Anything I should know going in?

The intent shapes everything: which lenses to apply, how deep to go, what output sections to emphasize.

- "just give me the action items" → lightweight pass, actions-focused, skip dynamics/continuity
- "I need to understand the political dynamics" → deep dynamics lens, speaker-by-speaker conviction mapping
- "what changed from last time?" → continuity-heavy, evolution focus
- "full analysis" or no strong preference → balanced multi-lens approach

**Skip conditions** — don't harvest intent when:
- User already gave rich context with their invocation (e.g., `/meeting sync` or `/meeting [search term] just action items`)
- Batch/sync mode — use adaptive defaults (balanced approach)
- Backfill mode — historical, no user to ask

## Assessment Lenses

A library of ways to read, not a checklist to complete. Reach for whichever lenses the material and user intent call for.

### Substance (what was said)

Priorities, dependencies, events, evidence-backed positions, tradeoffs, open questions, confidence signals.

Classification vocabulary:

| Category | Signals | Example |
|----------|---------|---------|
| **decision** | "we decided", "let's go with", explicit choices, "the plan is" | "Use stdio for MCP transport" |
| **finding** | "turns out", "we discovered", "interesting that", realizations | "Neo4j HTTP is faster than Bolt" |
| **pattern** | "every time we", "I keep seeing", recurring themes | "All pricing discussions converge on usage-based gating" |
| **action** | "I'll do", "[name] will", "next step is", "TODO" | "Oz will write the MCP spec by Friday" |
| **unknown** | Referenced but not elaborated — gaps to fill | "Pricing discussion" |

Key questions: What was actually decided? What depends on what? What evidence supports it?

### Dynamics (how it was said)

Tone arc, conviction strength, alignment/divergence, interpersonal patterns, energy shifts.

Key questions: Where did energy spike or drop? What was stated with conviction vs explored tentatively? Who drove and who followed?

### Continuity (where it fits)

Decision evolution, topic recurrence, open threads addressed or dropped, meta-patterns, organizational arc.

Key questions: What shifted from previous meetings? What keeps recurring? What was dropped?

### Criticality (what's at stake)

Tensions between stated positions and revealed behavior. Contradictions within a single speaker's framing. Risks acknowledged vs risks visible but unspoken. The gap between what people say they'll do and the energy they bring to it.

Key questions: Where does stated confidence diverge from actual tone? What risks are visible but unnamed? What contradictions did nobody call out?

---

These are ways of reading, not agents to spawn or boxes to fill. A quick action-item extraction might only need Substance. A politically complex meeting might need all four. The user's intent and the material itself tell you which lenses matter.

## Analysis Toolkit

What's available (tools, not steps):

- **Inline analysis**: Read all inputs, produce output directly. Cheapest, fastest.
- **Scaffold-first**: Panel → lightweight scaffold → guided transcript reading. Good middle ground.
- **Parallel sub-agents**: Spawn via Task tool for dimension-specific deep analysis. Use for long/complex meetings.
- **Selective lenses**: Skip what doesn't apply. A standup doesn't need Criticality.
- **Hybrid**: Mix approaches freely. Scaffold inline, then spawn one agent for the lens that needs depth.

Factors to consider (guidance, not rules):

| Factor | Implication |
|--------|-------------|
| Transcript length (>30K chars) | Sub-agents may help for long transcripts |
| Transcript length (<10K chars) | Inline is probably sufficient |
| Graph context density | Continuity lens value scales with context |
| Meeting complexity (attendees, threads) | More complex → more analysis depth |
| User intent (quick vs deep) | May collapse toward inline |
| Cost sensitivity | Budget-aware → inline |

## Output Schema

The only true constraint. The graph needs structure.

### Enriched Artifact Schema

Each extracted artifact gets these fields:

| Field | Source | Description |
|-------|--------|-------------|
| **category** | Analysis | decision / finding / pattern / action |
| **title** | Analysis | Short descriptive title |
| **content** | Analysis | Full description with substance |
| **context** | Analysis | What conversation led to this |
| **rationale** | Analysis | The "why" behind it |
| **tradeoffs** | Analysis | What was considered and rejected |
| **confidence** | Analysis | 0.0-1.0 based on evidence strength |
| **speaker** | Analysis | "me" (microphone) vs "them" (system) |
| **panel_corroborated** | Analysis | Whether panel also captured this |
| **topics** | Analysis | 2-5 tags for quest linking |
| **open_questions** | Analysis | Raised but unresolved |
| **evidence_quote** | Analysis | Supporting quote (max 120 chars) |
| **urgency** | Analysis | high / medium / low / null |
| **importance** | Analysis | high / medium / low / null |
| **conviction_strength** | Analysis | assertion / hypothesis / exploration / null |
| **conviction_challenged** | Analysis | Was this challenged in discussion? |
| **evolution_type** | Analysis | new / shifted / reinforced / reversed / null |
| **evolution_context** | Analysis | What it supersedes/reinforces |
| **related_extracts** | Analysis | Cross-references between items [{id, relationship}] |

Confidence calibration: strong_agreement=0.9, data_backed=0.9, single_speaker=0.7, exploratory=0.5, contentious=0.6.

### Meeting Intelligence Briefing

```markdown
# Meeting Intelligence: {Title}

**Date**: YYYY-MM-DD
**Attendees**: {names}
**Source**: Granola ({doc-id})
**Approach**: {description of analysis approach chosen}
**Intent**: {what the user asked for}
**Tone**: {tone description} | Alignment: {score}

## Meta-Analysis

{3-5 paragraphs of opinionated analysis. Structure around four lenses:}

{1. **Heart of the conversation.** Not the agenda — the gravitational center. What were people circling, building toward, or working through? What question was actually being answered, even if nobody framed it that way? First paragraph, get there fast.}

{2. **What's new.** What emerged that wasn't obvious going in? A shift in thinking, a constraint that surfaced, a convergence nobody declared. If nothing genuinely new emerged, say so — "execution meeting, no new ground" is a valid and useful finding. Don't manufacture novelty.}

{3. **Actuality frame.** Given what we know about the team's current state — active quests, recent decisions, open threads from previous meetings — where does this meeting land? What does it change, accelerate, or block? Connect to live organizational state. Reference specific quests, prior decisions, or open threads by name when relevant.}

{4. **Recommended considerations.** Sharp, specific. Not "continue exploring X" — what concretely should happen, who should act, and why it matters now. Distinguish between urgent (blocks other work) and important (shapes direction).}

## Tone & Energy

{Only if Dynamics lens was applied}
- Overall: {tone description}
- Arc: {toneArc}
- Key moments:
  - {moment}: "{quote}" — {speaker}

## Priorities

| Item | Urgency | Importance | Owner | Evidence |
|------|---------|------------|-------|----------|
| ... | high | high | them | "quote" |

## Dependencies

- {blocker} → {blocked} ({owner})

## Dynamics

{Only if Dynamics lens was applied}
- Pattern: {pattern description}
- Alignment: {score}
- Tensions: {if any}

## Convictions

{Only if Dynamics lens was applied}
| Statement | Speaker | Strength | Evidence |
|-----------|---------|----------|----------|
| ... | me | assertion | "quote" |

## Decision Evolution

{Only if Continuity lens was applied and cross-meeting data exists}
- **{topic}**: {previous} → {current} ({trajectory})

## Cross-Meeting Patterns

{Only if Continuity lens found patterns}
- {topic}: discussed {N}x, {trajectory description}

## Analytical Tensions

{Places where different lenses point in different directions, or self-contradictions within a single speaker's framing. Not "cross-agent disagreement" — real tensions:}
{- stated confidence vs actual energy}
{- content assertions vs organizational history}
{- risks acknowledged vs risks visible but unspoken}
{Example: "Charlie advocates empowerment philosophy but the Windows accessibility gap means users can't actually act on it"}

{If no tensions exist, omit this section.}

## Actions

{Extracted, not turned into Artifact files}
| Owner | Description | Evidence |
|-------|-------------|----------|
| ... | ... | "quote" |

## Artifacts Extracted

{List of artifacts with confidence + quest links}

## Open Threads

{Unresolved items carried forward}
```

**Register:** A trusted colleague who attended the meeting and is briefing someone who wasn't there. Direct, opinionated, doesn't waste words on obvious things. Prioritizes insight over completeness.

**Anti-patterns:**
- "The meeting covered several important topics..." → you have nothing to say
- Restating what's already in the artifact list → the list exists for that
- Equal weight to everything discussed → prioritize ruthlessly
- "It would be good to follow up on..." → who, what, by when, or don't say it
- Ignoring graph context → the actuality frame is what distinguishes this from any summary tool
- Burying the lead → heart of the conversation goes first, not last

### Actions

Extracted, not turned into Artifact files. Schema per action:
- **owner**: who is responsible
- **description**: what they committed to
- **evidence_quote**: supporting quote (max 120 chars)

## Quality Standards

- No trivia extraction — only knowledge worth preserving
- Panel corroboration → `panel_corroborated: true`, transcript-only → `false` + confidence ≤ 0.7
- Evidence quotes: max 120 chars, prioritize signal over length
- Speaker attribution: microphone = "me", system = "them"
- Confidence calibration: strong_agreement=0.9, data_backed=0.9, single_speaker=0.7, exploratory=0.5, contentious=0.6
- Don't extract small talk, logistics, or trivia
- If a scaffold item has no transcript discussion at all, omit it (don't fabricate)

## Infrastructure

Mechanical steps that stay precise. Exact Cypher queries, exact file paths, exact state management.

### Step 0: Config check

Check if Granola is available:
```bash
bash bin/granola.sh test
```

If it fails (Granola not installed), stop with:
> Granola not found on this machine. Install Granola and record some meetings first.

Read folder config from `.egregore-state.json`:
```bash
jq -r '.granola_folders // empty' .egregore-state.json
```

**If `granola_folders` is not set** — first-time folder selection:
1. List available folders:
   ```bash
   bash bin/granola.sh folders
   ```
2. Present folders via AskUserQuestion:
   ```
   question: "Which Granola folders should Egregore watch for meetings?"
   header: "Folders"
   multiSelect: true
   options: [list of folders from output]
   ```
3. Save selected folders to `.egregore-state.json` under `granola_folders` (array of folder names).

### Step 0.5: Route subcommand

If `$ARGUMENTS` is `backfill`, jump to **Backfill Mode** (bottom of this document).

### Step 1: Fetch unprocessed meetings

Read processed meeting IDs:
```bash
jq -r '.processed_meetings // {} | keys[]' .egregore-state.json
```

Build exclude list (comma-separated IDs), then fetch from each configured folder:
```bash
bash bin/granola.sh list --folder "FolderName" --exclude "id1,id2,..."
```

If `$ARGUMENTS` is a search term (not "sync", not "backfill", and not empty), use `bash bin/granola.sh search "$ARGUMENTS"` to find matches across configured folders.

### Step 2: Select meetings

**Interactive mode** (no arguments or search term):
Present unprocessed meetings via AskUserQuestion:
```
question: "Which meeting should I process?"
header: "Meeting"
options:
  - label: "Meeting Title (Feb 12)"
    description: "With: Alice, Bob — 45 min"
  - label: "Another Meeting (Feb 11)"
    description: "With: Carol — 30 min"
```

**Sync mode** (`/meeting sync`):
Process all unprocessed meetings. Show count:
> Processing N unprocessed meetings...

**Search mode** (`/meeting [search]`):
If exactly one match, use it. If multiple, present via AskUserQuestion. If none:
> No meetings found matching "[search]".

### Step 3: Fetch meeting data

```bash
bash bin/granola.sh get <doc-id>
```

Parse the output JSON. You now have: `panel_text`, `transcript_text`, `transcript_structured`, `title`, `date`, `attendees`.

### Step 4: Load cross-meeting context (single batch call)

Run ALL 5 queries in a **single `bash bin/graph-batch.sh` call** (1 network round-trip instead of 5). Parse `results[0]` through `results[4]`.

```bash
bash bin/graph-batch.sh '[
  {"statement": "MATCH (a:Artifact) WHERE a.origin STARTS WITH '"'"'granola:'"'"' AND a.created >= datetime() - duration('"'"'P30D'"'"') RETURN a.id, a.title, a.type, a.topics, a.meetingTitle, a.meetingDate, a.confidence ORDER BY a.created DESC LIMIT 15"},
  {"statement": "MATCH (a:Artifact) WHERE a.origin STARTS WITH '"'"'granola:'"'"' AND a.openQuestions IS NOT NULL RETURN a.title, a.openQuestions, a.meetingTitle, a.meetingDate ORDER BY a.created DESC LIMIT 10"},
  {"statement": "MATCH (a:Artifact) WHERE a.origin STARTS WITH '"'"'granola:'"'"' AND a.created >= datetime() - duration('"'"'P60D'"'"') UNWIND a.topics AS topic WITH topic, count(DISTINCT a.meetingTitle) AS meetingCount, collect(DISTINCT a.meetingTitle)[..5] AS meetings WHERE meetingCount >= 2 RETURN topic, meetingCount, meetings ORDER BY meetingCount DESC LIMIT 10"},
  {"statement": "MATCH (newer:Artifact)-[:SUPERSEDES]->(older:Artifact) WHERE newer.created >= datetime() - duration('"'"'P60D'"'"') RETURN newer.title AS current, older.title AS previous, newer.topics, newer.meetingTitle ORDER BY newer.created DESC LIMIT 10"},
  {"statement": "MATCH (q:Quest {status: '"'"'active'"'"'}) WHERE q.topics IS NOT NULL RETURN q.id, q.title, q.topics"}
]'
```

Result mapping:
- `results[0]` → Q1: Recent meeting artifacts (30d)
- `results[1]` → Q2: Open questions from previous meetings
- `results[2]` → Q3: Topic recurrence (60d)
- `results[3]` → Q4: Decision evolution chains
- `results[4]` → Q5: Active quests (for topic linking)

**If the batch call fails**, continue without cross-meeting context. It's enrichment, not required.

### Step 5: Resolve attendees

Read the attendee map:
```bash
jq -r '.attendee_map // {}' .egregore-state.json
```

For each attendee from the meeting data, look up their graph name in the map. If an attendee is NOT in the map, auto-derive from email before asking:

1. **If attendee has email**: derive short name = email local part before `@` (e.g., `bartu@gizatech.xyz` → `bartu`)
2. **Check graph**: `MATCH (p:Person) WHERE p.name = $derived RETURN p` — if person exists, auto-map silently
3. **If name is clean** (alphanumeric, not generic like "info" or "admin"): auto-map and show confirmation line (no AskUserQuestion): `Auto-mapped: {full name} → {derived} (from email)`
4. **Only use AskUserQuestion if**: no email available, email local part is ambiguous/generic, or multiple candidates exist

Save all new mappings to `.egregore-state.json` under `attendee_map`.

Use `"me"` for the user (from `.egregore-state.json` → `name`) and `"them"` for other attendees in speaker attribution.

### Step 6: Intent harvesting

**Per the Intent Harvesting section above.** Use AskUserQuestion to understand what the user needs from this meeting before analysis begins.

**Skip in sync mode** — use adaptive defaults (balanced approach).
**Skip in backfill mode** — historical, no user to ask.
**Skip if user already gave rich context** with their invocation.

### Step 7: Analyze

The free zone.

Apply the lenses that serve the user's intent and the material. Use the toolkit however you judge appropriate. Record what approach you chose.

Guidelines:
- If spawning sub-agents, use Task tool with `model: "sonnet"` and `subagent_type: "general-purpose"`. Do NOT use `run_in_background: true` — run as standard parallel Task calls.
- Give each sub-agent: the relevant inputs for its lens, the output schema it needs to fill, and clear instructions.
- For inline analysis: read all inputs yourself and produce the output directly.
- For hybrid: scaffold inline, then spawn agents for specific lenses that need depth.
- Whatever approach you choose, the output must conform to the Enriched Artifact Schema and Meeting Intelligence Briefing format.

### Step 8: Present proposal

## Active Quests

{INSERT Q5 RESULTS — or "No active quests." if empty}

## Transcript

{INSERT TRANSCRIPT — use transcript_structured if available, otherwise transcript_text}

## Instructions

Analyze the transcript through the lens of SUBSTANCE: what was said, what matters, what depends on what, what's happening in the world around this meeting.

Return a JSON object with this structure:

{
  "priorities": [
    {"item": "...", "description": "free-form: why this matters, what makes it urgent",
     "urgency_tag": "high|medium|low|null", "importance_tag": "high|medium|low|null",
     "evidence_quote": "...", "speaker": "me|them"}
  ],
  "dependencies": [
    {"description": "free-form: what blocks what, why, and current state",
     "blocker": "...", "blocked": "...", "owner": "me|them|null",
     "evidence_quote": "..."}
  ],
  "events": [
    {"description": "free-form: what happened or is happening, internal or external",
     "type_tag": "external|internal|null",
     "evidence_quote": "..."}
  ],
  "enrichments": [
    {"scaffold_id": "s1|new", "category": "decision|finding|pattern|action",
     "title": "...", "brief": "...", "evidence_quote": "...",
     "speaker": "me|them", "tradeoffs": ["Pro: ...", "Con: ..."],
     "context": "...", "open_questions": ["..."],
     "confidence_description": "free-form: how confident are the speakers, what's the basis",
     "confidence_tag": "strong_agreement|data_backed|single_speaker|exploratory|contentious|null"}
  ],
  "_raw_notes": "Multi-paragraph prose: your full read on substance, priorities, dependencies, and events. Include reasoning, hedges, and observations that don't fit the structured fields. Note any connections to the open questions or quests provided."
}

## Rules

1. Match scaffold items to transcript segments. For gap items (gap: true), try especially hard to find evidence.
2. Extract the richest quote — prioritize signal over length. Max 120 chars per quote.
3. Classify speaker: "microphone" source = "me", "system" source = "them".
4. Find items the panel missed — things discussed substantively but not in the scaffold. Use scaffold_id: "new".
5. Do NOT extract small talk, logistics, or trivia.
6. If a scaffold item has no transcript discussion at all, omit it from enrichments (don't fabricate).
7. Tag fields (*_tag) are optional suggestions. If no predefined tag fits, leave it null and let the description carry the signal.
8. For open_questions: flag questions from previous meetings (provided above) that this meeting addresses.
9. The _raw_notes section is critical — this is where your nuanced analysis goes. Don't skimp on it.

Return ONLY valid JSON. No markdown fences, no explanation.
```

##### Agent 2: Dynamics Analyst

**Input**: transcript (structured or text) + attendee names. NO scaffold, NO prior context (fresh read).

**Task tool prompt**:

```
You are the Dynamics Analyst for a meeting analysis pipeline. Your job is to read HOW things were said — tone, energy, convictions, interpersonal dynamics. You receive NO prior context intentionally — we want a fresh read without anchoring.

## Attendees

{INSERT ATTENDEE LIST with graph names, e.g. "cem (me), renc (them)"}

## Transcript

{INSERT TRANSCRIPT — use transcript_structured if available, otherwise transcript_text}

## Instructions

Analyze the transcript through the lens of DYNAMICS: how people interacted, what the emotional texture was, what convictions were expressed and how strongly.

Return a JSON object with this structure:

{
  "tone": {
    "description": "free-form: overall emotional texture of the meeting and how it evolved",
    "arc": "free-form: the emotional trajectory from start to end",
    "moments": [
      {"description": "free-form: what happened and what it felt like",
       "quote": "...", "speaker": "me|them"}
    ]
  },
  "dynamics": {
    "description": "free-form: who drove the conversation, how they interacted, where they aligned or diverged",
    "pattern_tag": "collaborative_building|tension_then_alignment|one_sided|brainstorming|debate|null",
    "alignment_estimate": 0.85,
    "tension_points": [
      {"description": "free-form: what the tension was about and how it played out",
       "quote": "..."}
    ]
  },
  "convictions": [
    {"statement": "...", "speaker": "me|them",
     "description": "free-form: how strongly held, what it's based on, whether challenged",
     "strength_tag": "assertion|hypothesis|exploration|null",
     "quote": "..."}
  ],
  "_raw_notes": "Multi-paragraph prose: your full read on interpersonal dynamics, emotional undercurrents, power dynamics, and anything that doesn't reduce to fields. What was unsaid? Where did energy spike or drop? Who was driving and who was following?"
}

## Rules

1. Read tone from word choice, pacing, emphasis patterns, and conversational flow — not just content.
2. Classify speaker: "microphone" source = "me", "system" source = "them".
3. alignment_estimate: 0.0 (total disagreement) to 1.0 (perfect sync). Base it on actual evidence.
4. Tag fields (*_tag) are optional. If no predefined tag fits, leave null and let description carry the signal. New tags welcome if they capture something the predefined ones don't.
5. Convictions: distinguish between things stated as fact (assertion), things proposed tentatively (hypothesis), and things thrown out for discussion (exploration).
6. The _raw_notes section is where your real analysis lives. Be honest about uncertainty.

Return ONLY valid JSON. No markdown fences, no explanation.
```

##### Agent 3: Continuity Analyst

**Input**: panel_text + scaffold from Pass 0 + Q1 results (recent artifacts) + Q2 results (open questions) + Q3 results (topic recurrence) + Q4 results (decision evolution). NO transcript (keeps it cheap and focused).

**Task tool prompt**:

```
You are the Continuity Analyst for a meeting analysis pipeline. Your job is to compare what THIS meeting covers against what the organization already knows. You read the panel summary (not the transcript) and cross-reference it with historical graph data.

## Panel Summary

{INSERT PANEL_TEXT}

## Scaffold (extracted items from this meeting)

{INSERT SCAFFOLD JSON}

## Recent Meeting Artifacts (30 days)

{INSERT Q1 RESULTS — or "No recent meeting artifacts." if empty}

## Open Questions from Previous Meetings

{INSERT Q2 RESULTS — or "No previous open questions." if empty}

## Topic Recurrence (60 days)

{INSERT Q3 RESULTS — or "No recurring topics found." if empty}

## Decision Evolution Chains

{INSERT Q4 RESULTS — or "No decision evolution chains found." if empty}

## Instructions

Analyze how this meeting fits into the arc of the organization's recent work. What evolved? What recurred? What threads were picked up or dropped?

Return a JSON object with this structure:

{
  "decision_evolution": [
    {"topic": "...", "description": "free-form: how this position has changed across meetings and why",
     "current_position": "...",
     "previous_positions": [{"meeting": "...", "position": "...", "artifact_id": "..."}],
     "trajectory_tag": "shifted|reinforced|reversed|refined|null"}
  ],
  "recurring_topics": [
    {"topic": "...", "description": "free-form: what's happening with this theme over time",
     "meetings_count": 4, "first_seen": "..."}
  ],
  "open_threads": [
    {"description": "free-form: what thread from a previous meeting was addressed, continued, or dropped here",
     "status": "addressed|continued|dropped|null",
     "from_meeting": "...", "artifact_id": "..."}
  ],
  "meta_patterns": [
    {"description": "free-form: organizational-level pattern observed across meetings (convergence, oscillation, drift, etc.)"}
  ],
  "_raw_notes": "Multi-paragraph prose: your full read on how this meeting fits into the arc of the organization's evolution. Connections, tensions, and trajectories that don't fit the structured fields."
}

## Rules

1. Only reference data actually present in the graph context provided. Do not fabricate history.
2. If graph context is sparse (few or no previous artifacts), say so in _raw_notes and focus on what CAN be compared.
3. trajectory_tag is optional. Use it when the pattern is clear, leave null when ambiguous.
4. open_threads: specifically check if any of the "Open Questions from Previous Meetings" were addressed in this meeting's scaffold items.
5. meta_patterns: look for org-level dynamics (e.g., "pricing keeps being revisited" or "team is converging on agent-first architecture").
6. The _raw_notes section is where your real analysis lives. Be honest about confidence levels.

Return ONLY valid JSON. No markdown fences, no explanation.
```

**Parse all 3 agent results**: Extract the JSON from each response. If an agent returns invalid JSON or fails, log the failure and continue with whatever agents succeeded. The synthesis step works with partial input.

#### Step 3e: Synthesis (Opus, inline)

Read the 3 agent outputs (~15K total) + panel_text (~2K) + scaffold. Produce two things:

##### 1. Enriched artifact list

For each scaffold item (and new items from Substance enrichments), merge dimensional data from all three agents:

- **From Substance**: enrichments (evidence, tradeoffs, context, open_questions, confidence), priorities, dependencies, events
- **From Dynamics**: conviction strength for relevant items, tone context, speaker dynamics
- **From Continuity**: evolution context (does this item supersede or reinforce a previous position?)

Each merged artifact gets:
- `category`: from scaffold (or Substance for new items)
- `title`: from scaffold (or Substance)
- `content`: synthesized from scaffold brief + Substance context + Continuity evolution
- `context`: from Substance enrichment
- `rationale`: synthesized from Substance tradeoffs + Dynamics conviction context
- `tradeoffs`: from Substance
- `confidence`: mapped from Substance `confidence_tag` — strong_agreement=0.9, data_backed=0.9, single_speaker=0.7, exploratory=0.5, contentious=0.6
- `speaker`: from Substance enrichment
- `open_questions`: from Substance enrichment
- `evidence_quote`: from Substance enrichment
- `panel_corroborated`: true if from scaffold, false if new from Substance
- `topics`: 2-5 tags derived from content
- `conviction_strength`: from Dynamics (assertion/hypothesis/exploration/null)
- `conviction_challenged`: from Dynamics tension_points (boolean)
- `urgency`: from Substance priorities (high/medium/low/null)
- `importance`: from Substance priorities (high/medium/low/null)
- `evolution_type`: from Continuity (new/shifted/reinforced/reversed/null)
- `evolution_context`: from Continuity (what it supersedes/reinforces)
- `related_extracts`: cross-references between items (array of {id, relationship})

Separate **action items** from knowledge artifacts. Actions don't become Artifact files.

##### 2. Meeting Intelligence Briefing

Synthesize all agent outputs into a coherent briefing document. The **Meta-Analysis** section is the most important part — it justifies the pipeline's existence. It's what a reader gets that they wouldn't from skimming the transcript or reading the artifact list.

**Meta-Analysis synthesis guidance:**

Write 3-5 paragraphs of opinionated analysis. Not a summary — a reading of the meeting that tells someone something they wouldn't have figured out on their own.

Structure around four lenses:

1. **Heart of the conversation.** Not the agenda — the gravitational center. What were people circling, building toward, or working through? What question was actually being answered, even if nobody framed it that way? First paragraph, get there fast.

2. **What's new.** What emerged that wasn't obvious going in? A shift in thinking, a constraint that surfaced, a convergence nobody declared. If nothing genuinely new emerged, say so — "execution meeting, no new ground" is a valid and useful finding. Don't manufacture novelty.

3. **Actuality frame.** This is where cross-meeting context earns its keep. Given what we know about the team's current state — active quests, recent decisions, open threads from previous meetings, where work has been progressing — where does this meeting land? What does it change, accelerate, or block in the current reality? Connect the meeting's outcomes to the live state of the organization. Reference specific quests, prior decisions, or open threads by name when relevant. If the Continuity Analyst found evolution or recurrence, weave it in here.

4. **Recommended considerations.** Sharp, specific. Not "continue exploring X" — what concretely should happen, who should act, and why it matters now. Distinguish between urgent (blocks other work) and important (shapes direction). If the meeting surfaced tensions without resolving them, name what needs resolving and by whom.

**Register:** A trusted colleague who attended the meeting and is briefing someone who wasn't there. Direct, opinionated, doesn't waste words on obvious things. Prioritizes insight over completeness.

**Anti-patterns:**
- "The meeting covered several important topics..." → you have nothing to say
- Restating what's already in the artifact list → the list exists for that
- Equal weight to everything discussed → prioritize ruthlessly
- "It would be good to follow up on..." → who, what, by when, or don't say it
- Ignoring graph context → the actuality frame is what distinguishes this from any summary tool
- Burying the lead → heart of the conversation goes first, not last

```markdown
# Meeting Intelligence: {Title}

**Date**: YYYY-MM-DD
**Attendees**: {names}
**Source**: Granola ({doc-id})
**Tone**: {tone description} | Alignment: {score}

## Meta-Analysis

{3-5 paragraphs — heart of the conversation, what's new, actuality frame, recommended considerations. See guidance above.}

## Tone & Energy

{From Dynamics agent}
- Overall: {tone description}
- Arc: {toneArc}
- Key moments:
  - {moment}: "{quote}" — {speaker}

## Priorities

{From Substance agent}
| Item | Urgency | Importance | Owner | Evidence |
|------|---------|------------|-------|----------|
| ... | high | high | them | "quote" |

## Dependencies

{From Substance agent}
- {blocker} → {blocked} ({owner})

## Dynamics

{From Dynamics agent}
- Pattern: {pattern description}
- Alignment: {score}
- Tensions: {if any}

## Convictions

{From Dynamics agent}
| Statement | Speaker | Strength | Evidence |
|-----------|---------|----------|----------|
| ... | me | assertion | "quote" |

## Decision Evolution

{From Continuity agent — only if cross-meeting data exists}
- **{topic}**: {previous} → {current} ({trajectory})

## Cross-Meeting Patterns

{From Continuity agent — only if patterns found}
- {topic}: discussed {N}x, {trajectory description}

## Internal Tensions

{Where analytical lenses DISAGREE — this section is critical signal}
When Opus detects contradictions between agents (e.g., Substance says high confidence but Dynamics reads exploratory tone), record the tension explicitly:
- **{topic}**: {Agent A reads as X}, but {Agent B reads as Y} — {implication}

If no inter-agent tensions exist, omit this section.

## Artifacts Extracted

{List of artifacts with confidence + quest links}

## Open Threads

{Unresolved items carried forward — from Substance open_questions + Continuity open_threads}
```

##### 3. Detect inter-agent tensions

Specifically look for these contradiction patterns:
- Substance says "strong_agreement" but Dynamics reads low alignment → tension
- Substance says "data_backed" but Dynamics reads "exploratory" tone → tension
- Continuity says "reinforced" but Substance extracted a contradicting position → tension
- Dynamics reads high energy/conviction but Substance found no supporting evidence → tension

Record these in the "Internal Tensions" section. These disagreements ARE signal — they mark where stated intentions diverge from actual energy.

#### Step 3f: Present proposal (enhanced format)

Show the meta-analysis preview + merged extractions. The preview is 2-3 sentences from the meta-analysis — the "heart" and "what's new" lenses only, condensed. Full meta-analysis goes in the briefing file.

```
From "Weekly Sync — Feb 12" (Bob + Alice):
Approach: scaffold + inline analysis
Intent: full analysis
Tone: exploratory → decisive | Alignment: 0.85

  This meeting was really about settling the local transport question —
  everything else orbited that decision. The stdio choice unblocks MCP
  integration but creates a deferred problem for remote servers that
  nobody named yet.

  ◉ Decision: Use stdio transport for MCP servers           [0.9]
    "I tested both and stdio is way simpler" — them
    Conviction: assertion | Urgency: high
    Tradeoff: simpler local ↔ no remote support
    Open: how to handle remote MCP servers?
    Evolution: new position
    → mcp-integration

  ◉ Finding: Onboarding needs guided tour                   [0.9]
    "users drop off at step 4 without guidance" — me
    Conviction: data_backed | Urgency: medium
    → onboarding-flow

  ◉ Finding: Usage-based gating > tier gating                [0.5]
    * transcript-only — not in panel notes
    "gate by usage patterns not Claude tier" — them
    Conviction: exploration
    Open: what threshold?
    Evolution: shifted (was tier-based in Feb 8)
    → pricing-strategy

  Tensions:
    * "Usage gating" — stated as exploratory [0.5],
      but speaker energy suggests stronger conviction

  Actions:
    * Oz — write MCP auth spec by Friday
    * Bob — prototype guided tour by Monday

  Cross-refs: Decision (stdio) ← Finding (benchmark results)

Adjust? (y/edit/skip)
```

Display rules:
- `Approach:` line showing what analysis method was used
- `Intent:` line showing what user asked for
- Tone summary + alignment score at the top
- Meta-analysis preview: 2-3 sentences covering "heart" + "what's new". Indented.
- `[0.9]` confidence score right-aligned after title
- Evidence quote in quotes with speaker attribution
- Conviction strength + urgency on one line
- `* transcript-only` flag for items not in panel (confidence < 0.7)
- Tradeoffs shown as `X ↔ Y` on one line when exactly 2, otherwise listed
- Open questions on separate `Open:` line
- Evolution context when available
- `→ quest-id` for linked quests
- Tensions section after artifacts
- Actions listed separately at the end (not artifacts)
- Cross-refs shown if detected

Wait for user response:
- **y** or empty → proceed to reflection checkpoint
- **edit** → user modifies, then proceed
- **skip** → skip this meeting, move to next

**In sync mode**: still present each meeting's proposal for confirmation.

### Step 9: Reflection checkpoint

After the user approves, check if the analysis produced continuity signals worth reflecting on.

**Check for reflection triggers:**

| Signal | Reflection prompt |
|---|---|
| Decision evolution with `shifted` or `reversed` trajectory | "Your position on {topic} has shifted from {old} to {new}. Is this intentional?" |
| Open thread from previous meeting `addressed` | "{Thread} from {meeting} appears to be resolved. Confirm?" |
| Open thread from previous meeting `dropped` | "{Thread} from {meeting} wasn't mentioned. Still relevant, or deprioritized?" |
| Meta-pattern detected | "Pattern detected: {description}. Is this becoming a principle?" |
| No meaningful signals | Skip reflection entirely |

If a meaningful trigger exists, surface it via AskUserQuestion with **specific options drawn from the actual data**:

```
question: "{specific question from analysis}"
header: "Reflect"
options:
  - label: "{specific option 1}"
    description: "{what this means for the artifacts}"
  - label: "{specific option 2}"
    description: "{alternative interpretation}"
  - label: "Skip reflection"
    description: "Save artifacts as-is"
```

**Handle response:**
- Evolution confirmed → create additional decision artifact with SUPERSEDES relationship
- Compatible/clarification → add RELATES_TO relationship
- Open question → create finding with "Open: ..." prefix
- Skip → proceed without additional artifacts

**If no continuity analysis was done or no meaningful signals**, skip the reflection checkpoint entirely. Don't force it.

### Step 10: Write files

#### Meeting Intelligence Briefing

```bash
cat > "memory/meetings/{YYYY-MM-DD}-{slug}.md" << 'MEETINGEOF'
{MEETING INTELLIGENCE BRIEFING CONTENT}
MEETINGEOF
```

File naming: `{YYYY-MM-DD}-{slug}.md` where slug is lowercase, hyphens, max 50 chars, derived from title.

#### Individual artifact files

For each extraction (decisions, findings, patterns — NOT action items), including any additional artifacts from the reflection checkpoint:

```bash
cat > "memory/knowledge/{category}s/{YYYY-MM-DD}-{slug}.md" << 'ARTIFACTEOF'
# {Title}

**Date**: {YYYY-MM-DD}
**Author**: meeting
**Category**: {category}
**Confidence**: {0.0-1.0}
**Source**: {meeting title}
**Topics**: {topic1}, {topic2}
**Conviction**: {assertion|hypothesis|exploration}
**Urgency**: {high|medium|low}

## Context

{What conversation led to this}

## Content

{The actual substance}

## Rationale

{Why this matters}

## Tradeoffs

{Only include if tradeoffs exist}
- **Pro**: {pro}
- **Con**: {con}

## Open Questions

{Only include if open_questions exist}
- {question}

## Evidence

{Only include if evidence_quote exists}
> "{evidence_quote}" — {speaker}

## Evolution

{Only include if evolution context exists}
- Previous: {artifact-id} — {what changed}

## Related

- Quest: {quest-id}
- Related: {related-artifact-id} ({type})
ARTIFACTEOF
```

Omit sections that have no data.

### Step 11: Neo4j batch

Build a JSON array of queries for `bash bin/graph-batch.sh` calls.

**Batch limit: API accepts max 20 queries per `graph-batch.sh` call.** Count total queries before executing. If >20, split into chunks of <=20 and execute sequentially. Recommended split:
- **Batch 1** (nodes): Meeting node + INVOLVES relationships + Artifact nodes (typically <=15)
- **Batch 2** (edges): FROM_MEETING + PART_OF + RELATES_TO + SUPERSEDES relationships

**Meeting node**:
```json
{
  "statement": "MERGE (m:Meeting {id: $meetingId}) SET m.title = $title, m.date = date($date), m.granolaDocId = $docId, m.tone = $tone, m.toneArc = $toneArc, m.dynamicsPattern = $pattern, m.alignmentScore = $alignment, m.filePath = $filePath, m.artifactCount = $count, m.pipeline = 'adaptive', m.topology = $topology, m.agentCount = $agentCount, m.intent = $intent, m.processed = datetime() RETURN m.id",
  "parameters": {
    "meetingId": "meeting-{YYYY-MM-DD}-{slug}",
    "title": "{meeting title}",
    "date": "{YYYY-MM-DD}",
    "docId": "{granola doc id}",
    "tone": "{tone description}",
    "toneArc": "{arc}",
    "pattern": "{dynamics pattern}",
    "alignment": 0.85,
    "filePath": "meetings/{YYYY-MM-DD}-{slug}.md",
    "count": 3,
    "topology": "inline",
    "agentCount": 0,
    "intent": "full analysis"
  }
}
```

The `topology` field is a free-form string describing the approach chosen: `"inline"`, `"scaffold-inline"`, `"scaffold-2agents"`, `"scaffold-3agents"`, `"hybrid"`, etc.
The `agentCount` is the number of sub-agents spawned (0 for inline).
The `intent` is what the user asked for.

**Meeting → Person relationships** (INVOLVES):
```json
{
  "statement": "MATCH (m:Meeting {id: $meetingId}) MATCH (p:Person {name: $personName}) MERGE (m)-[:INVOLVES]->(p)",
  "parameters": {"meetingId": "...", "personName": "{graph name from attendee_map}"}
}
```

**Artifact nodes** (with dimensional properties):
```json
{
  "statement": "MERGE (a:Artifact {id: $artifactId}) SET a.title = $title, a.type = $category, a.topics = $topics, a.filePath = $filePath, a.origin = $origin, a.meetingTitle = $meetingTitle, a.meetingDate = $meetingDate, a.confidence = $confidence, a.speaker = $speaker, a.panelCorroborated = $panelCorroborated, a.urgency = $urgency, a.importance = $importance, a.convictionStrength = $convictionStrength, a.convictionChallenged = $convictionChallenged, a.evolutionType = $evolutionType, a.openQuestions = $openQuestions, a.created = datetime() WITH a OPTIONAL MATCH (p:Person {name: $author}) FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END | MERGE (a)-[:CONTRIBUTED_BY]->(p)) RETURN a.id",
  "parameters": {
    "artifactId": "{YYYY-MM-DD}-{slug}",
    "title": "{title}",
    "category": "{category}",
    "topics": ["topic1", "topic2"],
    "filePath": "knowledge/{category}s/{YYYY-MM-DD}-{slug}.md",
    "origin": "granola:{doc-id}",
    "meetingTitle": "{meeting title}",
    "meetingDate": "{meeting date}",
    "confidence": 0.9,
    "speaker": "them",
    "panelCorroborated": true,
    "urgency": "high",
    "importance": "high",
    "convictionStrength": "assertion",
    "convictionChallenged": false,
    "evolutionType": "new",
    "openQuestions": ["question1"],
    "author": "{short name}"
  }
}
```

**Artifact → Meeting relationships** (FROM_MEETING):
```json
{
  "statement": "MATCH (a:Artifact {id: $artifactId}), (m:Meeting {id: $meetingId}) MERGE (a)-[:FROM_MEETING]->(m)",
  "parameters": {"artifactId": "...", "meetingId": "..."}
}
```

**Quest linking** — before building the batch, detect topic overlap:
```cypher
MATCH (q:Quest {status: 'active'})
WHERE q.topics IS NOT NULL
WITH q, [t IN q.topics WHERE t IN $artifactTopics] AS shared
WHERE size(shared) >= 1
RETURN q.id AS quest, q.title AS title, shared AS sharedTopics
ORDER BY size(shared) DESC LIMIT 3
```

Add PART_OF queries:
```json
{
  "statement": "MATCH (a:Artifact {id: $artifactId}), (q:Quest {id: $questId}) MERGE (a)-[:PART_OF]->(q)",
  "parameters": {"artifactId": "...", "questId": "..."}
}
```

Add cross-extract RELATES_TO queries:
```json
{
  "statement": "MATCH (a:Artifact {id: $artifactId}), (b:Artifact {id: $relatedId}) MERGE (a)-[:RELATES_TO]->(b)",
  "parameters": {"artifactId": "...", "relatedId": "..."}
}
```

Add SUPERSEDES relationship if reflection produced one:
```json
{
  "statement": "MATCH (a:Artifact {id: $newId}), (b:Artifact {id: $oldId}) MERGE (a)-[:SUPERSEDES]->(b)",
  "parameters": {"newId": "...", "oldId": "..."}
}
```

Execute the entire batch:
```bash
bash bin/graph-batch.sh '[{...}, {...}, ...]'
```

Show progress:
```
Creating artifacts...

[1/6] ✓ Writing meetings/2026-02-12-weekly-sync.md (intelligence briefing)
  [2/6] ✓ Writing knowledge/decisions/2026-02-12-use-stdio-mcp-transport.md
        ✓ Writing knowledge/findings/2026-02-12-onboarding-needs-guided-tour.md
        ✓ Writing knowledge/findings/2026-02-12-usage-based-gating.md
  [3/5] ✓ Indexed in knowledge graph (batch: 12 queries)
  [4/5] ✓ Linked to 2 quests
  [5/5] ✓ Auto-saved
```

### Step 12: Mark processed

Update `.egregore-state.json` with processed meeting:

```bash
jq --arg id "$DOC_ID" --arg title "$TITLE" --arg date "$(date +%Y-%m-%d)" --argjson count $ARTIFACT_COUNT '
  .processed_meetings //= {} |
  .processed_meetings[$id] = {
    title: $title,
    processed_at: $date,
    artifacts_created: $count
  }
' .egregore-state.json > tmp.$$.json && mv tmp.$$.json .egregore-state.json
```

### Step 13: Confirmation TUI

Display the confirmation box. ~72 char width. Sigil: `MEETING`.

**Boundary handling (CRITICAL)** — No sub-boxes. Only 4 line patterns:

1. **Top**: `┌` + 70x`─` + `┐` (72 chars)
2. **Separator**: `├` + 70x`─` + `┤` (72 chars)
3. **Content**: `│` + 2 spaces + text + pad to 68 chars + `│` (72 chars)
4. **Bottom**: `└` + 70x`─` + `┘` (72 chars)

### Single meeting:

```
┌──────────────────────────────────────────────────────────────────────┐
│  MEETING                                            cem · Feb 12    │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  "Weekly Sync — Feb 12"                                              │
│  Attendees: Alice, Bob                                               │
│  Approach: scaffold + inline | Intent: full analysis                │
│  Tone: exploratory → decisive | Alignment: 0.85                     │
│                                                                      │
│  3 insights extracted:                                               │
│                                                                      │
│  ◉ Decision: Use stdio transport for MCP servers       [0.9]        │
│    "I tested both and stdio is way simpler" — them                   │
│    Conviction: assertion | Evolution: new                            │
│    → mcp-integration                                                 │
│                                                                      │
│  ◉ Finding: Onboarding needs guided tour               [0.9]        │
│    "users drop off at step 4" — me                                   │
│    Conviction: data_backed | Evolution: reinforced                   │
│    → onboarding-flow                                                 │
│                                                                      │
│  ◉ Finding: Usage-based gating > tier gating           [0.5]        │
│    * transcript-only                                                 │
│    Conviction: exploration | Evolution: shifted                      │
│    → pricing-strategy                                                │
│                                                                      │
│  Intelligence briefing: memory/meetings/2026-02-12-weekly-sync.md   │
│                                                                      │
├──────────────────────────────────────────────────────────────────────┤
│  ✓ Saved · graphed · pushed                                          │
│  Visible in /activity.                                               │
└──────────────────────────────────────────────────────────────────────┘
```

### Batch (sync mode):

```
┌──────────────────────────────────────────────────────────────────────┐
│  MEETING SYNC                                       cem · Feb 12    │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  3 meetings processed, 7 insights extracted:                         │
│                                                                      │
│  "Weekly Sync" — 3 insights (avg conf: 0.77)                        │
│  "Design Review" — 2 insights (avg conf: 0.9)                       │
│  "Sprint Planning" — 2 insights (avg conf: 0.8)                     │
│                                                                      │
│  3 intelligence briefings in memory/meetings/                        │
│                                                                      │
├──────────────────────────────────────────────────────────────────────┤
│  ✓ Saved · graphed · pushed                                          │
│  Visible in /activity.                                               │
└──────────────────────────────────────────────────────────────────────┘
```

### After confirmation

Run the `/save` flow to commit and push changes.

---

## Backfill Mode

Triggered by `/meeting backfill`. Re-processes already-ingested meetings to seed richer graph data.

### Backfill Steps

1. **List candidates**: Read `.egregore-state.json` → `processed_meetings`. Check which ones already have Meeting nodes:
   ```cypher
   MATCH (m:Meeting)
   WHERE m.granolaDocId IN $docIds
   RETURN m.granolaDocId
   ```
   Meetings that already have Meeting nodes are already backfilled — skip them.

2. **Confirm with user**:
   ```
   Found N meetings to backfill:
   - "Meeting Title 1" (Feb 10)
   - "Meeting Title 2" (Feb 8)
   ```
   Via AskUserQuestion:
   ```
   question: "How should I backfill these N meetings?"
   header: "Backfill"
   options:
     - label: "Quick extraction (Recommended)"
       description: "Inline analysis — priorities, evidence, actions. Fast and cheap."
     - label: "Deep analysis"
       description: "Multi-lens with sub-agents. Richer but slower."
     - label: "Cancel"
       description: "Don't backfill"
   ```

3. **Process each meeting**:
   - Fetch via `bash bin/granola.sh get <doc-id>`
   - Agent freedom applies — choose approach per meeting based on user's depth preference
   - No intent harvesting for backfill (historical, no user to ask)
   - No Continuity lens (nothing to compare against for initial backfill)

4. **Patch Neo4j** (idempotent — MERGE, not CREATE):
   - Create Meeting node with `m.pipeline = 'adaptive'`, `m.topology = $topology`, `m.agentCount = $agentCount`
   - Create INVOLVES relationships for attendees
   - Update existing Artifact nodes with new properties (urgency, importance, convictionStrength, etc.)
   - Create FROM_MEETING relationships linking existing artifacts to new Meeting node
   - All via a single `bash bin/graph-batch.sh` call per meeting

5. **Update artifact files** (optional enhancement):
   - If existing artifact files in `memory/knowledge/` are missing the new sections (Evidence, Evolution, Conviction), update them with the analysis data.
   - Use Bash to append sections — don't overwrite existing content.

6. **Show progress per meeting**:
   ```
   Backfilling 3 meetings...

   [1/3] "Meeting Title 1" (Feb 10)
         Approach: inline
         ✓ Analysis complete
         ✓ Meeting node created
         ✓ 3 artifacts enriched

   [2/3] "Meeting Title 2" (Feb 8)
         Approach: scaffold + 2 agents
         ✓ Analysis complete
         ✓ Meeting node created
         ✓ 4 artifacts enriched

   [3/3] ... (etc)

   Backfill complete: 3 meetings, 10 artifacts enriched.
   Continuity lens now has historical context for future meetings.
   ```

---

## Edge Cases

| Scenario | Handling |
|----------|----------|
| Granola not installed | Stop with clear message — no error, just guidance |
| No unprocessed meetings | "All meetings are already processed. Nothing new to ingest." |
| Empty panel + empty transcript | Skip meeting: "No content found for this meeting." |
| Empty panel, has transcript | Transcript-only analysis. All items confidence ≤ 0.7. |
| Has panel, empty transcript | Panel-only analysis. Substance limited to scaffold. |
| Meeting already processed | Skip silently (filtered by --exclude) |
| Neo4j unavailable | Still create files, skip graph ops. Warn: "Graph offline — files saved, will sync on next /save" |
| No quest matches | Create artifacts without quest links (no warning needed) |
| Agent topology produced incomplete output | Retry with different approach (e.g., fall back to inline) |
| User asked for quick extraction | Respect the intent, don't over-analyze |
| No graph context | Continuity lens has limited value, agent may skip it |
| Agent returns invalid JSON | Same as agent failure — continue with whatever succeeded |
| transcript_structured empty | Pass transcript_text instead |
| Memory symlink missing | Error: "Run /setup first — memory not linked" |
| User skips all meetings | "Nothing to process. Run /meeting later when you're ready." |
| Reflection finds no tension | Skip reflection checkpoint entirely — don't force it |
| Graph queries fail during context load | Continue without cross-meeting context — analysis still works, just less informed |
| Unknown attendee in meeting | Auto-derive from email first, AskUserQuestion only if ambiguous |
| Backfill with no processed meetings | "No meetings to backfill. Process some meetings first with /meeting." |
| Backfill meeting already has Meeting node | Skip — already backfilled (idempotent) |

## Examples

### Example 1: Quick extraction

```
> /meeting

Which meeting should I process?
  1. Charlie Onboarding Call — Feb 20 (with Charlie, Oz)

> 1

What matters most from this meeting?
  1. Just the action items
  2. Full analysis
  3. Focus on decisions

> 1

Reading meeting...

  Approach: inline (quick extraction per user intent)

From "Charlie Onboarding Call — Feb 20" (Charlie, Oz):
Approach: inline | Intent: just action items
Tone: collaborative | Alignment: 0.9

  Onboarding walkthrough with a new team member. Straightforward
  orientation — no contentious decisions.

  ◉ Action: Set up dev environment                          [0.9]
    "I'll get the repo cloned today" — them

  ◉ Action: Schedule follow-up for next week                [0.8]
    "let's sync again Tuesday" — me

  No tensions. No evolution signals.

Adjust? (y/edit/skip)
> y

Creating artifacts...
  [1/2] Writing meetings/2026-02-20-charlie-onboarding.md
  [2/2] Indexed in knowledge graph (batch: 4 queries)
```

### Example 2: Deep political analysis

```
> /meeting

Which meeting should I process?
  1. Strategy Review — Feb 19 (with Alice, Bob, Carol)

> 1

What matters most from this meeting?
  1. Full analysis
  2. Help me understand the political dynamics
  3. What changed from last time?

> 2

Reading meeting...

  Approach: scaffold + 3 sub-agents (deep dynamics per user intent)

From "Strategy Review — Feb 19" (Alice, Bob, Carol):
Approach: scaffold + 3 agents | Intent: political dynamics
Tone: tense → tentative alignment | Alignment: 0.6

  The real conversation was about control — who owns the roadmap
  and how decisions get ratified. Alice pushed hard for a formal
  RFC process while Bob resisted formalization. Carol mediated
  but her neutrality read as disengagement to both sides.

  ◉ Decision: Adopt lightweight RFC for architectural changes [0.6]
    "we need some process here" — them (Alice)
    Conviction: assertion (Alice), reluctant (Bob)
    Urgency: medium | Importance: high
    Evolution: new position (no prior process)

  ◉ Finding: Team alignment lower than stated               [0.5]
    * transcript-only
    Conviction: exploration
    Open: does Bob's resistance reflect a deeper concern?

  Tensions:
    * "RFC adoption" — stated as consensus [0.6], but
      Bob's energy dropped noticeably after the vote.
      Compliance ≠ conviction.
    * Alice frames process as empowerment, but the proposal
      centralizes approval through her team.

  Dynamics:
    * Alice drove 60% of the conversation
    * Bob disengaged in final third
    * Carol's mediator role masked her own position

  Actions:
    * Alice — draft RFC template by Friday
    * Bob — review and comment by Monday

Adjust? (y/edit/skip)
```

## Next

Run `/save` to share, or `/activity` to see the knowledge graph impact.
