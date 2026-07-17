#!/usr/bin/env python3
"""
Tank — Session State Manager

Handles reading, writing, and computing session state from ~/.tank/
Used by the Claude Code skill to persist data between prompts.

Session file layout:
  ~/.tank/sessions/<id>.json          # open sessions (state: "open") and
                                      # closing sessions (state: "closing",
                                      # end_time set, wrap-up pending)
  ~/.tank/sessions/closed/<id>.json   # finalized sessions (state: "closed")

When end-session finalizes a session it atomically moves the file into closed/.
close-goal on the last open goal parks the session as state: "closing" at the
top level; end-session later finalizes and moves it.
Read paths (get-session, last-session, compute-daily, etc.) look in both
locations. Lazy migration via migrate-closed moves any legacy top-level closed
files on first scan.

Usage:
  python state.py init                          # Create ~/.tank/ structure + default config
  python state.py start-session                 # Create new session file, return session_id
  python state.py log-prompt <session_id> <json> [instance_id] # Append prompt event to per-instance event log (instance_id = Claude Code session_id)
  python state.py read-session <session_id>     # Read hydrated session (metadata + merged events)
  python state.py get-session <session_id>      # Read current session state (open or closed, hydrated)
  python state.py get-session-summary <session_id> # Computed aggregates for current session
  python state.py end-session <session_id> <checkin_json> [end_time_iso] # Close session with 2x2 data (end_time_iso optional, defaults to now; pass explicit ISO timestamp to close stale sessions)
  python state.py compute-daily <date>          # Generate daily rollup from sessions (open + closed)
  python state.py last-session                  # Get the most recent session ID and its state
  python state.py config                        # Read current config
  python state.py config-set <key> <value>      # Update a config value
  python state.py set-field <session_id> <field> <json_value> # Set a top-level session field
  python state.py setup-permissions             # Merge Tank permission rules into ~/.claude/settings.json (idempotent)
  python state.py migrate-closed                # Move legacy closed sessions from top-level to closed/ (idempotent)
  python state.py add-goal <session_id> <instance_id> <text>  # Add a goal entry, returns goal_id
  python state.py close-goal <session_id> <goal_id>           # Close a goal; if last open goal, parks session as "closing"
  python state.py reopen-session <session_id>                 # Reopen a closed/closing session (undo stale-close finalization)
"""

import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from glob import glob

TANK_DIR = Path.home() / ".tank"
SESSIONS_DIR = TANK_DIR / "sessions"
CLOSED_DIR = SESSIONS_DIR / "closed"
DAILIES_DIR = TANK_DIR / "dailies"
EVENTS_DIR = TANK_DIR / "events"
CONFIG_PATH = TANK_DIR / "config.json"

# Instance identity is sourced from the Claude Code hook payload's `session_id`
# field and passed explicitly to log_prompt. There is no module-level default —
# callers must supply instance_id to avoid silently mixing events from different
# Claude Code conversations into the same log file.

DEFAULT_CONFIG = {
    "version": 1,
    "pomodoro_interval_minutes": 60,
    "min_break_duration_minutes": 5,
    "session_timeout_minutes": None,
    "closing_window_seconds": 300,
    "hard_boundary_sensitivity": "medium",
    "intrusiveness": "high",
    "work_hours": {"start": "09:00", "end": "18:00"},
    "timezone": "Australia/Sydney",
    "recovery_suggestions_enabled": True,
    "onboarding_complete": False,
}


def get_session_timeout(config):
    """Return the session timeout in minutes.

    If session_timeout_minutes is explicitly set (not None), use that value.
    Otherwise fall back to pomodoro_interval_minutes (live reference, not a copy).
    """
    timeout = config.get("session_timeout_minutes")
    if timeout is not None:
        return timeout
    return config.get("pomodoro_interval_minutes", 60)


def get_closing_window(config):
    """Return how long (seconds) a session may stay in 'closing' before being abandoned.

    Falls back to 300 when the key is absent or None — legacy configs predate it.
    """
    value = config.get("closing_window_seconds")
    if value is not None:
        return value
    return 300


_VALID_STATES = {"open", "closing", "closed"}


