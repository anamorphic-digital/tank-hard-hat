# Data Model Reference

## File Structure

```
~/.tank/
  config.json
  sessions/
    YYYY-MM-DD_NNN.json    # open or closing session metadata (no events embedded)
    closed/
      YYYY-MM-DD_NNN.json  # finalized session (state: "closed")
  events/
    YYYY-MM-DD_NNN/        # one directory per session_id
      <instance_id>.jsonl  # per-instance append-only event log (one JSON per line)
                           # instance_id = Claude Code's session_id from the hook payload
  dailies/
    YYYY-MM-DD.json        # daily rollup
  retro/
    YYYY-WNN.json          # weekly rollup
```

## Event Log Layout

Events are stored in `~/.tank/events/<session_id>/<instance_id>.jsonl`.

- Each line is a single JSON object (a prompt event) with no trailing comma.
- Lines are appended atomically using Python's file-append mode — partial events
  from crashes are silently skipped by the reader.
- Multiple `.jsonl` files may exist under one session directory (one per
  concurrent instance). `read-session` merges all of them and sorts by timestamp.
- `instance_id` is the Claude Code `session_id` from the `UserPromptSubmit` hook
  payload — stable across all prompts in one conversation, unique per running
  instance. If the payload field is absent (e.g. in tests), the fallback is
  `"unknown"` — a visible sentinel, never `"default"` (which was the pre-#7
  placeholder).

### Retention: events do not outlive the session

The event store is working memory for the *open* session — retry similarity
and hard-boundary scoring, both session-scoped. On the transition into the
terminal `closed` state (either edge: `end-session`, or the hook's
stale-closing sweep) the aggregates are snapshotted onto the session record
(including `task_ids`, so daily workstream breadth survives) and
`events/<session_id>/` — prompt fingerprints, `cwd`, and the `signals/`
evidence — is deleted. A per-prompt GC pass in the hook converges crash
leftovers and legacy dirs. `closing` sessions keep their events: that state
is escapable via `reopen-session`. Secret-shaped tokens (long hex blobs,
long mixed letter+digit runs) are dropped by the fingerprinter before events
are ever written.

### Backward compatibility

Sessions recorded before this layout was introduced store events directly in
`session["prompts"]` inside the session JSON. `read-session` detects the old
format (no `.jsonl` files under `~/.tank/events/<session_id>/`) and falls back
to reading from `session["prompts"]` transparently.

## Per-Prompt Event

Logged on every prompt. Written to the per-instance event log.

```json
{
  "timestamp": "2026-03-14T10:32:15+11:00",
  "instance_id": "abc1234def",
  "goal_id": "goal-20260314103215-4a2f",
  "cwd": "/home/user/myproject",
  "prompt_length_tokens": 142,
  "specificity_score": 0.7,
  "task_id": "auth-middleware",
  "classification": "continuation",
  "gap_since_last_seconds": 45,
  "files_referenced": ["src/middleware/auth.ts"],
  "has_questions": true,
  "retry_similarity": 0.0,
  "signals_fired": [],
  "content_fingerprint": {
    "keywords": ["auth", "middleware", "session"],
    "files": ["src/middleware/auth.ts"],
    "intent_verb": "fix",
    "target": "auth middleware session handling"
  },
  "content_summary": null
}
```

### Fields

- **timestamp**: ISO 8601 with timezone
- **instance_id**: The Claude Code `session_id` from the `UserPromptSubmit` hook payload.
  Identifies which Claude Code conversation/window produced this event. Stable
  across all prompts in one conversation; unique per running instance.
- **goal_id**: The `id` of the open goal owned by this instance at the time of the
  prompt. Null when the instance has no open goal (e.g. the instance joined the
  session but the goal-only mini-ritual has not yet run, or was skipped).
- **cwd**: Working directory of the Claude Code instance at the time of the prompt.
  From the hook payload's `cwd` field. Absent if not provided by the runtime.
- **prompt_length_tokens**: Approximate token count (word count * 1.3)
- **specificity_score**: 0.0-1.0 heuristic based on: file references (+0.2 each, max 0.4), line numbers (+0.1), technical terms (+0.1), concrete nouns (+0.1), length > 50 tokens (+0.1), question marks (+0.1). Capped at 1.0. **Note:** This score is descriptive data for retros only — it does not drive signals. Raw specificity drifts in meaning as session context accumulates (a terse prompt late in a session may be precise, not vague). Actual communication failure is captured by retry_loop and diminishing_returns signals instead.
- **task_id**: Inferred from context — file path, branch, or topic keyword. Used to track workstreams
- **classification**: `"new_task"` | `"continuation"` | `"retry"` | `"refinement"`
  - `new_task`: different task_id from previous prompt
  - `continuation`: same task_id, building on previous response
  - `retry`: same task_id, high similarity to recent prompt (>= 0.6)
  - `refinement`: same task_id, low similarity but same narrow scope
