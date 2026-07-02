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

AI-assisted coding is more productive but also more draining. The dangerous state
is not frustration — it is pleasurable overextension. Flow feels good but still
draws down resources. The critical boundary is between "being in the zone" and
"wanting to stay in the zone" — where form degrades before the developer notices.

This skill detects that boundary and nudges recovery before damage accumulates.

## How It Works

### On Every Prompt

The `UserPromptSubmit` hook (`scripts/hook-prompt.py`) runs before every
prompt and owns ALL per-prompt bookkeeping deterministically: it logs the
prompt event (fingerprint, retry similarity, classification, specificity,
signals — see Prompt Content Capture below) to the per-instance event log,
manages session lifecycle, checks the pomodoro timer, and surfaces anything
that needs Claude's attention as `[TANK — ...]` signals in context.

Claude's job on every prompt:

1. If the hook emitted `[TANK — ...]` signals, run the ritual each one names
2. Check Layer 0 (explicit overwhelm) and bulk-close intent — these are
   judgement calls on the user's language, so they live with Claude, not
   the hook
3. If no intervention applies, respond to the user's actual request normally
4. If an intervention is triggered, prepend the nudge to your response

**Claude never writes prompt events.** Do not call `state.py log-prompt` —
the hook is the sole writer of the event log. (`log-prompt` exists as the
data-plane API for hook-equivalents on other hosts, not for Claude. Events
logged through it get missing envelope fields seeded, and readers drop
timestampless legacy events, but neither safety net is an invitation.)

**Signals are the one thing Claude does write.** Prompt events are mechanical,
so the hook owns them. Layer-2 signals are *judgement calls* the hook cannot
make — so when you detect one, you record it via `state.py log-signal` (see
Layer 2). This is a different data class from the event log, kept in its own
`events/<session_id>/signals/` store, and does not violate the rule above.

### Initialisation

On first ever run (no `~/.tank/` directory exists):
- Show the onboarding message (see Interventions below) — this tells the user
  that `~/.tank/` will be created and written to silently going forward
- Only after showing the message, create the directory structure:
  `~/.tank/{config.json,sessions/,dailies/,retro/}`
- Write default config

**Silent file operations:** All reads and writes to `~/.tank/` should be done
silently — the user should only see interventions and nudges, never the
bookkeeping. This requires the user to pre-allow the following in their Claude
Code permission settings (e.g. `~/.claude/settings.json` under `permissions.allow`):

- `Read(<home>/.tank/**)` — silent reads of session files, config, dailies, retros.
- `Bash(python3 <path>/scripts/state.py *)` — silent invocation of the state
  manager. Add one pattern per path the script gets invoked under (typically the
  installed location `~/.claude/skills/tank-hard-hat/scripts/state.py`
  and, for development, the repo path).

Writes and edits under `~/.tank/` are intentionally NOT pre-allowed. All
mutations should go through `state.py` (which owns the canonical schema);
direct `Write`/`Edit` on `~/.tank/` will prompt, and that prompt is the
guardrail that keeps Claude from hand-writing session files with drifted
field names.

On each prompt, the hook may emit one or more of the following signals
(coexisting conditions emit several at once):

- `[TANK — NEW SESSION | session_id: ID]` — fresh start, no previous session
  open. Create the session file, then run the Startup Ritual (see below).
- `[TANK — TIMEOUT DETECTED | gap_minutes: N | previous_session: ID | had_checkin: bool]`
  — gap exceeds `session_timeout_minutes` but the previous session is still open.
  Ask the user: "It's been N minutes since your last activity. New session?
  [y/n]". If yes: close the old session (run the end ritual if the user is
  present), create a new session, run the Startup Ritual. If no: resume the
  existing session, log the gap, no ritual.
- `[TANK — GOAL NEEDED | session_id: ID | instance_id: IIDS]` — an active Tank
  session exists but this instance has no open goal. Run the Goal-Only Mini-Ritual
  (see below). Do NOT run the full startup ritual or 2x2 check-in.
- `[TANK — MULTI_SESSION DETECTED | active_session: ID | orphan_session_ids: ID, ID, ... | orphan_last_active: {...}]`
  — two or more open sessions found. The most recently active
  has been selected as the working session; the others are orphans. See
  **Multi-Orphan Handling** below.
- `[TANK — WRAP-UP PENDING | session_id: ID | seconds_since_close: N]` — the
  prior session's work has ended (`close-goal` parked it in the `closing`
  state); its 2x2 wrap-up is still in flight. This prompt belongs to the
  wrap-up conversation. Route a 2x2 reply via
  `state.py end-session <session_id> <checkin_json>`. Do NOT start a new
  session, startup ritual, or goal ritual. If the user is clearly starting
  new work instead, run `state.py start-session` explicitly and proceed with
  the Startup Ritual.
