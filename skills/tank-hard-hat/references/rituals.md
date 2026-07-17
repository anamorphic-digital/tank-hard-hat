# Session Lifecycle Rituals

Procedures and exact output formats for session lifecycle moments: onboarding,
session start, goal collection, orphan cleanup, bulk-close, and the
end-of-session wrap-up. SKILL.md's dispatch table says *when* each of these
fires; this file says *how* to run it. The boxed formats are contracts —
reproduce them exactly, do not improvise.

## Onboarding (first run)

Fires on the first invocation, when `~/.tank/` does not exist. Show the
message first — only after showing it do you create the directory structure
(`~/.tank/{config.json,sessions/,dailies/}`) and write default config.

```
───────────────────────────────────────
🦉 Tank — first run

   I'll help you balance intensity and
   recovery. Here's how it works:

   • ~60 min check-ins at natural pauses
   • A heads-up if I spot diminishing returns
   • A quick 2-question check-in at session end

   One setup question.

   I need two rules allowlisted so
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
- `p` → print the two rules with real paths expanded, and tell them to paste
  into `~/.claude/settings.json` under `permissions.allow`. Do not write
  anything. The rules are exactly what `state.py setup-permissions` would
  write — the Bash rule embeds the path this install actually invokes
  `state.py` through (symlinked skill dir, plugin cache, or repo checkout):
  - `Bash(python3 <path-to-this-install>/scripts/state.py *)`
  - `Read(<HOME>/.tank/**)`
- `x` → proceed without setup. Accept that future prompts will fire.
- Unrecognised or empty → default to `a`.

After dispatching, set `onboarding_complete: true` in `config.json`.
Onboarding does not re-fire once `onboarding_complete` is `true`.

Prompt tracking is fingerprint-based and is not a setup question — there is
nothing to choose. (A legacy `prompt_tracking_mode: "summary"` value in an
existing config is accepted and currently behaves identically to
fingerprint; do not ask users to pick a mode.)

## Startup Ritual

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
   returns. If you'd rather I stay
   quiet today, use /tank-hard-hat quiet.

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

## Goal-Only Mini-Ritual

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

## Multi-Orphan Handling

Fires when `[TANK — MULTI_SESSION DETECTED]` is received.

**Cause:** Multiple open or closing sessions found at the top level of
`sessions/` — typically from botched closes, racy starts, or sessions
abandoned mid-close across different IDE windows or machines.

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

## Bulk-Close Procedure

Shared by two entry points: natural-language bulk-close detection (SKILL.md:
Bulk-Close Intent) and the `/tank-hard-hat end` command when
other goals are still open. The behaviour is identical either way.

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
goals are closed, run the Session End Check-in (below) with
`end_reason: "explicit_quit"` — the 2x2 reply via `end-session` (or the
stale-closing sweep, if the user walks away) completes the ending.

**Step 4 — On NO or no response:** Leave all goals open. No nag, no follow-up.
Same trust principle as pomodoro-skip handling — one offer, then silence.

**Graceful degradation:** If `state.py close-goal` fails for any goal (e.g.
concurrent modification), log the failure silently, continue closing remaining
goals, and surface a brief note only if the session could not be ended at all.

## Session End Check-in (Layer 3)

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

Quadrant mapping: High Energy + Feels Good = In the zone; High Energy +
Feels Bad = Anxious / overstimulated; Low Energy + Feels Good = Calm;
Low Energy + Feels Bad = Drained.

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
