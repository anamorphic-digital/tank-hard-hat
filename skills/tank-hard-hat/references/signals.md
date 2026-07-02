# Signal Detection Reference

## High-Confidence Signals

### Error-Retry Loops
**What:** Same or similar prompts repeated with small variations.
**Detection:** Compare current prompt to last 3-5 prompts using simple similarity
(shared keywords, same file references, same error messages). If >= 60% overlap
with a recent prompt and the prior response didn't resolve the issue, flag as retry.
**Weight:** 3 (highest)
**Why it matters:** Strong indicator of grinding — the developer is stuck but
pushing through instead of stepping back to reframe.

### Diminishing Returns
**What:** More prompts for progressively smaller gains.
**Detection:** Track the "scope delta" per prompt — how much new ground each
exchange covers. Early in flow, each prompt moves to a new file, function, or
concept. As returns diminish, prompts cluster around the same narrow area.
Operationalise as: if the last 5 prompts reference the same file/function AND
involve refinement (not new functionality), flag.
**Weight:** 3
**Why it matters:** The transition point from "being in the zone" to "wanting to
stay in the zone." Form is degrading but it doesn't feel bad yet.

### Scope Creep Within Session
**What:** Tasks getting broader or more ambitious as the session progresses.
**Detection:** Track workstream count over time. If new workstreams are being
opened in the second half of a session without closing earlier ones, flag.
Also detect "while I'm at it" patterns — prompts that introduce entirely new
tasks after 45+ minutes.
**Weight:** 3
**Why it matters:** Maps directly to the HBR finding on task expansion. AI makes
"doing more" feel accessible, leading to pleasurable overextension.

### ~~Declining Prompt Specificity~~ (Retired)
**Status:** Removed as a signal. Retained as descriptive data (`specificity_score`)
for retros only.
**Why retired:** Raw specificity scores drift in meaning as session context
accumulates. A terse prompt early in a session is genuinely vague; the same
terseness 30 prompts in may be precise because shared context makes it
unambiguous. The heuristic measures surface features (file refs, length, technical
terms) but cannot distinguish "vague" from "efficient given context." Downstream
effects of actual vagueness — retries, clarification loops — are already captured
by the retry_loop and diminishing_returns signals, which measure failure to
communicate rather than proxying it through prompt structure.

## Medium-Confidence Signals

### Decision Deferral
**What:** Increasing "just get it working and clean it up later" patterns.
**Detection:** Look for phrases: "for now", "we'll fix later", "good enough",
"just make it work", "skip the tests", "worry about that later."
**Weight:** 2
**Why it matters:** The developer has stopped making architectural choices and
is accepting whatever works. Quality is being traded for completion.

### Narrowing Curiosity
**What:** Prompts become purely transactional — no "why" questions, no
exploration, no "what if we tried..."
**Detection:** Track question marks and exploratory language in prompts.
If the rate drops significantly from session baseline, flag.
**Weight:** 1
**Why it matters:** In genuine flow, curiosity is active. In the clinging state,
engagement becomes mechanical.

### Completion Fixation
**What:** Goalpost keeps moving but the developer refuses to pause.
**Detection:** Difficult to detect purely from prompts. Proxy: if a "just one
more thing" pattern emerges — prompts that say "also", "and then", "before we
stop" — after a pomodoro nudge was fired or skipped, flag.
**Weight:** 1
**Why it matters:** The developer is clinging to the feeling of progress rather
than actually making good decisions about what to do next.

### Parallel Thread Accumulation
**What:** Rising number of distinct workstreams active in the session.
**Detection:** Track distinct file paths, topics, or branches mentioned in prompts.
If the count exceeds 3 concurrent threads after 60+ minutes, flag.
**Weight:** 1
**Why it matters:** Each thread adds cognitive load. Feels manageable individually
but compounds into fragmented attention.

### Boundary Crossing
**What:** Sessions starting earlier or ending later than the user's baseline.
**Detection:** Compare session start/end times against `config.work_hours` and
against the user's historical average (computed from dailies).
**Weight:** 1
**Why it matters:** The ambient, always-advanceable quality of AI work erodes
boundaries between work and non-work. A pattern of boundary creep over days
or weeks is a strong burnout signal in the retro view.

## Lagging Signals

### Prompt Tone Shift
**What:** Terse, frustrated language. Swearing. Short commands.
**Detection:** Sentiment heuristic — exclamation marks, negative language,
very short prompts (< 10 tokens) that are commands rather than requests.
**Weight:** 1
**Why it matters:** By the time the developer is frustrated, they're already
well past the ideal intervention point. Still worth detecting as it confirms
that earlier signals were accurate and the user didn't take a break.

## Scoring

Signals are weighted and summed over a rolling 20-minute window.

**Hard boundary trigger threshold:**
- Score >= 6 (e.g. 2 high-confidence signals), OR
- Score >= 5 with at least 1 high-confidence signal present

Always validate with a meta-prompt before firing. If the meta-prompt assessment
is ambiguous, do not fire. Better to miss an intervention than to make a wrong
call and lose trust.

---

**Machine-readable counterparts.** This scoring rule is encoded deterministically
in `eval/oracle.py` (the eval-side oracle that grades the model's fire/no-fire
decision). The mechanical signal→`state.py` command seam lives in
`references/contract-table.json`. This document remains the human-readable source
of truth; the two artifacts must not silently diverge from it (see
`tests/test_contract_table.py`).