- **gap_since_last_seconds**: Time since previous prompt. Null for first prompt in session
- **files_referenced**: File paths mentioned in the prompt
- **has_questions**: Whether the prompt contains question marks or exploratory language
- **retry_similarity**: 0.0-1.0 similarity score against the most similar of the last 5 prompts
- **signals_fired**: Array of signal names detected on this prompt (e.g. `["retry_loop", "decision_deferral"]`)
- **content_fingerprint**: (fingerprint mode only) Compact keyword object extracted from the prompt. Contains:
  - `keywords`: top content words from the prompt (3-6 items)
  - `files`: file paths mentioned in the prompt
  - `intent_verb`: the primary action verb (e.g. "fix", "add", "list", "debug")
  - `target`: short phrase describing what the prompt is about
  - Set to `null` when `prompt_tracking_mode` is `"summary"`
- **content_summary**: (summary mode only) One-line intent summary of the prompt (e.g. "Fix auth middleware session handling"). Set to `null` when `prompt_tracking_mode` is `"fingerprint"`

## Session File

Open and closing sessions: `~/.tank/sessions/YYYY-MM-DD_NNN.json`
Closed sessions: `~/.tank/sessions/closed/YYYY-MM-DD_NNN.json`

The `state` field is canonical; file location is a derived index. Sessions are
created at the top level of `sessions/` and stay there through the `closing`
state (last goal closed, wrap-up pending). When `end-session` finalizes a
session it sets `state: "closed"` and renames the file into the `closed/`
subdirectory. The top-level `sessions/` directory therefore contains exactly
the sessions that may still need attention (open or closing), making
active-session discovery a bounded, cheap scan.

### Lifecycle

```
        create_session            close-goal (last goal)        end-session (2x2)
             │                            │                            │
             ▼                            ▼                            ▼
          ┌──────┐                   ┌─────────┐                  ┌────────┐
          │ open │ ────────────────▶ │ closing │ ───────────────▶ │ closed │
          └──────┘                   └─────────┘                  └────────┘
             ▲                            │                            │
             │      reopen-session        │  stale (> closing_window)  │
             │◀───────(user said no)──────┤  hook finalizes,           │
             │                            └─ checkin: null ───────────▶│
             └── end-session directly (timeout-confirmed close) ──────▶┘
```

- **open** — session active; goals may be open.
- **closing** — work has ended (`end_time` set by `close-goal`), wrap-up
  pending. The file still occupies `sessions/` top level. Stale after
  `closing_window_seconds` (default 300, measured from `end_time`); the
  hook's sweep then finalizes it with `end_reason: "wrap_up_abandoned"`.
- **closed** — terminal. `checkin: null` on a closed session unambiguously
  means "wrap-up never collected".

`state.py reopen-session <id>` is the undo for a stale-closing finalization:
restores `state: "open"`, clears `end_time`/`end_reason`, and moves the file
back to `sessions/` if needed. Goals stay closed.

**Lazy migration:** On every hook scan, a top-level file is moved to `closed/`
only when its derived state is `closed` — i.e. an explicit `state: "closed"`,
or a legacy file (no `state` key) with `end_time` set. Migration never moves
`closing` files; they stay at the top level until `end-session` or the
stale-closing sweep finalizes them. This is idempotent and requires no
manual step.

This file contains **session metadata only** — prompt events are stored in the
per-instance event logs under `~/.tank/events/<session_id>/`. Use
`state.py read-session <id>` to get a hydrated session that includes merged events.

