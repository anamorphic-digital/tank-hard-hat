# Tank Hard Hat 🦺

**Hard hat for your brain.** A Claude Code skill that monitors stress and
recovery during AI-paired coding — it nudges breaks at natural seams and flags
diminishing returns before you feel them. Grounded in the Tank stress-recovery
framework.

AI-assisted coding is more productive, and more draining. The dangerous state
isn't frustration — it's *pleasurable overextension*: flow feels good while it
draws down your reserves. Tank Hard Hat watches for the boundary between
"being in the zone" and "wanting to stay in the zone", and speaks up before
form degrades.

## What it does

- **Break rhythm** — a soft nudge at ~60 minutes of continuous work, timed to
  natural seams (a passing test, a commit) rather than mid-thought.
- **Diminishing-returns detection** — behavioural signals (retry loops, scope
  creep, narrowing curiosity) scored over a rolling window, validated before
  ever surfacing. A wrong call loses your trust; the skill is built to miss an
  intervention rather than fire a false one.
- **Overwhelm response** — if you say you're maxed out, it stops
  problem-solving and points you at a real break. Immediately, no heuristics.
- **Session check-ins** — a two-question energy/affect check at session end,
  accumulating into daily and weekly retros you can reflect on.

Everything runs locally. **All data stays in `~/.tank/` on your machine —
there is no network code in this repository, and you can audit that claim in
a few minutes: it's three Python files, stdlib only.**

## Install

### Option A — plugin (recommended)

```
/plugin marketplace add anamorphic-digital/tank-hard-hat
/plugin install tank-hard-hat@tank-hard-hat
```

The prompt hook registers automatically. **If you previously installed
manually (Option B/C), remove your manual `UserPromptSubmit` hook entry from
`~/.claude/settings.json` first** — running both logs every prompt twice and
skews the retry detection. Then add this line to `~/.claude/CLAUDE.md` so the
skill is applied on every prompt:

```
Always load and apply the tank-hard-hat skill on every prompt.
```

### Option B — manual skill install

```bash
git clone https://github.com/anamorphic-digital/tank-hard-hat.git
ln -s "$(pwd)/tank-hard-hat/skills/tank-hard-hat" ~/.claude/skills/tank-hard-hat
```

Then register the hook in `~/.claude/settings.json` (merge, don't replace):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 ~/.claude/skills/tank-hard-hat/scripts/hook-prompt.py" } ] }
    ]
  }
}
```

and add the same `CLAUDE.md` line as Option A.

### Option C — skills CLI

```bash
npx skills add anamorphic-digital/tank-hard-hat
```

Then register the hook and `CLAUDE.md` line as in Option B (adjust the paths
to wherever the CLI installed the skill).

Requirements: Python 3.11+, stdlib only. No dependencies to install.

## First run

Send any prompt. The skill walks you through a two-step onboarding:

1. **Permissions.** For silent operation it needs two allowlist rules in
   `~/.claude/settings.json` (read `~/.tank/**`, run its state manager).
   It can add them for you — one approval prompt, then silence — or print
   them for you to paste.
2. **Tracking mode.** Fingerprint keywords (default, zero token cost) or
   semantic summaries. Fingerprint is fine for most people.

## Commands

| Command | What it does |
| --- | --- |
| `/tank-hard-hat help` | Show the command reference. |
| `/tank-hard-hat quiet` | Suppress nudges this session (overwhelm response still fires). |
| `/tank-hard-hat resume` | Re-enable nudges. |
| `/tank-hard-hat end` | Close this window's goal; run the end-of-session check-in. |

## Optional: pomodoro countdown in your statusline

`skills/tank-hard-hat/scripts/statusline.py` prints a segment like
`tank: 37m` (time until break nudge), empty when no session is active. Wire it
into your statusline script:

```bash
tank=$(python3 ~/.claude/skills/tank-hard-hat/scripts/statusline.py 2>/dev/null)
[ -n "$tank" ] && parts="$parts | $tank"
```

## Data and privacy

```
~/.tank/
  config.json     Skill configuration
  sessions/       One JSON file per session
  dailies/        Computed daily rollups
  retro/          Computed weekly retros
```

Plain JSON, human-readable, local-only. Delete `~/.tank/` at any time to
erase all of it.

## Uninstall

1. Remove the plugin (`/plugin uninstall tank-hard-hat`) or the symlink and
   hook entry if you installed manually.
2. Remove the line from `~/.claude/CLAUDE.md`.
3. Optionally `rm -rf ~/.tank/` and drop the two Tank rules from
   `permissions.allow` in `~/.claude/settings.json`.

## License

[Apache-2.0](LICENSE). © Anamorphic Digital.