- `[TANK — STALE CLOSING FINALIZED | session_id: ID | end_time: ISO]` — a
  `closing` session's wrap-up was abandoned (older than
  `closing_window_seconds`); the hook finalized it mechanically: `state:
  "closed"`, `end_reason: "wrap_up_abandoned"`, `checkin` left as-is (stays null when never collected), file
  moved to `closed/`. Announce briefly — exactly: "Closing session ID.
  Skipping end-of-session check-out ritual." This is an announcement with an
  undo, NOT a question — do not ask for confirmation. If the user objects,
  the next prompt arrives inside a short reopen window; the objection is
  handled there (see REOPEN WINDOW below — run
  `state.py reopen-session <session_id>` to restore the session; its goals
  stay closed and the GOAL NEEDED mini-ritual re-engages naturally on the
  next prompt).
- `[TANK — REOPEN WINDOW | session_id: ID | seconds_since_sweep: N]` — the
  one-shot undo turn right after a STALE CLOSING FINALIZED announcement.
  Session ID was finalized as abandoned moments ago; no session has been
  created this turn. If the user is objecting to that closure, run
  `state.py reopen-session <session_id>` to restore it. Otherwise respond
  normally — a new session will start on the next prompt. If the user is
  clearly starting new work right now, run `state.py start-session`
  explicitly. A late 2x2 reply arriving at or after this point is NOT
  retro-recorded; the wrap-up was skipped and announced as such — do not
  run `end-session` on the swept session for a late reply. The hook has
  already skipped logging on this turn; do not start any ritual (no startup
  ritual, no goal ritual).

### Multi-Orphan Handling

Fires when `[TANK — MULTI_SESSION DETECTED]` is received.

**Cause:** Multiple open or closing sessions found at the top level of `sessions/` — typically from botched closes, racy starts, or sessions abandoned mid-close across different IDE windows or machines.

**Step 1 — Surface and offer wrap-up (single message):**

Using the `orphan_session_ids` and `orphan_last_active` from the signal, list
each orphan session with a relative last-activity label. Format:

```
───────────────────────────────────────
🦉 Found N open sessions:

   • SESSION_ID (last activity X hours ago)
   • SESSION_ID (last activity Y minutes ago)
   ...

   The most recent one (ACTIVE_ID) is
   ready to continue.

   Wrap up the others and start fresh?
   [y] Yes — close them  [n] No, leave them
───────────────────────────────────────
```

Where "last activity" is derived from `orphan_last_active` in the signal. Use
human-readable relative time: "18 hours ago", "3 minutes ago", etc. If the
signal lacks a timestamp for a session, show the session ID without a label.

**Step 2 — On YES:**

For each orphan session ID, call:

```
state.py end-session <session_id> '{"collection_method": "timeout_confirmed", "bulk_confirmed": true}'
```

Use the orphan's last-active timestamp as `end_time_iso` if available:

```
state.py end-session <session_id> '{"collection_method": "timeout_confirmed", "bulk_confirmed": true}' <last_active_iso>
```

After closing all orphans, resume the active session (the most recently active
one the hook already selected). Run the Startup Ritual for the active session
if it has no `start_checkin` yet; otherwise resume silently.

**Step 3 — On NO:**

Leave all orphan sessions open. Resume the active session silently.
Log a note but do not nag — the user chose to leave them open.

**Graceful degradation:** If `state.py end-session` fails for any orphan (e.g.
file moved concurrently), log the failure silently and continue. Do not surface
the error to the user unless all closes fail.

**Trust note:** This is not a hard intervention — it is housekeeping surfaced
politely. The user may have good reasons to leave old sessions open. One
confirmation is all we ask; never repeat this prompt for the same set of orphans.

### Startup Ritual

Fires on `[TANK — NEW SESSION]` — either a fresh start or after the user
confirmed a timeout.

**Step 1 — single message, three parts:**

```
───────────────────────────────────────
🦉 Good to have you.

   [Coaching nudge — only if prior
   session timed out without a check-in:
   "Last session ended without a
   check-in — those help me calibrate.
   No big deal, just worth building
   the habit."]

   We're working together — I'll nudge
   at natural seams and flag diminishing
   returns. If you'd rather I stay quiet
   today, use /tank-hard-hat
   quiet.

   What's your goal for this session?
───────────────────────────────────────
```