```json
{
  "session_id": "2026-03-14_001",
  "state": "closed",
  "start_time": "2026-03-14T09:15:00+11:00",
  "end_time": "2026-03-14T11:42:00+11:00",
  "end_reason": "explicit_quit",
  "duration_minutes": 147,
  "start_checkin": {
    "energy": 4,
    "affect": 4,
    "quadrant": "in_the_zone",
    "collected_at": "2026-03-14T09:15:00+11:00"
  },
  "goal": "Ship the auth middleware refactor",
  "goals": [
    {
      "id": "goal-20260314091500-3b1c",
      "instance_id": "abc1234def",
      "text": "Ship the auth middleware refactor",
      "started_at": "2026-03-14T09:15:00+11:00",
      "ended_at": "2026-03-14T11:42:00+11:00"
    },
    {
      "id": "goal-20260314100000-7f2a",
      "instance_id": "xyz9876ghi",
      "text": "Write tests for the billing module",
      "started_at": "2026-03-14T10:00:00+11:00",
      "ended_at": null
    }
  ],
  "quiet_mode": false,
  "prompts": [],
  "aggregates": {
    "prompt_count": 47,
    "active_workstream_count": 3,
    "retry_loop_count": 2,
    "longest_unbroken_stretch_minutes": 63,
    "natural_breakpoints_detected": 4,
    "specificity_trend": "declining",
    "prompts_per_interval": [12, 15, 20]
  },
  "interventions": {
    "pomodoro_nudges": [
      {
        "fired_at": "2026-03-14T10:18:00+11:00",
        "response": "break_taken",
        "break_duration_minutes": 7
      }
    ],
    "hard_boundary_nudges": [
      {
        "fired_at": "2026-03-14T11:35:00+11:00",
        "signals": ["retry_loop", "decision_deferral"],
        "signal_score": 6,
        "meta_prompt_confirmed": true,
        "evidence_shown": "Last 20 mins: 4 retries on auth middleware, prompts getting shorter.",
        "response": "break_taken"
      }
    ]
  },
  "checkin": {
    "energy": 2,
    "affect": 3,
    "quadrant": "calm",
    "collected_at": "2026-03-14T11:42:00+11:00",
    "collection_method": "explicit_quit",
    "relative_mood": "same"
  },
  "meta_scores": [
    {
      "at": "2026-03-14T10:18:00+11:00",
      "type": "pomodoro",
      "assessment": "Productive flow. Clear task progression, good specificity."
    },
    {
      "at": "2026-03-14T11:35:00+11:00",
      "type": "hard_boundary",
      "assessment": "Diminishing returns confirmed. Retry pattern on auth middleware, prompt length declining."
    }
  ]
}
```

### Session-level Fields

- **session_id**: `YYYY-MM-DD_NNN` format
- **state**: `"open"` | `"closing"` | `"closed"` — the canonical lifecycle
  state; file location is derived from it, not the other way around.
  - `open`: session active; goals may be open.
  - `closing`: the last open goal was closed — `close-goal` set `end_time`,
    `end_reason`, and `duration_minutes` — but the 2x2 wrap-up is still
    pending. The file stays at `sessions/` top level.
  - `closed`: terminal; the file lives in `sessions/closed/`.
  - **Legacy derivation:** files without a `state` key derive it on read —
    `end_time == null` → `"open"`, otherwise `"closed"`. The derived value is
    written back on the next touch. (Unrecognised values are treated as
    absent and derived the same way.)
- **start_time / end_time**: ISO 8601 with timezone
- **end_reason**: `"explicit_quit"` | `"timeout_confirmed"` | `"timeout"` | `"wrap_up_abandoned"` | null. `timeout_confirmed` = user confirmed session end after idle gap. `timeout` = auto-closed (stale session, no user present). `wrap_up_abandoned` = written by the hook's stale-closing sweep when a `closing` session outlived `closing_window_seconds` without its 2x2 being collected.
- **duration_minutes**: Wall-clock duration from start_time to end_time (or last prompt if session still open)
- **swept_at** (sweep bookkeeping): ISO 8601 timestamp written by the
  hook's stale-closing sweep at finalization time. Historical — preserved by
  `reopen-session`, so a reopened (`open`) session may carry this field as
  historical data. Absent on sessions never swept.
- **reopen_offer_pending** (sweep bookkeeping): Boolean. Set to `true` by
  the sweep alongside `swept_at`; the hook's one-shot reopen window (the
  prompt right after the sweep announcement, within `closing_window_seconds`
  of `swept_at`) consumes it — cleared to `false` when the window fires,
  when it expires, or by `reopen-session`. While `true`, the next
  no-open-sessions prompt emits `[TANK — REOPEN WINDOW]` instead of
  creating a session, so an undo never leaves a phantom behind. When
  multiple sessions are swept in one turn, only the mtime-newest candidate's
  flag is consumed by the reopen window; any remaining `true` flags on other
  swept sessions are inert — the time backstop (`closing_window_seconds`)
  prevents them from firing. Absent on sessions never swept.
- **start_checkin**: Energy/affect snapshot at session start. Same schema as `checkin` but without `collection_method` and `relative_mood`. Fields:
  - `energy`: int 1-5 (1 = low, 5 = high)
  - `affect`: int 1-5 (1 = bad, 5 = good)
  - `quadrant`: `"in_the_zone"` | `"anxious"` | `"calm"` | `"drained"`
  - `collected_at`: ISO 8601 timestamp
  - Null if skipped or not yet collected.
- **goal**: Free-text string describing the session intention (legacy single-goal field from
  before multi-instance support). Null if not set or skipped. Superseded by `goals` for
  multi-instance sessions; retained for backward compatibility.
