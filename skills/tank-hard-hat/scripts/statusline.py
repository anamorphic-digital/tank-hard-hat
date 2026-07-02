#!/usr/bin/env python3
"""
Tank — Statusline Pomodoro Countdown

Outputs a countdown segment for Claude Code's statusline.
Reads session state from ~/.tank/ — does not use stdin.

Usage in a statusline script:
    tank=$(python3 ~/.claude/skills/tank-hard-hat/scripts/statusline.py 2>/dev/null)
    [ -n "$tank" ] && parts="$parts | $tank"
"""

import json
import math
import sys
from datetime import datetime
from glob import glob
from pathlib import Path

# Import shared helpers from state.py to avoid duplication
sys.path.insert(0, str(Path(__file__).parent))
from state import get_session_timeout, derive_state

try:
    sys.stdin.close()
except Exception:
    pass

TANK_DIR = Path.home() / ".tank"
SESSIONS_DIR = TANK_DIR / "sessions"
CONFIG_PATH = TANK_DIR / "config.json"


def read_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def get_latest_session():
    files = sorted(glob(str(SESSIONS_DIR / "*.json")))
    if not files:
        return None
    return json.loads(Path(files[-1]).read_text())


def time_since_last_break(session, config):
    """Minutes since the last break (gap >= min_break_duration)."""
    min_break = config.get("min_break_duration_minutes", 5)
    prompts = session.get("prompts", [])
    now = datetime.now().astimezone()

    if not prompts:
        start = datetime.fromisoformat(session["start_time"])
        return (now - start).total_seconds() / 60

    last_break_time = datetime.fromisoformat(session["start_time"])

    for i in range(1, len(prompts)):
        prev_ts = datetime.fromisoformat(prompts[i - 1]["timestamp"])
        curr_ts = datetime.fromisoformat(prompts[i]["timestamp"])
        gap_minutes = (curr_ts - prev_ts).total_seconds() / 60
        if gap_minutes >= min_break:
            last_break_time = curr_ts

    return (now - last_break_time).total_seconds() / 60


def is_session_active(session, config):
    """True if the session is open and not idle-expired."""
    if derive_state(session) != "open":
        return False

    idle_threshold = get_session_timeout(config)
    now = datetime.now().astimezone()

    prompts = session.get("prompts", [])
    if prompts:
        last_ts = datetime.fromisoformat(prompts[-1]["timestamp"])
        gap_minutes = (now - last_ts).total_seconds() / 60
        return gap_minutes < idle_threshold
    else:
        start = datetime.fromisoformat(session["start_time"])
        gap_minutes = (now - start).total_seconds() / 60
        return gap_minutes < idle_threshold


def format_countdown(elapsed_minutes, interval):
    """Format the countdown string from elapsed minutes and interval."""
    remaining = interval - elapsed_minutes
    if remaining >= 0:
        return f"tank: {math.floor(remaining)}m"
    else:
        overtime = math.ceil(abs(remaining))
        if overtime == 0:
            return "tank: 0m (break time)"
        return f"tank: -{overtime}m (break time)"


def main():
    if not TANK_DIR.exists():
        return

    config = read_config()
    session = get_latest_session()
    if session is None:
        return

    if not is_session_active(session, config):
        return

    interval = config.get("pomodoro_interval_minutes", 60)
    elapsed = time_since_last_break(session, config)
    segment = format_countdown(elapsed, interval)
    print(segment, end="")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