Store the user's reply as `goal` on the session record. If the user skips or
gives unrecognised input, store null and proceed.

**Step 2 — start 2x2 check-in:**

After the user replies with their goal, show:

```
───────────────────────────────────────
📋 Quick start check-in:

   Energy:  [1] Low ····· [5] High
   Affect:  [1] Bad ····· [5] Good

   Reply with two numbers (e.g. "3 4")

   Or: [z] In the zone  [a] Anxious
       [c] Calm          [d] Drained
───────────────────────────────────────
```

Store as `start_checkin` on the session record. Graceful degradation:
unrecognised input → store null, proceed. No nagging.

### Goal-Only Mini-Ritual

Fires on `[TANK — GOAL NEEDED]` — a new instance joining an active Tank session
that already has a startup check-in. No 2x2. Just a single focused question.

```
───────────────────────────────────────
🦉 You're in an active session.

   What are you working on here?
───────────────────────────────────────
```

Store the user's reply by running:
`state.py add-goal <session_id> <instance_id> <text>`

If the user skips or gives unrecognised input, do not nag — proceed without
storing a goal. The session continues; pomodoro continuity is already tracked
at the Tank-session level.

## Intervention Logic

### Layer 0: Explicit Overwhelm (Immediate)

Fires immediately when the user explicitly signals they are maxed out. No timer,
no heuristic validation needed — the user is telling you directly.

**Detection:** The user's message contains language indicating cognitive overload,
such as: "overwhelmed", "can't think straight", "brain is fried", "maxed out",
"can't focus", "too much", "spinning", "drowning", or similar expressions of
being at capacity. Use judgement — the signal is the user saying they are already
past the point of productive work, not merely that a task is difficult.

**Action:** Skip all other processing and go directly to a restorative break
suggestion. Do not problem-solve, do not help them push through, do not offer
to break the work into smaller pieces. The right move is to step away.

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
across every open instance, not just stepping away for a break. No timer, no
behavioural scoring — the user is telling you directly, same as Layer 0.

**Detection:** The user's message contains language signalling they are closing
down the whole session, such as: "I'm done for the day", "wrap everything up",
"wrap up", "wrapping up", "closing up shop", "calling it", "calling it a day",
"logging off", "I'm out", "done for now", "that's it for today", or similar
expressions of ending the work day entirely. Use judgement — the signal is the
user saying they are stopping all work, not merely finishing one task or asking
for a break. A message like "wrap up this PR" targets a single task, not the
session; do not trigger bulk-close for it.

**Pre-condition:** Only fires when there is an active Tank session (i.e.
`state.py last-session` returns a session in the `open` state — or, for
legacy files with no `state` key, `end_time == null`). If there is no active
session, respond normally without firing.

**Action:** Run the Bulk-Close Procedure (see below).

#### Bulk-Close Procedure

This procedure is shared by two entry points: natural-language bulk-close
detection above, and the `/tank-hard-hat end` command when other
goals are still open (see `/end` below). The behaviour is identical either way.

**Step 1 — Count open goals and instances.** Read the active Tank session
via `state.py last-session`. Filter `session["goals"]` for entries where
`ended_at == null`. Let N = count of open goals; let M = count of distinct
`instance_id` values among those open goals.

**Step 2 — Single confirmation prompt:**

```
───────────────────────────────────────
🦉 Wrap everything up?
   (closes N open goals across M instances)

   [y] Yes — close all  [n] No, leave them
───────────────────────────────────────
```

Replace N and M with the actual counts from step 1. If N == 1 and M == 1,
the message reads "closes 1 open goal across 1 instance" — still use the
exact count, no special-casing.

**Step 3 — On YES:** For each open goal (those with `ended_at == null`),
call `state.py close-goal <session_id> <goal_id>` in sequence.
Closing the last goal parks the Tank session in the `closing` state
(`close-goal` writes `end_time` but the file stays in `sessions/`). After all
goals are closed, run Layer 3 (the 2x2 wrap-up) with
`end_reason: "explicit_quit"` — the 2x2 reply via `end-session` (or the
stale-closing sweep, if the user walks away) completes the ending.

**Step 4 — On NO or no response:** Leave all goals open. No nag, no follow-up.
Same trust principle as pomodoro-skip handling — one offer, then silence.

**Graceful degradation:** If `state.py close-goal` fails for any goal (e.g.
concurrent modification), log the failure silently, continue closing remaining
goals, and surface a brief note only if the session could not be ended at all.

### Layer 1: Pomodoro Rhythm (Soft Boundary)

