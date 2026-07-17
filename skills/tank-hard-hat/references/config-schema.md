# Config Schema Reference

Default config file: `~/.tank/config.json`

```json
{
  "version": 1,
  "pomodoro_interval_minutes": 60,
  "min_break_duration_minutes": 5,
  "session_timeout_minutes": null,
  "closing_window_seconds": 300,
  "hard_boundary_sensitivity": "medium",
  "intrusiveness": "high",
  "work_hours": {
    "start": "09:00",
    "end": "18:00"
  },
  "timezone": "Australia/Sydney",
  "recovery_suggestions_enabled": true,
  "onboarding_complete": false,
  "prompt_tracking_mode": "fingerprint"
}
```

## Field Descriptions

### pomodoro_interval_minutes
How long before the soft boundary nudge fires.
- Default: 60
- Range: 30-120
- The nudge will fire within ±5 minutes of this value, depending on natural breakpoint detection.

### min_break_duration_minutes
Minimum gap between prompts that counts as a "real" break.
- Default: 5
- Range: 3-15
- Gaps shorter than this are not logged as breaks.

### session_timeout_minutes
How long an idle gap before the session is considered over and the end-of-session ritual fires on the next prompt.
- Default: `null` — when null, reads from `pomodoro_interval_minutes` at runtime (live reference, not a copy). This means changing `pomodoro_interval_minutes` also changes the session timeout unless `session_timeout_minutes` is explicitly set.
- Range: 10-120
- Set explicitly to decouple session timeout from the pomodoro rhythm (e.g. shorter pomodoros but longer idle tolerance).

### closing_window_seconds
How long (in seconds) a session may sit in the `closing` state — last goal closed, 2x2 wrap-up pending — before the wrap-up is considered abandoned.
- Default: 300. Also the runtime fallback when the key is absent or null (legacy configs predate it).
- Measured from the session's `end_time` (written by `close-goal`).
- Within the window, the hook treats prompts as part of the wrap-up conversation (`[TANK — WRAP-UP PENDING]`).
- On expiry, the hook's stale-closing sweep finalizes the session mechanically: `state: "closed"`, `end_reason: "wrap_up_abandoned"`, `checkin` left as-is (stays null when never collected), file moved to `sessions/closed/`. The skill announces the finalization (`[TANK — STALE CLOSING FINALIZED]`) and offers `reopen-session` as the undo.
- A `closing` session with an unparseable `end_time` is swept immediately — it could never become in-window.

### hard_boundary_sensitivity
Controls the signal score threshold for hard boundary triggers.
- `"low"`: score >= 8 required (fewer interventions, higher confidence)
- `"medium"`: score >= 6 required (default)
- `"high"`: score >= 4 required (more interventions, may have more false positives)

### intrusiveness
Controls how much the skill surfaces information unprompted.
- `"high"`: all nudges active, pomodoro includes a brief session summary, hard nudge shows full evidence. Default for new users.
- `"medium"`: pomodoro is minimal (just the time reminder), hard nudge shows evidence.
- `"low"`: pomodoro is silent (logged but not shown), only hard nudges are visible.

This should adapt over time based on user behaviour. If the user consistently takes breaks before the pomodoro fires, the skill can suggest reducing intrusiveness.

### work_hours
Used for boundary crossing detection. Sessions starting before `start` or ending after `end` are flagged in the daily data.
- Format: 24-hour time string
- Set to the user's typical working window, not a prescription.

### timezone
Used for timestamp normalisation and daily rollup boundaries.

### recovery_suggestions_enabled
Whether the hard boundary nudge includes personalised recovery suggestions.
- Default: true
- Future: the skill could learn which activities the user finds restorative and personalise this.

### onboarding_complete
Set to true after the first-run onboarding message is shown. Prevents it from showing again.

### prompt_tracking_mode
How prompt content is tracked for pattern detection (retries, narrowing curiosity, etc.).
- `"fingerprint"` (default, and the only implemented mode): stores a compact keyword object per prompt — lightweight, zero token cost.
- `"summary"` is accepted as a legacy value and behaves identically to `"fingerprint"`: `content_fingerprint` is still populated (retry detection depends on it) and nothing writes `content_summary`. Do not offer it as a choice.
