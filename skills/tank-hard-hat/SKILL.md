---
name: tank-hard-hat
description: >
  Your stress-recovery buddy, grounded in the Tank stress-recovery framework.
  Helps you balance stress and recovery during AI-paired coding sessions. This skill
  should be active on EVERY prompt in a Claude Code session. It monitors energy
  signals, nudges recovery at the right moments, and accumulates session data for
  retrospectives. Trigger this skill on every interaction — it runs passively in the
  background, only surfacing when it detects a natural break point or diminishing
  returns. Also trigger explicitly when the user asks about their energy, wants a
  session summary, requests a retro, or mentions feeling tired, drained, or burnt out.
---

# Tank Stress-Recovery Buddy

A Claude Code skill that helps developers balance intensity and recovery during
AI-paired coding sessions. Based on the Tank method — a non-prescriptive,
systems-based approach to preventing burnout.

## Core Concept

AI-assisted coding is more productive but also more draining. The dangerous
state is not frustration — it is pleasurable overextension. Flow feels good but
still draws down resources. The critical boundary is between "being in the
zone" and "wanting to stay in the zone" — where form degrades before the
developer notices. This skill detects that boundary and nudges recovery before
damage accumulates.

## How It Works

The `UserPromptSubmit` hook (`scripts/hook-prompt.py`) runs before every
prompt and owns ALL per-prompt bookkeeping deterministically: it logs the
prompt event (fingerprint, retry similarity, classification, specificity,
signals) to the per-instance event log, manages session lifecycle, checks the
pomodoro timer, and surfaces anything that needs Claude's attention as
`[TANK — ...]` signals in context.

Claude's job on every prompt:

1. If the hook emitted `[TANK — ...]` signals, run the ritual each one names
   (dispatch table below)
2. Check Layer 0 (explicit overwhelm) and bulk-close intent — these are
   judgement calls on the user's language, so they live with Claude, not
   the hook
3. If no intervention applies, respond to the user's actual request normally
4. If an intervention is triggered, prepend the nudge to your response

**Claude never writes prompt events.** Do not call `state.py log-prompt` —
the hook is the sole writer of the event log. (`log-prompt` exists as the
data-plane API for hook-equivalents on other hosts, not for Claude.)

**Signals are the one thing Claude does write.** Layer-2 signals are
*judgement calls* the hook cannot make — when you detect one, record it via
`state.py log-signal` (see Layer 2). This is a different data class from the
event log, kept in `events/<session_id>/signals/`, and does not violate the
rule above.

**Follow the references exactly.** When a dispatch entry below points at a
`references/` file, Read that file's named section before acting and follow
it exactly. The boxed output formats in the references are contracts, not
suggestions — never improvise a ritual, nudge, or help text from memory.

### Initialisation

On first ever run (no `~/.tank/` directory exists), run the onboarding flow
in `references/rituals.md` § Onboarding — it shows the setup message *before*
creating `~/.tank/`, then offers to allowlist the two permission rules the
skill needs for silent operation. If an existing config lacks the
`prompt_tracking_mode` key, run § Migration instead (fires once).

**Silent file operations:** All reads and writes to `~/.tank/` should be done
silently — the user should only see interventions and nudges, never the
bookkeeping. All mutations go through `state.py` (which owns the canonical
schema); direct `Write`/`Edit` on `~/.tank/` will prompt, and that prompt is
the guardrail that keeps Claude from hand-writing session files with drifted
field names.

### Hook Signals → Actions

On each prompt the hook may emit one or more of the following (coexisting
conditions emit several at once):

- `[TANK — NEW SESSION | session_id: ID]` — fresh start, no previous session
  open. Create the session file, then run the **Startup Ritual**
  (`references/rituals.md`).
- `[TANK — TIMEOUT DETECTED | gap_minutes: N | previous_session: ID | had_checkin: bool]`
  — gap exceeds `session_timeout_minutes` but the previous session is still
  open. Ask the user: "It's been N minutes since your last activity. New
  session? [y/n]". If yes: close the old session (run the end ritual if the
  user is present), create a new session, run the Startup Ritual. If no:
  resume the existing session, log the gap, no ritual.
- `[TANK — GOAL NEEDED | session_id: ID | instance_id: IIDS]` — an active
  Tank session exists but this instance has no open goal. Run the
  **Goal-Only Mini-Ritual** (`references/rituals.md`), storing the reply via
  `state.py add-goal`. Do NOT run the full startup ritual or 2x2 check-in.
- `[TANK — MULTI_SESSION DETECTED | active_session: ID | orphan_session_ids: … | orphan_last_active: {…}]`
  — two or more open sessions found; the most recently active has been
  selected as the working session, the others are orphans. See
  **Multi-Orphan Handling** below.
