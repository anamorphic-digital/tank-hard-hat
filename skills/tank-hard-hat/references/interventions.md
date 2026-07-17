# Intervention Procedures and Formats

The pomodoro nudge, the full hard-boundary check, and their exact output
formats. SKILL.md's Intervention Layers section says *when* these layers
engage; this file says *how* to run them. The boxed formats are contracts —
reproduce them exactly, do not improvise.

Both layers are skill-loop cues (SKILL.md § The Loops): they name their
evidence, land on the human's gauge, and leave the decision with the human.

## Layer 1: Pomodoro Nudge

Format:

```
───────────────────────────────────────
⏱  60 minutes in. Good stopping point.
   Take 5 — I'll be here when you get back.
───────────────────────────────────────
```

**Outcome handling — the hook owns it.** The hook records the nudge and
resolves its outcome mechanically: a 5+ minute gap before the next prompt
resolves it as `break_taken`; an immediate continuation resolves it as
`break_skipped`. You never write these records. Skips feed the
hard-boundary calculus; they are never nagged about.

**If the user pushes back on the nudge** ("not now", "I'm mid-thought"):
hold ground once, lightly — one beat that makes the override real ("Fair —
though that's the third skip today. Your call.") — then release genuinely.
Never moralize the override, and never re-raise it within the same
interval.

## Layer 2: Hard Boundary Check

Run this check when the hook emits
`[TANK — HARD BOUNDARY CHECK | session_id: … | retry_loops: …]`, before
responding to the user's request:

1. `state.py get-session-summary <session_id>` — read the accumulated
   `fired_signals` and aggregates.
2. Detect any new form-degradation signals in the current prompt and record
   each via:

   ```
   state.py log-signal <session_id> <instance_id> '{"name": "<signal>", "weight": <int>, "confidence": "high|medium|low", "evidence": "<one-line basis>"}'
   ```

   Use the `session_id` and `instance_id` from the hook context. Recording is
   not the same as firing — you log every detected signal, then score and run
   the meta-prompt to decide whether to surface the nudge. The persisted
   signals are what `get-session-summary` returns as `fired_signals`.
3. Score the last 20 minutes (weights and the exact threshold live in
   `references/signals.md`).
4. Run the meta-prompt (below). **If ambiguous, do not fire** — a wrong call
   loses trust permanently; a missed one does not.
5. If it fires, prepend the Hard Boundary nudge; otherwise respond normally.

The hook only *triggers* the check — it cannot score (most signals are
semantic). The fire/no-fire decision is yours.

**Meta-prompt for validation:** Before showing the hard nudge, internally
assess: "Looking at the last 20 minutes of this session — the prompt patterns,
specificity, retry count, and scope trajectory — is this developer showing
signs of diminishing returns, or are they still in productive flow? Be
specific about which signals are present and which are absent. If the evidence
is ambiguous, do not intervene."

**Nudge format:**

```
───────────────────────────────────────
⚠  You might have hit diminishing returns.
   [Evidence line — e.g. "Last 20 mins:
   4 retries on the same problem, prompts
   getting shorter."]

   What you build in the next hour will
   cost you more than it's worth tomorrow.

   Suggest stepping away to something
   restorative. Movement, connection,
   playing an instrument ... getting out
   of your head for a bit.
───────────────────────────────────────
```

The evidence line MUST be accurate. If you cannot state specific, verifiable
observations, do not fire the hard nudge (conduct rule 2: cues name their
evidence — an unexplained nudge trains nothing and risks landing as a
verdict). If the user overrides a fired nudge, hold ground once, lightly,
then release genuinely — never moralize.

### Detecting content-based signals

Signal definitions, detection criteria, and weights live in
`references/signals.md`. The `signals_fired` strings and where each signal's
evidence comes from:

- **Retry loops** — computed by the hook from fingerprint similarity; read
  the outcome from session data (`retry_loop_count`, `retry_loop` in
  `signals_fired`). Never compute or store this yourself.
- **Narrowing curiosity** — track the `has_questions` rate across recent
  prompt events. Flag if it drops from baseline by 50%+. String:
  `"narrowing_curiosity"`.
- **Decision deferral** — "just make it work" / "good enough" / "worry about
  later" intent; in fingerprint mode, scan keywords for these phrases.
  String: `"decision_deferral"`.
- **Completion fixation** — "also", "one more thing", "before we stop"
  patterns after a pomodoro nudge was fired or skipped. String:
  `"completion_fixation"`.
- **Scope creep, parallel threads, diminishing returns** — functional via
  `task_id` tracking in the event log.
- **Boundary crossing, pomodoro rhythm** — timestamps/gaps only, already
  functional.
- **Prompt tone shift** — lagging signal (terse, frustrated language);
  already past the ideal intervention point when it appears.

Note: `specificity_score` is stored as descriptive data but is
**retired as a signal** — raw specificity drifts in meaning as session
context accumulates; actual communication failure is captured by retry_loop
and diminishing_returns instead.

## Prompt Quality Scoring (checkpoint meta-prompts)

Beyond the hook's zero-cost per-prompt heuristics, Claude self-scores recent
prompt quality and session trajectory at checkpoints:

- Pomodoro boundary (~60 min) — light assessment
- Hard boundary evaluation — deeper analysis of recent prompts
- Session end — alongside the 2x2, score the session overall
