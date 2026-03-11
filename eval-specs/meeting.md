# Eval: Meeting Analysis Pipeline (Adaptive)

Defines the evaluation framework for the adaptive meeting analysis pipeline. Spec only — no tooling yet.

## Slots

| Slot | Model | Input | Output |
|------|-------|-------|--------|
| `substance` | agent_choice (inline / sonnet / opus / haiku) | transcript + scaffold + open questions + quests | priorities, dependencies, events, enrichments, `_raw_notes` |
| `dynamics` | agent_choice (inline / sonnet / opus / haiku / null) | transcript + attendees | tone, energy, convictions, interpersonal dynamics, `_raw_notes` |
| `continuity` | agent_choice (inline / sonnet / opus / haiku / null) | panel + graph context (Q1-Q4) + scaffold | decision evolution, recurring topics, open threads, meta-patterns, `_raw_notes` |
| `criticality` | agent_choice (inline / null) | all available inputs | self-contradictions, stated vs revealed, unspoken risks |
| `synthesis` | agent_choice (inline / sonnet / opus) | all outputs + panel + scaffold | Meeting Intelligence Briefing + enriched artifact list |

`inline` = handled by main agent, no sub-agent. `null` = lens skipped.

## Topology Configs

| Config | substance | dynamics | continuity | criticality | synthesis | Notes |
|--------|-----------|----------|------------|-------------|-----------|-------|
| `inline` | inline | inline | inline | inline | inline | Single-pass, cheapest |
| `scaffold-inline` | inline | inline | inline | inline | inline | Structured but still inline |
| `parallel-3` | sonnet | sonnet | sonnet | inline | opus | Old "current" reference |
| `adaptive` | agent_choice | agent_choice | agent_choice | agent_choice | agent_choice | Agent chooses freely |
| `deep` | sonnet | opus | sonnet | opus | opus | Quality ceiling |
| `baseline` | haiku | haiku | haiku | null | opus | Cost floor |

### Ablation Configs

| Config | substance | dynamics | continuity | criticality | synthesis |
|--------|-----------|----------|------------|-------------|-----------|
| `substance_only` | sonnet | null | null | null | opus |
| `dynamics_only` | null | sonnet | null | null | opus |
| `continuity_only` | null | null | sonnet | null | opus |
| `substance+dyn` | sonnet | sonnet | null | null | opus |
| `substance+cont` | sonnet | null | sonnet | null | opus |

When a slot is `null`, the synthesis step receives an empty object for that lens output.

## Test Corpus

Diverse meeting types to test generalization:

| ID | Type | Characteristics |
|----|------|----------------|
| `7df47eba-a155-4a37-93a5-5528f0d8a68d` | Strategy session | Egregore onboarding workflow — 2 attendees, decision-heavy |
| `dbca2151-7730-473c-901e-b056abe640b2` | Strategy session | Egregore workflow + AI strategy — 2 attendees, mixed decisions/findings |
| *(add more as meetings accumulate)* | | |

Target: 5-10 meetings across at least 3 types (strategy, design review, standup/sync).

## Evaluation Criteria

### Per-Slot Criteria

**Substance (`substance_richness`)**:
Does the output capture nuances beyond what the panel says? Specifically:
- Priorities identified with supporting evidence
- Dependencies with blockers and owners
- Events (internal/external) that contextualize decisions
- Enrichments that add context, tradeoffs, and open questions to scaffold items
- `_raw_notes` that contain reasoning not captured in structured fields

**Dynamics (`dynamics_insight`)**:
Does the dynamics analysis reveal interpersonal signals not obvious from text?
- Tone arc that captures emotional evolution through the meeting
- Conviction strength distinctions (assertion vs hypothesis vs exploration)
- Interpersonal dynamics (who drove, alignment/tension, power dynamics)
- `_raw_notes` with observations about what was unsaid or implied

**Continuity (`continuity_value`)**:
Does cross-meeting context add information the other lenses miss?
- Decision evolution chains linking to specific previous artifacts
- Topic recurrence with meaningful trajectory analysis (not just counting)
- Open threads from previous meetings addressed or continued
- Meta-patterns that emerge across multiple meetings

**Criticality (`criticality_depth`)**:
Does the analysis surface tensions that other lenses miss?
- Self-contradictions within a single speaker's framing
- Gaps between stated confidence and actual energy
- Risks visible but unnamed
- Compliance ≠ conviction distinctions

**Synthesis (`synthesis_coherence`)**:
Is the final briefing more than the sum of its parts?
- Meta-analysis captures the "so what" not available from any single lens
- Analytical tensions section identifies genuine contradictions
- Artifacts are enriched with dimensional properties
- The briefing reads as a coherent intelligence document, not a concatenation

### Cross-Cutting Criteria

**Cost efficiency (`cost_efficiency`)**:
Quality per dollar. Compare config outputs against their cost:
- inline (~$0.02): minimum cost
- scaffold-inline (~$0.05): structured inline
- parallel-3 (~$0.40): reference multi-agent
- deep (~$0.55): maximum quality

**Complementarity (`lens_complementarity`)**:
Do lenses produce genuinely different signal? Compare:
- `substance_only` vs `adaptive`: what do dynamics + continuity + criticality add?
- `substance+dyn` vs `adaptive`: what does continuity add?
- `substance+cont` vs `adaptive`: what does dynamics add?

If ablation variants score within 10% of `adaptive`, the removed lens isn't adding value.

## Dimensions (1-10)

1. **extraction_completeness**: All significant insights captured?
2. **classification_accuracy**: Correct categories and confidence signals?
3. **evidence_quality**: Well-selected quotes that support insights?
4. **tradeoff_extraction**: Tradeoffs captured specifically, not generically?
5. **context_richness**: Each item understandable standalone?
6. **cross_referencing**: Connections identified, tensions revealed?
7. **noise_filtering**: Trivia excluded, high signal-to-noise?
8. **approach_appropriateness**: Did the agent match topology to material complexity and intent?
9. **intent_alignment**: Did the output match what the user asked for?
10. **cost_quality_frontier**: Where does additional compute stop buying quality?

## Success Criterion

Adaptive + intent-aware analysis should match or beat the best forced topology on 70%+ of meetings.

## Tournament Protocol

1. Pick 2 configs (e.g., `adaptive` vs `parallel-3`)
2. Run both on the same meeting (same intent prompt for both)
3. Present outputs side-by-side (anonymized: "Output A" vs "Output B")
4. Human picks winner per dimension + overall
5. Record result, update Elo rankings
6. Repeat across corpus for statistical significance (minimum 3 meetings per matchup)

For `adaptive` config: record the topology it chose so the eval captures approach selection.

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
