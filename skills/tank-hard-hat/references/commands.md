# Slash Command Implementations

`/tank-hard-hat <arg>` invokes the skill with an argument.
Dispatch on the arg below. Any unrecognised arg (including `--help`, `-h`,
or a typo) dispatches to `help`. The boxed outputs are contracts — print
them exactly.

## `/tank-hard-hat help`

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

## `/tank-hard-hat quiet`

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

## `/tank-hard-hat resume`

Sets `quiet_mode: false` on the current session. Undoes quiet mode.

Implementation: `state.py set-field <id> quiet_mode false`.

Confirm with:
```
───────────────────────────────────────
🦉 Nudges back on.
───────────────────────────────────────
```

## `/tank-hard-hat end`

Closes the calling instance's open goal only.

**Implementation:**
1. Identify the current instance's `instance_id` (the Claude Code `session_id`
   from the hook payload — available as the identifier of this conversation).
2. Find the active Tank session via `state.py last-session`.
3. Look up the open goal owned by this instance_id in `session["goals"]`.
4. Call `state.py close-goal <session_id> <goal_id>`.

**Dispatch on the result:**

If `session_ended: false` (other goals are still open): announce the remaining
open goals by name, then run the **Bulk-Close Procedure**
(`references/rituals.md`) to offer closing them all at once:

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
in the `closing` state (`close-goal` returns `"state": "closing"`). Run the
Session End Check-in (`references/rituals.md`) immediately with
`end_reason: "explicit_quit"`; the 2x2 reply via `end-session` finalizes the
session.