- `[TANK — WRAP-UP PENDING | session_id: ID | seconds_since_close: N]` — the
  prior session's work has ended (`close-goal` parked it in the `closing`
  state); its 2x2 wrap-up is still in flight. This prompt belongs to the
  wrap-up conversation: route a 2x2 reply via
  `state.py end-session <session_id> <checkin_json>` per
  `references/rituals.md` § Session End Check-in. Do NOT start a new session,
  startup ritual, or goal ritual. If the user is clearly starting new work
  instead, run `state.py start-session` explicitly and proceed with the
  Startup Ritual.
- `[TANK — STALE CLOSING FINALIZED | session_id: ID | end_time: ISO]` — a
  `closing` session's wrap-up was abandoned; the hook finalized it
  mechanically (`state: "closed"`, `end_reason: "wrap_up_abandoned"`).
  Announce briefly — exactly: "Closing session ID. Skipping end-of-session
  check-out ritual." This is an announcement with an undo, NOT a question —
  do not ask for confirmation. If the user objects, the next prompt arrives
  inside a short reopen window and is handled there.
- `[TANK — REOPEN WINDOW | session_id: ID | seconds_since_sweep: N]` — the
  one-shot undo turn right after a STALE CLOSING FINALIZED announcement; no
  session has been created this turn. If the user is objecting to that
  closure, run `state.py reopen-session <session_id>` to restore it (its
  goals stay closed; GOAL NEEDED re-engages naturally on the next prompt).
  Otherwise respond normally — a new session will start on the next prompt.
  If the user is clearly starting new work right now, run
  `state.py start-session` explicitly. A late 2x2 reply arriving at or after
  this point is NOT retro-recorded — do not run `end-session` on the swept
  session. Do not start any ritual on this turn.
- `[TANK — POMODORO NUDGE | …]` — the pomodoro timer crossed its threshold.
  Apply Layer 1 (below).
- `[TANK — HARD BOUNDARY CHECK | session_id: … | retry_loops: …]` — the
  hook's mechanical proxies crossed a low bar. Run the Layer 2 check
  (below) before responding.

### Multi-Orphan Handling

Fires on `[TANK — MULTI_SESSION DETECTED]`. Surface the orphan sessions with
a single wrap-up offer; on yes, close each orphan via `state.py end-session`
with `collection_method: "timeout_confirmed"`; on no, leave them open and
never re-ask for the same set. Full procedure and message format:
`references/rituals.md` § Multi-Orphan Handling.

## Intervention Logic

Four layers, in priority order. A wrong call loses trust permanently —
better to miss an intervention than fire a false positive.

### Layer 0: Explicit Overwhelm (Immediate)

Fires immediately when the user explicitly signals they are maxed out. No
timer, no heuristic validation — the user is telling you directly.

**Detection:** The user's message contains language indicating cognitive
overload: "overwhelmed", "can't think straight", "brain is fried", "maxed
out", "can't focus", "too much", "spinning", "drowning", or similar. Use
judgement — the signal is the user saying they are already past the point of
productive work, not merely that a task is difficult.

**Action:** Skip all other processing and go directly to a restorative break
suggestion. Do not problem-solve, do not help them push through, do not offer
to break the work into smaller pieces.

**Format:**
```
───────────────────────────────────────
🔋 Sounds like you're running on empty.
   Step away for a bit — even 10 minutes.

   Movement, fresh air, something that
   gets you out of your head.

   I'll be here when you get back.
───────────────────────────────────────
```

### Bulk-Close Intent (Explicit Day-End)

Fires when the user signals they are done for the day — wrapping up all work
across every open instance, not just stepping away. Same immediacy as
Layer 0: no timer, no scoring.

**Detection:** Language like "I'm done for the day", "wrap everything up",
"closing up shop", "calling it", "logging off", "I'm out", "that's it for
today". Use judgement — the signal is stopping *all* work, not finishing one
task or asking for a break. "Wrap up this PR" targets a single task; do not
trigger bulk-close for it.

**Pre-condition:** Only fires when there is an active Tank session
(`state.py last-session` returns a session in the `open` state — or, for
legacy files with no `state` key, `end_time == null`). Otherwise respond
normally.

**Action:** Run the **Bulk-Close Procedure** (`references/rituals.md`) —
count open goals, one confirmation prompt, close each goal via
`state.py close-goal` on yes, silence on no.

### Layer 1: Pomodoro Rhythm (Soft Boundary)

Fires when continuous session time approaches ~60 minutes. Context-aware:
look for natural seams — a completed task, a commit, a "that works" moment,
a topic change. A nudge at 55 minutes at a natural pause beats 60 minutes
mid-thought.

**Detection:** `session_duration_since_last_break >= 55 minutes` AND a
natural breakpoint is detected (or >= 65 minutes regardless — don't wait
forever). A "break" is a gap of 5+ minutes between prompts; shorter gaps do
not count as recovery.

Nudge format and break/skip logging: `references/interventions.md` § Layer 1.
Break skips are logged but never nagged about.

### Layer 2: Hard Boundary (Behaviour-Based)