def derive_state(session):
    """Return the session's lifecycle state as a string.

    If the session already has a ``state`` key whose value is one of the
    recognised states (``"open"``, ``"closing"``, ``"closed"``), return it.
    Any other value — including ``None`` or arbitrary junk — is treated as
    absent and derived from ``end_time`` instead.

    For legacy files that predate the field: derive from ``end_time``.
      - ``end_time`` is null  → ``"open"``
      - ``end_time`` is set   → ``"closed"``
    """
    stored = session.get("state")
    if stored in _VALID_STATES:
        return stored
    return "open" if session.get("end_time") is None else "closed"


def init():
    """Create ~/.tank/ directory structure and default config."""
    for d in [TANK_DIR, SESSIONS_DIR, CLOSED_DIR, DAILIES_DIR, EVENTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print(json.dumps({"status": "created", "config": DEFAULT_CONFIG}))
    else:
        config = json.loads(CONFIG_PATH.read_text())
        print(json.dumps({"status": "exists", "config": config}))


def start_session():
    """Create a new session file and return the session ID.

    Uses os.open(path, O_CREAT | O_EXCL | O_WRONLY) so that simultaneous
    creates from two instances pick a single winner deterministically — whichever
    process wins the O_EXCL race gets the ID; the loser increments the sequence
    and retries. This prevents two concurrent instances from writing the same
    session file.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    CLOSED_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    while True:
        existing = sorted(
            glob(str(SESSIONS_DIR / f"{today}_*.json"))
            + glob(str(CLOSED_DIR / f"{today}_*.json"))
        )
        seq = len(existing) + 1
        session_id = f"{today}_{seq:03d}"

        session = {
            "session_id": session_id,
            "state": "open",
            "start_time": datetime.now().astimezone().isoformat(),
            "end_time": None,
            "end_reason": None,
            "duration_minutes": 0,
            "start_checkin": None,
            "goal": None,
            "goals": [],
            "quiet_mode": False,
            "prompts": [],
            "aggregates": {},
            "interventions": {"pomodoro_nudges": [], "hard_boundary_nudges": []},
            "checkin": None,
            "meta_scores": [],
        }

        path = SESSIONS_DIR / f"{session_id}.json"
        content = json.dumps(session, indent=2).encode()
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, content)
            finally:
                os.close(fd)
        except FileExistsError:
            # Another process beat us to this sequence number — rescan and retry
            continue
        break

    print(json.dumps({"session_id": session_id, "path": str(path)}))


def _find_session_path(session_id):
    """Return the Path for a session file, searching top-level then closed/.

    Returns None if not found in either location.
    """
    open_path = SESSIONS_DIR / f"{session_id}.json"
    if open_path.exists():
        return open_path
    closed_path = CLOSED_DIR / f"{session_id}.json"
    if closed_path.exists():
        return closed_path
    return None


def migrate_closed_sessions():
    """Move legacy/stray top-level session files into closed/.

    Lazy migration: runs on demand (called by scan operations). Idempotent —
    files already in closed/ are untouched because this only iterates over the
    top-level directory.

    Rule: move only when derive_state(session) == "closed".
    This covers state: "closed" explicitly, and legacy files (no state key) with
    end_time set (which derive to "closed"). It never touches state: "closing"
    files (derive_state returns "closing", not "closed") — those must stay at
    the top level until end-session finalises them.
    """
    CLOSED_DIR.mkdir(parents=True, exist_ok=True)
    for p in list(SESSIONS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if derive_state(data) == "closed":
            dest = CLOSED_DIR / p.name
            os.rename(p, dest)


def log_prompt(session_id, prompt_json, instance_id=None):
    """Append a prompt event to the per-instance event log (append-only, one JSON per line).

    Events are written to ~/.tank/events/<session_id>/<instance_id>.jsonl.
    The session metadata file is located via _find_session_path (so it works
    whether the session is open or closed) and is updated only to refresh
    duration_minutes — events are no longer stored there.

    instance_id should always be passed explicitly (it is the Claude Code
    session_id from the hook payload). If omitted, falls back to "unknown"
    as a sentinel — this is intentionally distinct from "default" so that
    callers that forget to pass it get a visible, diagnosable file name rather
    than silently mixing events into a legacy bucket.
    """
    if instance_id is None:
        instance_id = "unknown"

    # Parse event and write one JSON line to the per-instance log.
    # Seed envelope fields the caller omitted — readers sort and compute
    # gaps from the timestamp, so every stored event must carry one.
    event = json.loads(prompt_json)
    if not event.get("timestamp"):
        event["timestamp"] = datetime.now().astimezone().isoformat()
    if not event.get("type"):
        event["type"] = "prompt"
    log_dir = EVENTS_DIR / session_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{instance_id}.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, separators=(",", ":")) + "\n")

    # Update duration in session metadata (lightweight — no read-modify-write of events)
    # Use _find_session_path so this works for both open and closed sessions.
    session_path = _find_session_path(session_id)
    if session_path is None:
        print(json.dumps({"error": f"Session {session_id} not found"}))
        sys.exit(1)
    session = json.loads(session_path.read_text())
    start = datetime.fromisoformat(session["start_time"])
    now = datetime.now().astimezone()
    session["duration_minutes"] = round((now - start).total_seconds() / 60, 1)
    session_path.write_text(json.dumps(session, indent=2))

    # Count events across all instance logs for the response
    all_logs = list(log_dir.glob("*.jsonl"))
    total = sum(
        len(f.read_text().strip().splitlines()) for f in all_logs if f.read_text().strip()
    )
    print(json.dumps({"logged": True, "prompt_count": total}))


def log_signal(session_id, instance_id, signal_json):
    """Append a detected Layer-2 signal to the per-instance signal log.

    Signals are written to
    ~/.tank/events/<session_id>/signals/<instance_id>.jsonl — a `signals/`
    subdirectory, deliberately *not* alongside the prompt logs, so the
    non-recursive `*.jsonl` glob in read_session never merges them into the
    prompt stream. The signal log is the deterministic observable the eval
    harness reads back: which signals the model detected, when, at what weight.

    The record carries whatever the caller provides (name, weight, confidence,
    evidence); the timestamp is seeded if omitted so the 20-minute scoring
    window can be computed from it.
    """
    if instance_id is None:
        instance_id = "unknown"

    record = json.loads(signal_json)
    if not record.get("timestamp"):
        record["timestamp"] = datetime.now().astimezone().isoformat()

    log_dir = EVENTS_DIR / session_id / "signals"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{instance_id}.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    all_logs = list(log_dir.glob("*.jsonl"))
    total = sum(
        len(f.read_text().strip().splitlines()) for f in all_logs if f.read_text().strip()
    )
    print(json.dumps({"logged": True, "signal_count": total}))


def _read_signals(session_id):
    """Merge all persisted detected-signal records for a session, sorted by timestamp.

    Reads ~/.tank/events/<session_id>/signals/*.jsonl across every instance.
    Returns [] when no signals have been recorded. Malformed lines are skipped
    (crash-resilience, mirroring read_session)."""
    sig_dir = EVENTS_DIR / session_id / "signals"
    if not sig_dir.exists():
        return []
    signals = []
    for log_path in sorted(sig_dir.glob("*.jsonl")):
        text = log_path.read_text().strip()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                signals.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    signals = [s for s in signals if s.get("timestamp")]
    signals.sort(key=lambda s: s["timestamp"])
    return signals


def read_session(session_id):
    """Return a hydrated session dict: metadata from sessions/<id>.json plus events.

    Locates the metadata file via _find_session_path, which searches both the
    top-level sessions/ directory and sessions/closed/ — so this works for both
    open and closed sessions.

    Event sourcing priority:
    1. If ~/.tank/events/<session_id>/ exists and contains .jsonl files, merge
       all events from those files and sort by timestamp. The embedded
       session["prompts"] array is ignored in this case.
    2. Otherwise fall back to the session["prompts"] array (old data format).
       This keeps previously-recorded sessions readable without migration.

    Returns None if the session metadata file does not exist in either location.
    """
    session_path = _find_session_path(session_id)
    if session_path is None:
        return None

    session = json.loads(session_path.read_text())

    # Ensure every session returned by a read path has a state key,
    # even if the file predates the field (legacy derivation).
    session["state"] = derive_state(session)

    log_dir = EVENTS_DIR / session_id
    jsonl_files = sorted(log_dir.glob("*.jsonl")) if log_dir.exists() else []

    if jsonl_files:
        # New format: events live in per-instance JSONL logs
        events = []
        for log_path in jsonl_files:
            text = log_path.read_text().strip()
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # Skip malformed lines — crash-resilience
        # Drop legacy events written without a timestamp (pre-seeding
        # log_prompt) — readers sort and compute gaps from it.
        events = [e for e in events if e.get("timestamp")]
        events.sort(key=lambda e: e["timestamp"])
        session["prompts"] = events
    # else: fall back to session["prompts"] array as-is (old format, no migration needed)

    return session


def get_session(session_id):
    """Read current session state (hydrated: metadata + merged events; open or closed)."""
    session = read_session(session_id)
    if session is not None:
        print(json.dumps(session, indent=2))
    else:
        print(json.dumps({"error": f"Session {session_id} not found"}))


def compute_aggregates(prompts, duration_minutes, fired_signals):
    """Pure aggregates computation over a session's merged prompt events.

    Shared by get-session-summary (on-demand, open sessions) and by both
    finalization edges into `closed` (end-session below; the stale-closing
    sweep in hook-prompt.py imports this). The snapshot persisted at close is
    all that dailies can read once the event store is deleted —
    fingerprints are working memory for the open session and do not outlive
    it.
    """
    task_ids = set()
    retry_count = 0
    max_stretch = 0
    current_stretch_start = None
    breakpoints = 0

    for i, p in enumerate(prompts):
        if p.get("task_id"):
            task_ids.add(p["task_id"])
        if p.get("classification") == "retry":
            retry_count += 1

        gap = p.get("gap_since_last_seconds")
        if gap is not None and gap > 300:  # 5 min break
            if current_stretch_start is not None:
                stretch = (datetime.fromisoformat(prompts[i-1]["timestamp"]) -
                          datetime.fromisoformat(prompts[current_stretch_start]["timestamp"]))
                max_stretch = max(max_stretch, stretch.total_seconds() / 60)
            current_stretch_start = i
            breakpoints += 1
        elif current_stretch_start is None:
            current_stretch_start = i

    # Final stretch
    if current_stretch_start is not None and len(prompts) > current_stretch_start:
        stretch = (datetime.fromisoformat(prompts[-1]["timestamp"]) -
                  datetime.fromisoformat(prompts[current_stretch_start]["timestamp"]))
        max_stretch = max(max_stretch, stretch.total_seconds() / 60)

    # Specificity trend — only use prompts that have real scores (not defaults)
    scores = [p["specificity_score"] for p in prompts if p.get("specificity_score") is not None]
    if len(scores) >= 6:
        first_half = sum(scores[:len(scores)//2]) / (len(scores)//2)
        second_half = sum(scores[len(scores)//2:]) / (len(scores) - len(scores)//2)
        if second_half < first_half - 0.1:
            trend = "declining"
        elif second_half > first_half + 0.1:
            trend = "improving"
        else:
            trend = "stable"
    else:
        trend = "insufficient_data"

    return {
        "prompt_count": len(prompts),
        "active_workstream_count": len(task_ids),
        # task_ids persist by name (topic slugs), not just count, so daily
        # rollups can still dedupe workstreams across sessions after the
        # per-prompt events are deleted at close.
        "task_ids": sorted(task_ids),
        "retry_loop_count": retry_count,
        "longest_unbroken_stretch_minutes": round(max_stretch, 1),
        "natural_breakpoints_detected": breakpoints,
        "specificity_trend": trend,
        "duration_minutes": duration_minutes,
        # Detected Layer-2 signals (read-only surface). The model records these
        # via log-signal as it detects them; the scoring/threshold decision is
        # left to the model in Option A. The eval oracle grades against this.
        "fired_signals": fired_signals,
    }


def get_session_summary(session_id):
    """Compute aggregates for the current session (uses read_session for merged events)."""
    session = read_session(session_id)
    if session is None:
        print(json.dumps({"error": f"Session {session_id} not found"}))
        sys.exit(1)
    prompts = session["prompts"]

    if not prompts:
        print(json.dumps({"summary": "no prompts yet"}))
        return

    summary = compute_aggregates(
        prompts, session["duration_minutes"], _read_signals(session_id)
    )

    # Write aggregates back to session metadata file via _find_session_path
    # (Don't write the full hydrated session — events stay in event logs)
    raw_path = _find_session_path(session_id)
    if raw_path is not None:
        raw_session = json.loads(raw_path.read_text())
        raw_session["aggregates"] = summary
        raw_path.write_text(json.dumps(raw_session, indent=2))
    print(json.dumps(summary))


def end_session(session_id, checkin_json, end_time_iso=None):
    """Close a session with the 2x2 check-in data.

    Works on open, closing, and already-closed sessions.

    Transitions:
      open     → closed: sets end_time (was null), computes duration, moves to closed/
      closing  → closed: preserves end_time (set by close-goal), leaves duration alone,
                         moves to closed/
      closed   → closed: records checkin/end_reason only; end_time and duration untouched
                         (unless explicit end_time_iso is supplied — backdating stale closes)

    In all cases:
      - state is set to "closed"
      - checkin and end_reason are always updated
      - explicit end_time_iso overrides end_time and triggers duration recompute
    """
    path = _find_session_path(session_id)
    if path is None:
        print(json.dumps({"error": f"Session {session_id} not found"}))
        sys.exit(1)
    session = json.loads(path.read_text())

    checkin = json.loads(checkin_json)
    session["checkin"] = checkin
    session["end_reason"] = checkin.get(
        "collection_method", session.get("end_reason") or "explicit_quit"
    )

    # Always finalize the state regardless of the incoming value
    session["state"] = "closed"

    already_closed = path.parent == CLOSED_DIR

    # Decide whether to (re)write end_time:
    #   - explicit end_time_iso always wins (backdating support)
    #   - otherwise only set end_time when it is currently null (open path)
    #   - closing sessions already have end_time set by close-goal — preserve it
    should_write_end_time = end_time_iso is not None or session.get("end_time") is None

    if should_write_end_time:
        if end_time_iso is not None:
            datetime.fromisoformat(end_time_iso)  # validate, but preserve original offset
            session["end_time"] = end_time_iso
        else:
            session["end_time"] = datetime.now().astimezone().isoformat()

        start = datetime.fromisoformat(session["start_time"])
        end = datetime.fromisoformat(session["end_time"])
        session["duration_minutes"] = round((end - start).total_seconds() / 60, 1)

    # Snapshot aggregates before the event store goes away. The event log is
    # working memory for the open session (retry similarity, Layer-2 scoring);
    # once closed it is never read again except via this snapshot. If the
    # events are already gone (re-close of a legacy/finalized session), keep
    # whatever snapshot was taken at the original close.
    hydrated = read_session(session_id)
    if hydrated and hydrated.get("prompts"):
        session["aggregates"] = compute_aggregates(
            hydrated["prompts"], session["duration_minutes"], _read_signals(session_id)
        )

    path.write_text(json.dumps(session, indent=2))

    if not already_closed:
        CLOSED_DIR.mkdir(parents=True, exist_ok=True)
        closed_path = CLOSED_DIR / f"{session_id}.json"
        os.rename(path, closed_path)

    # Terminal state reached: delete the per-prompt event store (fingerprints,
    # cwd, signal evidence). Failure is tolerated — the hook's GC pass
    # converges any leftover dir on a later prompt.
    shutil.rmtree(EVENTS_DIR / session_id, ignore_errors=True)

    print(json.dumps({"ended": True, "session_id": session_id, "duration": session["duration_minutes"]}))


def last_session():
    """Get the most recent session (searches both top-level and closed/, hydrated)."""
    all_paths = [
        Path(f)
        for f in glob(str(SESSIONS_DIR / "*.json")) + glob(str(CLOSED_DIR / "*.json"))
    ]
    if not all_paths:
        print(json.dumps({"error": "No sessions found"}))
        return
    # Sort by filename (YYYY-MM-DD_NNN.json) so sequence ordering is correct
    # regardless of which directory the file lives in.
    path = sorted(all_paths, key=lambda p: p.name)[-1]
    session_id = path.stem
    session = read_session(session_id)
    if session is not None:
        print(json.dumps(session, indent=2))
    else:
        print(json.dumps({"error": f"Session {session_id} not found"}))


def read_config():
    """Read current config."""
    if CONFIG_PATH.exists():
        print(CONFIG_PATH.read_text())
    else:
        print(json.dumps({"error": "Config not found. Run 'init' first."}))


def config_set(key, value):
    """Update a config value."""
    config = json.loads(CONFIG_PATH.read_text())

    # Type coercion
    if value.lower() in ("true", "false"):
        value = value.lower() == "true"
    elif value.isdigit():
        value = int(value)
    elif "." in value:
        try:
            value = float(value)
        except ValueError:
            pass

    # Handle nested keys (e.g. work_hours.start)
    keys = key.split(".")
    obj = config
    for k in keys[:-1]:
        obj = obj[k]
    obj[keys[-1]] = value

    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    print(json.dumps({"updated": key, "value": value}))


# Layer 3 wrap-up fields (checkin, relative_mood) are allowed so the 2x2
# can be persisted on a session that close-goal has already moved to closed/.
# The canonical writer is `end-session` (see SKILL.md Layer 3), but a set-field
# fallback must degrade gracefully rather than hard-fail — see issue #20.
ALLOWED_SET_FIELDS = {"goal", "start_checkin", "quiet_mode", "checkin", "relative_mood"}


def set_field(session_id, field, json_value):
    """Set a top-level field in the session file to a JSON-parsed value."""
    if field not in ALLOWED_SET_FIELDS:
        print(json.dumps({"error": f"Field '{field}' not in allowed fields: {sorted(ALLOWED_SET_FIELDS)}"}))
        sys.exit(1)
    path = _find_session_path(session_id)
    if path is None:
        print(json.dumps({"error": f"Session {session_id} not found"}))
        sys.exit(1)
    session = json.loads(path.read_text())
    session[field] = json.loads(json_value)
    path.write_text(json.dumps(session, indent=2))
    print(json.dumps({"updated": field, "session_id": session_id}))


def _generate_goal_id():
    """Generate a short unique goal ID using timestamp + random hex suffix."""
    import random
    ts = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    suffix = format(random.randint(0, 0xFFFF), "04x")
    return f"goal-{ts}-{suffix}"


def add_goal(session_id, instance_id, text):
    """Add a goal entry to the session's goals array.

    Schema: {id, instance_id, text, started_at, ended_at: null}
    Returns JSON with goal_id.
    """
    path = _find_session_path(session_id)
    if path is None:
        print(json.dumps({"error": f"Session {session_id} not found"}))
        sys.exit(1)

    session = json.loads(path.read_text())
    goal_id = _generate_goal_id()
    goal = {
        "id": goal_id,
        "instance_id": instance_id,
        "text": text,
        "started_at": datetime.now().astimezone().isoformat(),
        "ended_at": None,
    }
    session.setdefault("goals", []).append(goal)
    path.write_text(json.dumps(session, indent=2))
    print(json.dumps({"goal_id": goal_id, "session_id": session_id}))


def close_goal(session_id, goal_id):
    """Close a goal by setting ended_at.

    If this was the last open goal in the session, parks the session in the
    "closing" state: writes end_time, end_reason, duration_minutes, and
    state: "closing" — but leaves the file at sessions/<id>.json (NOT moved
    to closed/). The wrap-up ritual (Layer 3) will later call end-session to
    finalize the session and move the file.

    If other goals remain open, writes state: "open" explicitly.

    Both branches assign state directly; derive_state is not used here.

    Returns JSON with closed, goal_id, session_ended (bool), state (when
    session_ended is True), and open_goals (list of still-open goals) when
    session stays alive.
    """
    path = _find_session_path(session_id)
    if path is None:
        print(json.dumps({"error": f"Session {session_id} not found"}))
        sys.exit(1)

    session = json.loads(path.read_text())

    goals = session.get("goals", [])

    # Find the goal
    goal = next((g for g in goals if g["id"] == goal_id), None)
    if goal is None:
        print(json.dumps({"error": f"Goal {goal_id} not found in session {session_id}"}))
        sys.exit(1)

    # Set ended_at
    goal["ended_at"] = datetime.now().astimezone().isoformat()
    session["goals"] = goals

    # Check remaining open goals
    open_goals = [g for g in goals if g.get("ended_at") is None]

    if open_goals:
        # Other goals still open — session continues in "open" state
        session["state"] = "open"
        path.write_text(json.dumps(session, indent=2))
        open_goal_summaries = [
            {"id": g["id"], "instance_id": g["instance_id"], "text": g["text"]}
            for g in open_goals
        ]
        print(json.dumps({
            "closed": True,
            "goal_id": goal_id,
            "session_ended": False,
            "open_goals": open_goal_summaries,
        }))
    else:
        # Last open goal closed — park in "closing" state, file stays in sessions/
        now = datetime.now().astimezone()
        session["end_time"] = now.isoformat()
        session["end_reason"] = "explicit_quit"
        session["state"] = "closing"

        start = datetime.fromisoformat(session["start_time"])
        session["duration_minutes"] = round((now - start).total_seconds() / 60, 1)

        path.write_text(json.dumps(session, indent=2))

        print(json.dumps({
            "closed": True,
            "goal_id": goal_id,
            "session_ended": True,
            "session_id": session_id,
            "state": "closing",
        }))


def reopen_session(session_id):
    """Undo path for stale-closing finalization.

    When the hook auto-finalizes an abandoned wrap-up and the user objects,
    the skill calls this to restore the session to the open state.

    Behaviour:
    - Locate the session via _find_session_path (searches sessions/ and closed/)
    - Set state: "open", end_time: null, end_reason: null
    - Clear reopen_offer_pending (sweep bookkeeping) so the one-shot reopen
      window can't re-trigger; keep swept_at as historical data
    - Leave duration_minutes, checkin, and goals untouched
    - If the file is in closed/, move it back to sessions/ top level
    - Print JSON: {"reopened": true, "session_id": ..., "state": "open"}
    - Unknown session id: print {"error": ...} and exit 1
    """
    path = _find_session_path(session_id)
    if path is None:
        print(json.dumps({"error": f"Session {session_id} not found"}))
        sys.exit(1)

    session = json.loads(path.read_text())
    session["state"] = "open"
    session["end_time"] = None
    session["end_reason"] = None

    # Sweep bookkeeping: a manual late reopen must ensure the one-shot
    # reopen-window offer can never re-trigger (in the normal flow the hook
    # already cleared it on the offer turn). swept_at stays as history.
    if "reopen_offer_pending" in session:
        session["reopen_offer_pending"] = False

    # If the file is in closed/, move it back to the top-level sessions/ dir
    if path.parent == CLOSED_DIR:
        top_level_path = SESSIONS_DIR / f"{session_id}.json"
        path.write_text(json.dumps(session, indent=2))
        os.rename(path, top_level_path)
    else:
        path.write_text(json.dumps(session, indent=2))

    print(json.dumps({"reopened": True, "session_id": session_id, "state": "open"}))


def setup_permissions(home=None):
    """Merge the permission rules Tank needs for silent operation into
    ~/.claude/settings.json.

    Idempotent: running twice adds each rule at most once. Preserves any
    unrelated settings and any existing `permissions.allow` entries.

    The ~/.tank reads and `state.py` invocations otherwise prompt on every
    hook firing, which is the UX we're trying to eliminate — see SKILL.md
    "Silent file operations".
    """
    home = Path(home) if home is not None else Path.home()
    settings_path = home / ".claude" / "settings.json"
    # The rule must match however this install invokes state.py — symlinked
    # skill dir, plugin cache, or repo checkout — so derive it from this
    # file's own invocation path (abspath, not resolve: a symlinked install
    # should be allowlisted under the symlink path it is invoked through).
    installed_state_py = Path(os.path.abspath(__file__))

    required = [
        f"Bash(python3 {installed_state_py} *)",
        f"Read({home}/.tank/**)",
    ]

    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    allow = settings.setdefault("permissions", {}).setdefault("allow", [])

    added = []
    already_present = []
    for rule in required:
        if rule in allow:
            already_present.append(rule)
        else:
            allow.append(rule)
            added.append(rule)

    settings_path.write_text(json.dumps(settings, indent=2))
    print(json.dumps({
        "settings_path": str(settings_path),
        "added": added,
        "already_present": already_present,
    }))


def compute_daily(date_str):
    """Generate daily rollup from session files (searches both top-level and closed/,
    uses read_session for merged events).
    """
    files = sorted(
        glob(str(SESSIONS_DIR / f"{date_str}_*.json"))
        + glob(str(CLOSED_DIR / f"{date_str}_*.json"))
    )
    if not files:
        print(json.dumps({"error": f"No sessions found for {date_str}"}))
        return

    sessions = [read_session(Path(f).stem) for f in files]
    sessions = [s for s in sessions if s is not None]

    total_minutes = sum(s.get("duration_minutes", 0) for s in sessions)
    all_nudges = []
    all_hard = []
    all_checkins = []
    all_signals = set()
    max_stretch = 0
    workstreams = set()

    for s in sessions:
        nudges = s.get("interventions", {}).get("pomodoro_nudges", [])
        hard = s.get("interventions", {}).get("hard_boundary_nudges", [])
        all_nudges.extend(nudges)
        all_hard.extend(hard)
        for h in hard:
            all_signals.update(h.get("signals", []))
        if s.get("checkin"):
            all_checkins.append({
                "quadrant": s["checkin"].get("quadrant"),
                "session": s["session_id"],
            })
        stretch = s.get("aggregates", {}).get("longest_unbroken_stretch_minutes", 0)
        max_stretch = max(max_stretch, stretch)
        for p in s.get("prompts", []):
            if p.get("task_id"):
                workstreams.add(p["task_id"])
        # Closed sessions have no event store — their task_ids live in the
        # aggregates snapshot taken at finalization.
        workstreams.update(s.get("aggregates", {}).get("task_ids", []))

    breaks_taken = sum(1 for n in all_nudges if n.get("response") == "break_taken")
    compliance = breaks_taken / len(all_nudges) if all_nudges else None

    starts = [s["start_time"] for s in sessions if s.get("start_time")]
    ends = [s["end_time"] for s in sessions if s.get("end_time")]

    daily = {
        "date": date_str,
        "total_active_minutes": round(total_minutes, 1),
        "session_count": len(sessions),
        "avg_session_length_minutes": round(total_minutes / len(sessions), 1),
        "pomodoro_compliance_rate": round(compliance, 2) if compliance is not None else None,
        "hard_boundary_triggers": len(all_hard),
        "hard_boundary_signals": list(all_signals),
        "longest_unbroken_stretch_minutes": round(max_stretch, 1),
        "first_session_start": min(starts)[-14:-6] if starts else None,
        "last_session_end": max(ends)[-14:-6] if ends else None,
        "workstream_breadth": len(workstreams),
        "checkins": all_checkins,
    }

    DAILIES_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILIES_DIR / f"{date_str}.json"
    path.write_text(json.dumps(daily, indent=2))
    print(json.dumps(daily))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "init":
        init()
    elif cmd == "start-session":
        start_session()
    elif cmd == "log-prompt" and len(sys.argv) >= 4:
        # Optional 4th argument: instance_id (Claude Code session_id from hook payload)
        instance_id = sys.argv[4] if len(sys.argv) >= 5 else None
        log_prompt(sys.argv[2], sys.argv[3], instance_id=instance_id)
    elif cmd == "log-signal" and len(sys.argv) >= 5:
        log_signal(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "read-session" and len(sys.argv) >= 3:
        session = read_session(sys.argv[2])
        if session is not None:
            print(json.dumps(session, indent=2))
        else:
            print(json.dumps({"error": f"Session {sys.argv[2]} not found"}))
            sys.exit(1)
    elif cmd == "get-session" and len(sys.argv) >= 3:
        get_session(sys.argv[2])
    elif cmd == "get-session-summary" and len(sys.argv) >= 3:
        get_session_summary(sys.argv[2])
    elif cmd == "end-session" and len(sys.argv) >= 4:
        end_time_iso = sys.argv[4] if len(sys.argv) >= 5 else None
        end_session(sys.argv[2], sys.argv[3], end_time_iso)
    elif cmd == "last-session":
        last_session()
    elif cmd == "config":
        read_config()
    elif cmd == "config-set" and len(sys.argv) >= 4:
        config_set(sys.argv[2], sys.argv[3])
    elif cmd == "set-field" and len(sys.argv) >= 5:
        set_field(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "compute-daily" and len(sys.argv) >= 3:
        compute_daily(sys.argv[2])
    elif cmd == "setup-permissions":
        setup_permissions()
    elif cmd == "migrate-closed":
        migrate_closed_sessions()
    elif cmd == "add-goal" and len(sys.argv) >= 5:
        add_goal(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "close-goal" and len(sys.argv) >= 4:
        close_goal(sys.argv[2], sys.argv[3])
    elif cmd == "reopen-session" and len(sys.argv) >= 3:
        reopen_session(sys.argv[2])
    else:
        print(f"Unknown command or missing args: {cmd}")
        print(__doc__)
        sys.exit(1)