- **goals**: Array of goal objects, one per instance that has joined this Tank session.
  A Tank session ends when its last open goal closes (or on timeout). Schema per goal:
  - `id`: Unique goal identifier (`goal-YYYYMMDDHHMMSS-XXXX` format).
  - `instance_id`: The Claude Code `session_id` of the instance that owns this goal.
    One instance owns at most one open goal at a time.
  - `text`: Free-text description of what the instance is working on. Collected via
    the goal-only mini-ritual when the instance joins an active session.
  - `started_at`: ISO 8601 timestamp when the goal was created.
  - `ended_at`: ISO 8601 timestamp when the goal was closed, or null if still open.
    Set by `state.py close-goal`. When all goals have `ended_at` set, the session ends.
- **quiet_mode**: Boolean. When true, Layer 1 (pomodoro) and Layer 2 (hard boundary) interventions are suppressed for this session. Default: false.
- **checkin**: End-of-session 2x2 check-in. On a `closed` session,
  `checkin: null` unambiguously means the wrap-up was never collected
  (skipped, or abandoned and swept). Fields:
  - `energy`: int 1-5
  - `affect`: int 1-5
  - `quadrant`: `"in_the_zone"` | `"anxious"` | `"calm"` | `"drained"`
  - `collected_at`: ISO 8601 timestamp
  - `collection_method`: `"explicit_quit"` | `"idle_timeout"`
  - `relative_mood`: `"better"` | `"same"` | `"worse"` | null — how the user feels compared to session start. Only meaningful when both `start_checkin` and `checkin` were collected.

### Aggregates (computed at session end or on demand)

- **prompt_count**: Total prompts in session
- **active_workstream_count**: Distinct task_ids seen
- **retry_loop_count**: Number of prompts classified as `retry`
- **longest_unbroken_stretch_minutes**: Longest gap-free period (no gap > 5 min)
- **natural_breakpoints_detected**: Count of moments where a task completed or topic changed after 45+ minutes
- **specificity_trend**: `"stable"` | `"improving"` | `"declining"` — linear trend of specificity_score over session
- **prompts_per_interval**: Prompt count per 20-minute interval (for acceleration detection)

## Daily Rollup

One file per day: `~/.tank/dailies/YYYY-MM-DD.json`

Computed from all session files for that day.

```json
{
  "date": "2026-03-14",
  "total_active_minutes": 210,
  "session_count": 2,
  "avg_session_length_minutes": 105,
  "pomodoro_compliance_rate": 0.67,
  "hard_boundary_triggers": 1,
  "hard_boundary_signals": ["retry_loop", "decision_deferral"],
  "longest_unbroken_stretch_minutes": 63,
  "first_session_start": "09:15",
  "last_session_end": "17:42",
  "workstream_breadth": 5,
  "checkins": [
    { "quadrant": "in_the_zone", "session": "2026-03-14_001" },
    { "quadrant": "drained", "session": "2026-03-14_002" }
  ]
}
```

### Fields

- **pomodoro_compliance_rate**: breaks_taken / nudges_fired
- **hard_boundary_signals**: union of all signals across the day's hard nudges
- **workstream_breadth**: distinct task_ids across all sessions
- **first_session_start / last_session_end**: for boundary creep detection (compare across days)

## Weekly/Fortnightly Retro

One file per week: `~/.tank/retro/YYYY-WNN.json`

```json
{
  "week": "2026-W11",
  "period_start": "2026-03-09",
  "period_end": "2026-03-15",
  "days_active": 5,
  "total_active_minutes": 1890,
  "avg_daily_active_minutes": 378,
  "trends": {
    "session_length": "increasing",
    "break_compliance": "decreasing",
    "scope_breadth": "stable",
    "boundary_creep": "mild"
  },
  "quadrant_distribution": {
    "in_the_zone": 3,
    "anxious": 1,
    "calm": 4,
    "drained": 2
  },
  "recovery_debt_score": 4.2,
  "patterns": [
    "Sessions getting 15 min longer on average vs last week",
    "Friday sessions ending 90 min later than Monday",
    "3 of 5 hard nudges were retry-loop related"
  ],
  "daily_summaries": [ /* refs to daily files */ ]
}
```

### Recovery Debt Score

A running heuristic (0-10 scale, higher = more debt):

- +1 for each pomodoro break skipped
- +2 for each hard boundary ignored (continued without break)
- +1 for each session ending in "drained" quadrant
- +0.5 for each day with boundary creep (start earlier or end later than baseline)
- -1 for each session ending in "calm" quadrant
- -0.5 for each pomodoro break taken that lasted 10+ minutes
- Floor at 0, cap at 10

This is deliberately simple and meant to be a conversation starter for the retro,
not a precise measurement.

## Quadrant Mapping

From the two numeric inputs (energy 1-5, affect 1-5):

| Energy | Affect | Quadrant |
|--------|--------|----------|
| >= 3   | >= 3   | in_the_zone |
| >= 3   | < 3    | anxious |
| < 3    | >= 3   | calm |
| < 3    | < 3    | drained |

Shortcut inputs map directly: `z` → in_the_zone, `a` → anxious, `c` → calm, `d` → drained