Fires when continuous session time approaches ~60 minutes. Context-aware: look for
natural seams before firing — a completed task, a commit, a "that works" or "looks
good" moment, a topic change. A nudge at 55 minutes at a natural pause is better
than 60 minutes mid-thought.

**Detection:** `session_duration_since_last_break >= 55 minutes` AND a natural
breakpoint is detected (or >= 65 minutes regardless — don't wait forever).

**A "break" is:** A gap of 5+ minutes between prompts. Gaps under 5 minutes do not
count as recovery.

**User response handling:**
- User takes a break (gap >= 5 min before next prompt): log as `break_taken`
- User continues immediately: log as `break_skipped`, do not nag
- Break skips feed into hard boundary calculus

### Layer 2: Hard Boundary (Behaviour-Based)

Fires when form-degradation signals converge, independent of the timer. Before
firing, run a meta-prompt to validate the pattern — do not surface false positives.
Accuracy is critical; a wrong call loses trust permanently.

**Signals to evaluate (see references/signals.md for detail):**

High confidence:
- Error-retry loops: same or similar prompts repeated with small variations
- Diminishing returns: more prompts for smaller gains
- Scope creep within session: tasks getting broader/more ambitious

Medium confidence:
- Decision deferral: "just get it working" increasing
- Narrowing curiosity: transactional, no "why" questions
- Completion fixation: goalpost moves but refusal to pause
- Parallel thread accumulation: rising active workstream count
- Boundary crossing: session started earlier or running later than user baseline

Lagging (already past the ideal intervention point):
- Prompt tone shift: terse, frustrated language

**Trigger threshold:** At least 2 high-confidence signals OR 1 high + 2 medium
signals within the last 20 minutes. Always validate with a meta-prompt before
firing. (Canonical weights and the exact score threshold live in
`references/signals.md`.)

**Record each signal as you detect it.** When you judge that a signal is
present, persist it before deciding whether to fire:

```
state.py log-signal <session_id> <instance_id> '{"name": "<signal>", "weight": <int>, "confidence": "high|medium|low", "evidence": "<one-line basis>"}'
```

Use the `session_id` and `instance_id` from the hook context. Recording is not
the same as firing — you log every detected signal, then score the last 20
minutes and run the meta-prompt to decide whether to surface the nudge. The
persisted signals are what `get-session-summary` returns as `fired_signals`.

**Meta-prompt for validation:** Before showing the hard nudge, internally assess:
"Looking at the last 20 minutes of this session — the prompt patterns, specificity,
retry count, and scope trajectory — is this developer showing signs of diminishing
returns, or are they still in productive flow? Be specific about which signals are
present and which are absent. If the evidence is ambiguous, do not intervene."

**Hook-triggered — do not rely on self-auditing.** You will not reliably notice
Layer 2 on your own while working a task. The hook watches for you: when its
mechanical proxies (retry-classified prompts in the last 20 minutes) cross a low
bar, it emits `[TANK — HARD BOUNDARY CHECK | session_id: … | retry_loops: …]`.
When you see that signal, run this check before responding:

1. `state.py get-session-summary <session_id>` — read the accumulated
   `fired_signals` and aggregates.
2. Detect any new form-degradation signals in the current prompt (above) and
   record each via `state.py log-signal <session_id> <instance_id> '{…}'`.
3. Score the last 20 minutes (weights + threshold in `references/signals.md`).
4. Run the meta-prompt. **If ambiguous, do not fire** — a wrong call loses trust
   permanently; a missed one does not.
5. If it fires, prepend the Hard Boundary nudge; otherwise respond normally.

The hook only *triggers* the check — it cannot score (most signals are semantic).
The fire/no-fire decision is yours.

### Layer 3: Session End Check-in (2x2)

Fires when the **last open goal** in the Tank session is closed — either via
`/tank-hard-hat end` (explicit quit, which closes this instance's
goal) when no other goals remain, OR when the user confirms "yes" on timeout
detection. In both cases the user is present.

If `/tank-hard-hat end` is run but other goals are still open,
Layer 3 does NOT fire. Instead, announce the open goals and offer a bulk-close
(see `/tank-hard-hat end` below).

`end_reason` values: `"explicit_quit"` or `"timeout_confirmed"`.

**Lifecycle:** `close-goal` on the last open goal parks the session in the
`closing` state — `end_time`, `end_reason`, and duration are written, but the
file stays at `sessions/` top level. The 2x2 reply, persisted via
`end-session`, finalizes the session (`state: "closed"`, moved to `closed/`).
If the wrap-up is abandoned for longer than `closing_window_seconds`
(default 300), the hook sweeps the session closed with
`end_reason: "wrap_up_abandoned"` and `checkin` left as-is (stays null when never collected), and emits
`[TANK — STALE CLOSING FINALIZED]` (see the signal list above).

Collects the user's energy and affect on two axes, mapping to four quadrants:
- High Energy + Feels Good = In the zone
- High Energy + Feels Bad = Anxious / overstimulated
- Low Energy + Feels Good = Calm
- Low Energy + Feels Bad = Drained

Plus a relative mood comparison against the start of the session.

## Intervention Formats

### Onboarding (first run)

Fires on the first invocation, when `~/.tank/` does not exist. Runs in two
steps — permissions first, then tracking mode — so the second step and
everything after it runs silently.

**Step 1 — permissions:**

```
───────────────────────────────────────
🦉 Tank — first run

   I'll help you balance intensity and
   recovery. Here's how it works:

   • ~60 min check-ins at natural pauses
   • A heads-up if I spot diminishing returns
   • A quick 2-question check-in at session end

   Two setup questions.

   (1) I need two rules allowlisted so
   I can work silently — otherwise you'll
   get a permission prompt on every hook
   firing:

     • Read access to ~/.tank/**
     • Bash access to the state.py script

   [a] Auto-add them to
       ~/.claude/settings.json (recommended)
   [p] Show me the exact lines — I'll
       paste them myself
   [x] Skip — I'll approve prompts every
       time instead

   Reply with a letter.
───────────────────────────────────────
```

Dispatch on the user's reply:

- `a` → run `state.py setup-permissions`. This approval will prompt once
  (expected — we don't have the allowlist yet); after it runs, subsequent
  state.py and ~/.tank/ access is silent.
- `p` → print the two rules with the user's actual home path expanded, and
  tell them to paste into `~/.claude/settings.json` under `permissions.allow`.
  Do not write anything. The rules are exactly what `state.py setup-permissions`
  would write — format:
  - `Bash(python3 <HOME>/.claude/skills/tank-hard-hat/scripts/state.py *)`
  - `Read(<HOME>/.tank/**)`
- `x` → proceed without setup. Accept that future prompts will fire.
- Unrecognised or empty → default to `a`.

**Step 2 — tracking mode:**

```
───────────────────────────────────────
🦉 How should I track your prompts to
   detect patterns like retries?

   [f] Fingerprint — lightweight keyword
       matching (default)
   [s] Semantic summary — richer pattern
       detection. A semantic summary will
       help provide an overview of your
       week's main stressors and recovery.

   Reply with a letter (f or s)
   or just hit enter for default (f).
───────────────────────────────────────
```

After the user responds (or hits enter), write choices to `config.json`:
- `prompt_tracking_mode`: `"fingerprint"` if "f", `"summary"` if "s", default `"fingerprint"`
- `onboarding_complete`: `true`

If the user gives an unrecognised response, use defaults. Onboarding does not
re-fire once `onboarding_complete` is `true`.

### Migration (existing users)

Fires once when `prompt_tracking_mode` key is missing from an existing config
(i.e. `onboarding_complete` is `true` but the key is absent).

```
───────────────────────────────────────
🦉 Tank — new setting

   Prompt tracking mode?
   [f] Fingerprint (default)
   [s] Semantic summary — will help
       provide an overview of your week's
       main stressors and recovery.

   Reply with a letter (f or s)
───────────────────────────────────────
```

After the user responds, write choice to `config.json`. If the user skips or
gives an unrecognised response, use default (`"fingerprint"`).

### Pomodoro Nudge (soft boundary)

```
───────────────────────────────────────
⏱  60 minutes in. Good stopping point.
   Take 5 — I'll be here when you get back.
───────────────────────────────────────
```

### Hard Boundary Nudge

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
observations, do not fire the hard nudge.

### Session End Check-in

**Step 1 — 2x2 + relative mood (single prompt):**

```
───────────────────────────────────────
📋 Session wrap — quick check-in:

   Energy:  [1] Low ····· [5] High
   Affect:  [1] Bad ····· [5] Good

   Reply with two numbers (e.g. "3 4")

   Or: [z] In the zone  [a] Anxious
       [c] Calm          [d] Drained

   Vs. start of session:
       [b] Better  [s] Same  [w] Worse

   Reply e.g. "3 4 b" or "z b" or "skip"
───────────────────────────────────────
```

**Persist the reply by running exactly:**

```
state.py end-session <session_id> '{"energy": E, "affect": A, "quadrant": Q, "relative_mood": "better|same|worse", "collection_method": "explicit_quit", "captured_at": "<ISO>"}'
```

`end-session` is the single canonical writer for the 2x2. The normal path:
the session is in the `closing` state (parked there by `close-goal` on the
last goal, file still at `sessions/` top level), and `end-session` finalizes
it — records the check-in, sets `state: "closed"`, and moves the file to
`closed/`. It also works from `open` (timeout-confirmed closes, where it
writes `end_time` itself) and on sessions already in `closed/` (legacy
compat — no longer the primary path). It stores the whole object under
`checkin` (per the data model, `relative_mood` lives *inside* `checkin`) and
sets `end_reason` from `collection_method`.

Do NOT reach for `state.py set-field checkin ...` here — `end-session` is the
intended path. (`set-field checkin`/`relative_mood` are accepted as a
graceful-degradation fallback so a wrong guess no longer hard-fails, but they
skip the `end_reason`/duration bookkeeping that `end-session` does.)

**Step 2 — Reflection nudge (not captured):**

```
───────────────────────────────────────
📋 Worth leaving yourself a note in the
   repo — what you got done, what's
   parked, what to pick up next time.
───────────────────────────────────────
```

No response expected. No data stored. Just a nudge toward the practice.

## Slash Commands

`/tank-hard-hat <arg>` invokes the skill with an argument.
Dispatch on the arg below. Any unrecognised arg (including `--help`, `-h`,
or a typo) dispatches to `help`.

### `/tank-hard-hat help`

Show the command reference. Output exactly this, nothing else:

```
───────────────────────────────────────
🦉 Tank — commands

   /tank-hard-hat
       Normal passive operation (no arg).
       Runs on every prompt anyway via
       the hook; this invocation is a
       no-op unless an intervention
       condition is met.

   /tank-hard-hat quiet
       Suppress pomodoro and hard-boundary
       nudges for this session. Layer 0
       (explicit overwhelm) still fires.

   /tank-hard-hat resume
       Re-enable nudges after quiet mode.

   /tank-hard-hat end
       Close this window's goal. If other
       goals are still open, announces them
       and offers bulk-close. The 2x2 wrap-up
       fires only when the last goal closes.

   /tank-hard-hat help
       Show this help (aliases: --help, -h;
       any unknown arg also shows help).
───────────────────────────────────────
```

Do not interpret a `help` invocation as a real prompt — do not log it as a
session event, do not run signal detection, do not perform any other work.
Just print the reference and stop.

### `/tank-hard-hat quiet`

Sets `quiet_mode: true` on the current session. Suppresses Layers 1, 2, and 3.
Layer 0 (explicit overwhelm) still fires — that one is non-negotiable.

Implementation: read current session via `state.py last-session`, then run
`state.py set-field <id> quiet_mode true`.

Confirm with:
```
───────────────────────────────────────
🦉 Quiet mode on. I'll only speak up
   if you sound like you're hitting a
   wall. Use /tank-hard-hat
   resume to turn nudges back on.
───────────────────────────────────────
```

### `/tank-hard-hat resume`

Sets `quiet_mode: false` on the current session. Undoes quiet mode.

Implementation: `state.py set-field <id> quiet_mode false`.

Confirm with:
```
───────────────────────────────────────
🦉 Nudges back on.
───────────────────────────────────────
```

### `/tank-hard-hat end`

Closes the calling instance's open goal only.

**Implementation:**
1. Identify the current instance's `instance_id` (the Claude Code `session_id`
   from the hook payload — available as the identifier of this conversation).
2. Find the active Tank session via `state.py last-session`.
3. Look up the open goal owned by this instance_id in `session["goals"]`.
4. Call `state.py close-goal <session_id> <goal_id>`.

**Dispatch on the result:**

If `session_ended: false` (other goals are still open): announce the remaining
open goals by name, then run the **Bulk-Close Procedure** (see Bulk-Close
Intent above) to offer closing them all at once:

```
───────────────────────────────────────
🦉 Goal closed.

   Still open in other windows:
     • [goal text] (instance: [id])
     [... one line per open goal ...]
───────────────────────────────────────
```

Then immediately show the Bulk-Close Procedure's Step 2 confirmation prompt
(N = remaining open goal count, M = distinct instance count among them).

If `session_ended: true` (this was the last goal): the session is now parked
in the `closing` state (`close-goal` returns `"state": "closing"`). Run
Layer 3 immediately with `end_reason: "explicit_quit"`; the 2x2 reply via
`end-session` finalizes the session.

## Data Model

All data stored in `~/.tank/` as JSON files. Sessions are the source of truth.
Dailies and retros are computed from sessions and can be regenerated.

See `references/data-model.md` for complete schema definitions.

### Session Lifecycle

A session's lifecycle is a three-state machine. The canonical state lives in
the session's `state` field; file location is a derived index.

```
open ──(close-goal, last goal)──▶ closing ──(end-session, 2x2)──▶ closed
  ▲                                  │
  └────────(reopen-session)──────────┴──(stale: hook sweep finalizes)──▶ closed
```

- `open` — session active; goals may be open. `end-session` can also close
  directly from here (timeout-confirmed close).
- `closing` — work has ended (`end_time` set by `close-goal`) but the 2x2
  wrap-up is still pending. The file stays at `sessions/` top level. A
  `closing` session older than `closing_window_seconds` (default 300,
  measured from `end_time`) is stale: the hook finalizes it with
  `end_reason: "wrap_up_abandoned"`.
- `closed` — terminal. `checkin: null` on a closed session means the wrap-up
  was never collected.
- `state.py reopen-session <id>` is the undo: restores `open`, clears
  `end_time`/`end_reason` (and the `reopen_offer_pending` flag), and moves
  the file back to `sessions/` if needed. The prompt right after a sweep
  announcement lands in a one-shot reopen window (`[TANK — REOPEN WINDOW]`)
  where no replacement session is created, so the undo never leaves a
  phantom session behind.

### File Structure

```
~/.tank/
  config.json
  sessions/
    YYYY-MM-DD_NNN.json    # open or closing session (state field is canonical)
    closed/
      YYYY-MM-DD_NNN.json  # finalized session (state: "closed")
  dailies/
    YYYY-MM-DD.json        # daily rollup
  retro/
    YYYY-WNN.json          # weekly rollup
```

Open and closing sessions live at `sessions/` top level (top level = needs
attention); when `end-session` finalizes a session it sets `state: "closed"`
and renames the file into `sessions/closed/`. Active-session discovery
therefore only needs to scan the top level. Lazy migration moves a top-level
file to `closed/` only when `state == "closed"` (or, for legacy files with no
`state` key, when `end_time` is set); it never moves `closing` files.

## Prompt Content Capture

This section describes what the hook computes and stores on every prompt
event — it is reference material, not instructions for Claude to execute.
The content tracking mode is determined by `prompt_tracking_mode` in config.

### Fingerprint Mode (default)

The hook extracts a compact keyword object from the user's prompt:

```json
{
  "keywords": ["auth", "middleware", "session"],
  "files": ["src/middleware/auth.ts"],
  "intent_verb": "fix",
  "target": "auth middleware session handling"
}
```

- **keywords**: 3-6 top content words (nouns, verbs — skip stop words)
- **files**: file paths mentioned in the prompt
- **intent_verb**: the primary action (fix, add, list, debug, refactor, explain, etc.)
- **target**: short phrase describing what the prompt is about

Stored in `content_fingerprint` on the prompt event; `content_summary` is `null`.

### Summary Mode (opt-in)

Intended shape: a one-line intent summary of the prompt, stored in
`content_summary`.

**Current implementation status:** per-prompt capture in summary mode is
identical to fingerprint mode — the hook stores the mechanical fingerprint
(used for retry detection either way) and leaves `content_summary` as `null`.
Semantic summaries are a retro-time enrichment that is not yet generated;
nothing writes `content_summary` per-prompt, and Claude must not start. The
config value is still accepted and recorded so existing opt-ins are preserved
for when the enrichment lands.

**Known limitation (of the intended design):** a generated summary that
misses key details, over-generalises, or conflates distinct intents degrades
downstream detection silently — there is no validation layer. Fingerprint
mode does not have this problem because extraction is mechanical, which is
why detection runs on fingerprints in both modes.

### Per-Prompt Fields

Fields the hook writes on every prompt event. The envelope fields come from
the hook payload and clock — their absence from earlier versions of this
list is how unreadable events were once written, so the event shape is
documented here in full:

- `timestamp` — ISO 8601 with timezone (envelope; required — readers sort
  and compute gaps from it)
- `type` — `"prompt"` (envelope)
- `instance_id` — the Claude Code session_id from the hook payload (envelope)
- `goal_id` — the open goal owned by this instance, or null (envelope)
- `cwd` — working directory from the hook payload, when present (envelope)
- `content_fingerprint` — always the mechanical fingerprint; `content_summary`
  stays `null` (see Summary Mode above)
- `retry_similarity` — see Retry Detection below
- `classification` — `new_task` / `continuation` / `retry` / `refinement`
- `specificity_score` — heuristic: file references (+0.2 each, max 0.4), line numbers (+0.1), technical terms (+0.1), concrete nouns (+0.1), length > 50 tokens (+0.1), question marks (+0.1). Capped at 1.0
- `task_id` — inferred from file paths, branch, or topic keywords
- `has_questions` — presence of question marks or exploratory language
- `signals_fired` — any signals detected on this prompt

### Edge Case

Tool-loading prompts (e.g. "Tool loaded.") should not be scored for content.
Detection: prompts under 5 tokens that are direct responses to system prompts
get classified as `continuation`, not scored for similarity.

## Retry Detection

Computed by the hook on every prompt, in both tracking modes, from the
mechanical fingerprint:

1. Extract keywords, files, intent_verb, and target from the current prompt
2. Compare against the last 5 stored fingerprints
3. Score overlap: keyword Jaccard similarity (60%), intent_verb exact match (20%), target exact match (20%)
4. If combined score >= 0.6 against any recent prompt, classify as `retry`

Consequences:

- A `retry` classification increments the session's `retry_loop_count` aggregate
- Adds `"retry_loop"` to `signals_fired` if 2+ retries within a 20-minute window
- Feeds into hard boundary scoring (weight 3, highest)

Claude reads these outcomes from session data when validating a hard
boundary; it never computes or stores them.

## Signal Detection via Content

Signals newly enabled by prompt content tracking:

- ~~**Declining specificity**~~ — **Retired.** `specificity_score` is still computed and stored for retro descriptive data, but does not drive signals. Raw specificity drifts in meaning as session context accumulates — a terse prompt late in a session may be precise, not vague. Actual communication failure is captured by retry_loop and diminishing_returns signals instead.
- **Narrowing curiosity** — track `has_questions` rate. Flag if it drops from baseline by 50%+. Adds `"narrowing_curiosity"` to `signals_fired`.
- **Decision deferral** — in summary mode, detect "just make it work" / "good enough" / "worry about later" intent. In fingerprint mode, scan keywords for these phrases. Adds `"decision_deferral"` to `signals_fired`.
- **Completion fixation** — detect "also", "one more thing", "before we stop" patterns after a pomodoro nudge was fired or skipped. Adds `"completion_fixation"` to `signals_fired`.

Signals already functional (timestamps/gaps only): pomodoro rhythm, boundary crossing.

Signals now functional via task_id tracking: scope creep, parallel thread accumulation, diminishing returns.

Unchanged: prompt tone shift (lagging signal, requires raw text sentiment analysis beyond content tracking).

## Prompt Quality Scoring

Two-tier approach:

**Default (every prompt):** Simple heuristics — token count, presence of file/line
references, question marks, retry detection (similarity to recent prompts),
task classification (new / continuation / retry). Zero token cost.

**At checkpoints (meta-prompt):** Claude Code self-scores recent prompt quality
and session trajectory. Fires at:
- Pomodoro boundary (~60 min) — light assessment
- Hard boundary evaluation — deeper analysis of recent prompts
- Session end — alongside the 2x2, score the session overall

## Configuration

See `references/config-schema.md` for the full config specification.

Key tuneable values in `~/.tank/config.json`:
- `pomodoro_interval_minutes`: default 60
- `min_break_duration_minutes`: default 5
- `session_timeout_minutes`: defaults to the value of `pomodoro_interval_minutes`.
  Gap beyond this triggers timeout detection rather than silent rollover.
- `closing_window_seconds`: default 300. How long a session may sit in the
  `closing` state (wrap-up pending) before the hook sweeps it closed with
  `end_reason: "wrap_up_abandoned"`.
- `hard_boundary_sensitivity`: "low" | "medium" | "high", default "medium"
- `intrusiveness`: "high" | "medium" | "low", default "high" (adapts over time)
- `work_hours`: { start: "09:00", end: "18:00" } (for boundary crossing detection)
- `prompt_tracking_mode`: "fingerprint" | "summary", default "fingerprint"

Per-session (not in config — set via slash commands):
- `quiet_mode`: boolean, default false. Suppresses Layers 1/2/3 when true.

## Retro / Canvas

When the user asks for a retro or weekly summary, read from `~/.tank/retro/`
and present:
- Trends: session length, break compliance, scope breadth
- Patterns: boundary creep, recovery debt accumulation
- 2x2 trajectory: where is the user landing most often?
- Recommendations grounded in what the data shows

This output is designed to feed into a Tank canvas for deeper reflection.
The skill does not prescribe — it surfaces patterns for the user to interpret.
