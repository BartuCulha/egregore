---
pipeline_id: meeting-pipeline
title: Meeting Analysis Pipeline (Adaptive)
version: 2
status: active
created: 2026-02-12
updated: 2026-02-22
author: cem
corpus_size: 2
quick_configs:
  - adaptive
  - inline
  - parallel-3
dimensions_skip: []
---

# Meeting Analysis Pipeline (Adaptive)

## Architecture

v2: Adaptive analysis. The agent reads the material, asks what matters, and decides how to analyze. Replaces fixed 3-analyst topology with agent-chosen approach.

```
Input(meeting) → intent_harvest(user) → agent_chooses_approach → Output
                                              │
                                 ┌────────────┼────────────┐
                                 │            │            │
                              inline    scaffold+N    full pipeline
                              (cheap)   (balanced)    (expensive)
```

## Slots

Slots are now optional — the agent activates what the material and intent require.

| Slot | Role | Options | Default |
|------|------|---------|---------|
| substance | Priorities, dependencies, events, enrichments from transcript | opus, sonnet, haiku, inline | inline |
| dynamics | Tone, energy, convictions, interpersonal signals | opus, sonnet, haiku, inline, null | inline |
| continuity | Decision evolution, recurring topics, open threads, meta-patterns | opus, sonnet, haiku, inline, null | inline |
| criticality | Self-contradictions, stated vs revealed, unspoken risks | inline, null | inline |
| synthesis | Merge outputs into coherent briefing + enriched artifacts | opus, sonnet, inline | inline |

`inline` = handled by the main agent directly (no sub-agent spawned).
`null` = lens skipped entirely.

## Input Resolution
| Type | Command |
|------|---------|
| meeting | Granola MCP: `get_meeting_transcript` + `list_meetings` for metadata |

## Topology Configs

Each config defines a fixed topology for evaluation comparison. In production, the `adaptive` config lets the agent choose freely.

### inline
substance: {model: inline}, dynamics: {model: inline}, continuity: {model: inline}, criticality: {model: inline}, synthesis: {model: inline}
_Single-pass. Agent reads everything and produces output directly. Cheapest._

### scaffold-inline
substance: {model: inline}, dynamics: {model: inline}, continuity: {model: inline}, criticality: {model: inline}, synthesis: {model: inline}
_Panel → scaffold → guided transcript reading. Still inline but structured._

### parallel-3
substance: {model: sonnet}, dynamics: {model: sonnet}, continuity: {model: sonnet}, criticality: {model: inline}, synthesis: {model: opus}
_The old "current" — 3 Sonnet analysts + Opus synthesis. Reference topology._

### adaptive
substance: {model: agent_choice}, dynamics: {model: agent_choice}, continuity: {model: agent_choice}, criticality: {model: agent_choice}, synthesis: {model: agent_choice}
_Agent chooses freely based on material + user intent. Records topology used._

### deep
substance: {model: sonnet}, dynamics: {model: opus}, continuity: {model: sonnet}, criticality: {model: opus}, synthesis: {model: opus}
_Quality ceiling for high-stakes meetings._

### Ablation Configs (preserved)

### baseline
substance: {model: haiku}, dynamics: {model: haiku}, continuity: {model: haiku}, criticality: null, synthesis: {model: opus}
_Cost floor. Minimum viable quality._

### all-sonnet
substance: {model: sonnet}, dynamics: {model: sonnet}, continuity: {model: sonnet}, criticality: {model: inline}, synthesis: {model: sonnet}
_Mid-tier everywhere. Tests whether Opus synthesis is worth it._

### substance-only
substance: {model: sonnet}, dynamics: null, continuity: null, criticality: null, synthesis: {model: opus}
_Ablation: is substance alone sufficient?_

### substance-dynamics
substance: {model: sonnet}, dynamics: {model: sonnet}, continuity: null, criticality: null, synthesis: {model: opus}
_Ablation: what does continuity add?_

### substance-continuity
substance: {model: sonnet}, dynamics: null, continuity: {model: sonnet}, criticality: null, synthesis: {model: opus}
_Ablation: what does dynamics add?_

## Eval Dimensions

### Existing (1-7)

1. **extraction_completeness**: Did the config capture all significant insights from the meeting — decisions, findings, patterns, actions?
2. **classification_accuracy**: Are items correctly categorized (decision/finding/pattern/action)? Are confidence signals appropriate?
3. **evidence_quality**: Are quotes well-selected (high signal, not just long)? Do they support the extracted insight?
4. **tradeoff_extraction**: Were tradeoffs, pros/cons, tensions captured? Are they specific rather than generic?
5. **context_richness**: Enough context to understand each item standalone without reading the full transcript?
6. **cross_referencing**: Were connections between extractions identified? Does the analysis reveal tensions?
7. **noise_filtering**: Was trivia/logistics correctly excluded? Is the signal-to-noise ratio high?

### New (8-10)

8. **approach_appropriateness**: Did the agent match topology to material complexity and user intent? A 10-minute standup analyzed with 3 sub-agents is over-engineered. A 90-minute strategy session analyzed inline may miss dynamics. Score based on whether the chosen approach fits the material.

9. **intent_alignment**: Did asking the user produce measurably different and more useful output than default analysis? Compare: "just action items" should produce a focused, lightweight output — not a full meta-analysis. "Help me understand the political dynamics" should produce deep dynamics/criticality analysis. Score based on whether the output matches what the user asked for.

10. **cost_quality_frontier**: Where does additional compute stop buying quality? Plot quality (dimensions 1-7 average) against cost per topology. Identify the point of diminishing returns. This dimension doesn't have a "winner" — it maps the frontier.

## Success Criterion

Adaptive + intent-aware analysis should match or beat the best forced topology on 70%+ of meetings. Specifically:
- `adaptive` config ≥ `parallel-3` on dimensions 1-7 for 70%+ of corpus
- `adaptive` config scores ≥ 0.7 on `approach_appropriateness` (dimension 8)
- `adaptive` config scores ≥ 0.7 on `intent_alignment` (dimension 9)
- `adaptive` sits on or near the efficient frontier for `cost_quality_frontier` (dimension 10)

## Input Corpus
- meeting:7df47eba-a155-4a37-93a5-5528f0d8a68d
- meeting:dbca2151-7730-473c-901e-b056abe640b2
(Expand as meetings are processed)

## Corpus Sampling

When the input corpus grows beyond `corpus_size` (frontmatter), randomly sample `corpus_size` inputs per run using a deterministic seed (run date + seq number). This prevents eval cost from growing linearly with meeting history while maintaining reproducibility.

## Tournament Protocol

1. Pick 2 configs (e.g., `adaptive` vs `parallel-3`)
2. Run both on the same meeting (same intent prompt for both)
3. Present outputs side-by-side (anonymized: "Output A" vs "Output B")
4. Human picks winner per dimension + overall
5. Record result, update Elo rankings
6. Repeat across corpus for statistical significance (minimum 3 meetings per matchup)

For `adaptive` config: record the topology it chose so the eval captures not just output quality but approach selection.

### Calibration

Periodically run `/eval:calibrate` to check:
- Present blinded pairs to human alongside automated judge's assessment
- Record agree/disagree
- Produces divergence pattern report — answers "can I trust the automated tournament?"

## Future: Automated Judging

Once calibration data is sufficient (20+ human judgments), test automated judging:
- Use Opus as judge with calibrated rubric
- Compare automated rankings to human rankings
- Only trust automated scores where calibration divergence < 15%