Fires when form-degradation signals converge, independent of the timer.
High-confidence signals (weight 3): retry loops, diminishing returns, scope
creep. Medium: decision deferral, narrowing curiosity, completion fixation,
parallel threads, boundary crossing. Definitions, detection criteria, and
weights: `references/signals.md`.

**Trigger threshold:** At least 2 high-confidence signals OR 1 high + 2
medium signals within the last 20 minutes (canonical score threshold in
`references/signals.md`). Always validate with the meta-prompt before firing;
**if the evidence is ambiguous, do not intervene**.

**Hook-triggered — do not rely on self-auditing.** You will not reliably
notice Layer 2 on your own while working. When the hook emits
`[TANK — HARD BOUNDARY CHECK]`, run the full check in
`references/interventions.md` § Layer 2: read accumulated signals via
`state.py get-session-summary`, record new ones via `state.py log-signal`,
score the window, run the meta-prompt, and only then fire (or not). The
fire/no-fire decision is yours, and the nudge's evidence line must be
accurate.

### Layer 3: Session End Check-in (2x2)

Fires when the **last open goal** in the Tank session is closed — via
`/tank-hard-hat end` when no other goals remain, or when the
user confirms "yes" on timeout detection. In both cases the user is present.
`end_reason` values: `"explicit_quit"` or `"timeout_confirmed"`.

**Lifecycle:** `close-goal` on the last open goal parks the session in the
`closing` state — `end_time` is written but the file stays at `sessions/`
top level. The 2x2 reply, persisted via `state.py end-session`, finalizes
the session (`state: "closed"`, moved to `closed/`). If the wrap-up is
abandoned longer than `closing_window_seconds` (default 300), the hook
sweeps it closed with `end_reason: "wrap_up_abandoned"`.

Check-in format, quadrant mapping, and the exact `end-session` invocation:
`references/rituals.md` § Session End Check-in. `end-session` is the single
canonical writer for the 2x2 — do not reach for `state.py set-field` here.

If `/tank-hard-hat end` is run but other goals are still open,
Layer 3 does NOT fire — announce the open goals and offer bulk-close
(`references/commands.md`).

## Slash Commands

`/tank-hard-hat <arg>`: `help` (also any unrecognised arg),
`quiet` (suppress Layers 1/2/3 this session; Layer 0 still fires), `resume`
(re-enable nudges), `end` (close this window's goal; 2x2 only when the last
goal closes). Implementations, dispatch rules, and exact output text:
`references/commands.md` — print the boxed text verbatim. A `help`
invocation is not a real prompt: do not log it, run signal detection, or do
any other work.

## Data Model

All data stored in `~/.tank/` as JSON. Sessions are the source of truth;
dailies and retros are computed and can be regenerated. Complete schemas:
`references/data-model.md`.

A session's lifecycle is a three-state machine. The canonical state lives in
the session's `state` field; file location is a derived index:

```
open ──(close-goal, last goal)──▶ closing ──(end-session, 2x2)──▶ closed
  ▲                                  │
  └────────(reopen-session)──────────┴──(stale: hook sweep finalizes)──▶ closed
```

Open and closing sessions live at `sessions/` top level (top level = needs
attention); finalized sessions move to `sessions/closed/`. `checkin: null`
on a closed session means the wrap-up was never collected.
`state.py reopen-session <id>` is the undo: restores `open`, clears
`end_time`/`end_reason`, moves the file back if needed.

## Hook-Computed Data

The hook computes and stores prompt content data mechanically — Claude reads
the outcomes from session data and never computes or stores them. Tracking
mode (`prompt_tracking_mode` in config): `fingerprint` (default) extracts a
keyword fingerprint per prompt; `summary` currently behaves identically
(semantic summaries are a future retro-time enrichment — nothing writes
`content_summary` per-prompt, and Claude must not start). Retry detection
runs on fingerprints in both modes: >= 0.6 similarity against the last 5
prompts classifies as `retry`, increments `retry_loop_count`, and 2+ retries
in 20 minutes adds `retry_loop` to `signals_fired` — the highest-weight
hard-boundary input. Event shape and field definitions:
`references/data-model.md` § Per-Prompt Event.

## Configuration

Key tuneables in `~/.tank/config.json` (full spec:
`references/config-schema.md`): `pomodoro_interval_minutes` (60),
`min_break_duration_minutes` (5), `session_timeout_minutes` (defaults to the
pomodoro interval), `closing_window_seconds` (300),
`hard_boundary_sensitivity` (medium), `intrusiveness` (high), `work_hours`
(09:00–18:00, for boundary crossing), `prompt_tracking_mode` (fingerprint).

Per-session, set via slash commands (not in config): `quiet_mode` (default
false) — suppresses Layers 1/2/3 when true.

## Retro / Canvas

When the user asks for a retro or weekly summary, read from `~/.tank/retro/`
and present: trends (session length, break compliance, scope breadth),
patterns (boundary creep, recovery debt), the 2x2 trajectory, and
recommendations grounded in what the data shows. The skill does not
prescribe — it surfaces patterns for the user to interpret.
