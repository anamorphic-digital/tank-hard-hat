#!/usr/bin/env python3
"""
Tank — UserPromptSubmit Hook

Receives hook input on stdin from Claude Code, manages session state,
and outputs intervention context when thresholds are hit.

This script runs on EVERY user prompt via the UserPromptSubmit hook.
It must be fast and silent unless an intervention is needed.
"""

import json
import re
import shutil
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from glob import glob

# Import shared helpers from state.py to avoid duplication. Only PURE
# functions may be imported — path-dependent state.py code binds to state's
# own module globals, which test loaders cannot patch through this module.
sys.path.insert(0, str(Path(__file__).parent))
from state import (
    get_session_timeout,
    get_closing_window,
    derive_state,
    compute_aggregates,
)

TANK_DIR = Path.home() / ".tank"
SESSIONS_DIR = TANK_DIR / "sessions"
CLOSED_DIR = SESSIONS_DIR / "closed"
EVENTS_DIR = TANK_DIR / "events"
CONFIG_PATH = TANK_DIR / "config.json"

# Stop words for keyword extraction
STOP_WORDS = frozenset(
    "a an the is are was were be been being have has had do does did will would "
    "shall should may might can could of in to for on with at by from as into "
    "through during before after above below between out off over under again "
    "further then once here there when where why how all each every both few "
    "more most other some such no nor not only own same so than too very just "
    "don t s d ll ve re m it its he she they we you i my your his her their our "
    "this that these those am if or but and also about up what which who whom "
    "please help me let us get make sure think know want need try use".split()
)

# Common intent verbs
INTENT_VERBS = [
    "fix", "debug", "add", "create", "implement", "update", "change", "modify",
    "remove", "delete", "refactor", "rename", "move", "test", "write", "read",
    "explain", "show", "list", "find", "search", "check", "review", "optimize",
    "configure", "setup", "install", "deploy", "build", "run", "migrate",
]

# File path pattern
FILE_PATH_RE = re.compile(
    r'(?:^|[\s\'"`(])('
    r'(?:[a-zA-Z0-9_./-]+/[a-zA-Z0-9_./-]+)'  # path/to/file
    r'|(?:[a-zA-Z0-9_.-]+\.(?:py|js|ts|tsx|jsx|go|rs|rb|java|c|cpp|h|hpp|css|scss|html|json|yaml|yml|toml|md|sql|sh|bash|zsh))'  # file.ext
    r')'
)

# Line number pattern (e.g., :42, line 42, L42)
LINE_NUMBER_RE = re.compile(r'(?::(\d+)|[Ll]ine\s+(\d+)|[Ll](\d+))')

# Decision deferral phrases
DEFERRAL_PHRASES = [
    "for now", "good enough", "just make it work", "just get it working",
    "worry about later", "fix later", "skip the tests", "deal with later",
    "come back to", "we'll fix", "clean up later",
]

# Completion fixation phrases
FIXATION_PHRASES = [
    "one more thing", "also", "before we stop", "while we're at it",
    "quickly", "just one more", "and then", "real quick",
]


def extract_file_paths(text):
    """Extract file paths from prompt text."""
    matches = FILE_PATH_RE.findall(text)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for m in matches:
        m = m.strip("'\"`()")
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


_HEX_BLOB = re.compile(r'[0-9a-f]{20,}')


def _is_secret_shaped(token):
    """Machine-generated-looking tokens: long hex blobs, or long mixed
    letter+digit runs (credentials, API keys, hashes). Dropping them is
    loss-free for retry detection — a one-time random string carries no
    retry signal — and keeps pasted secrets out of the event log. This is a
    shape rule, not a secret detector: human-readable secrets are a
    documentation matter, not a detection one.
    """
    if _HEX_BLOB.fullmatch(token):
        return True
    return (
        len(token) >= 16
        and any(c.isdigit() for c in token)
        and any(c.isalpha() for c in token)
    )


def extract_keywords(text):
    """Extract 3-6 top content words from the prompt."""
    # Lowercase, split on non-alphanumeric
    words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text.lower())
    # Filter stop words, very short words, and secret-shaped tokens
    content_words = [
        w for w in words
        if w not in STOP_WORDS and len(w) > 2 and not _is_secret_shaped(w)
    ]
    # Count frequency
    freq = {}
    for w in content_words:
        freq[w] = freq.get(w, 0) + 1
    # Sort by frequency (desc), then by first appearance
    order = {}
    for i, w in enumerate(content_words):
        if w not in order:
            order[w] = i
    ranked = sorted(freq.keys(), key=lambda w: (-freq[w], order[w]))
    return ranked[:6]


def extract_intent_verb(text):
    """Extract the primary action verb from the prompt."""
    lower = text.lower()
    for verb in INTENT_VERBS:
        # Match verb at word boundary
        if re.search(r'\b' + verb + r'\b', lower):
            return verb
    return "unknown"


def extract_target(keywords, files, intent_verb):
    """Build a short target phrase from keywords and files."""
    parts = []
    if intent_verb != "unknown":
        parts.append(intent_verb)
    # Add up to 4 keywords that aren't the verb
    for kw in keywords:
        if kw != intent_verb and len(parts) < 5:
            parts.append(kw)
    return " ".join(parts) if parts else "unknown"


def compute_fingerprint(text):
    """Extract a compact keyword fingerprint from prompt text."""
    files = extract_file_paths(text)
    keywords = extract_keywords(text)
    intent_verb = extract_intent_verb(text)
    target = extract_target(keywords, files, intent_verb)
    return {
        "keywords": keywords,
        "files": files,
        "intent_verb": intent_verb,
        "target": target,
    }


def compute_specificity_score(text, files):
    """Heuristic specificity score from 0.0 to 1.0."""
    score = 0.0
    # File references: +0.2 each, max 0.4
    score += min(len(files) * 0.2, 0.4)
    # Line numbers: +0.1
    if LINE_NUMBER_RE.search(text):
        score += 0.1
    # Technical terms (rough heuristic: camelCase, snake_case, or ALL_CAPS identifiers)
    if re.search(r'[a-z][A-Z]|[a-zA-Z]_[a-zA-Z]|[A-Z]{2,}_[A-Z]', text):
        score += 0.1
    # Length > 50 tokens (approximate: words * 1.3)
    word_count = len(text.split())
    if word_count * 1.3 > 50:
        score += 0.1
    # Question marks
    if '?' in text:
        score += 0.1
    # Concrete nouns heuristic: presence of specific identifiers (dotted paths, parens)
    if re.search(r'[a-zA-Z]+\.[a-zA-Z]+\(|[a-zA-Z]+\(', text):
        score += 0.1
    return min(score, 1.0)


def compute_retry_similarity(fingerprint, recent_fingerprints):
    """Compare fingerprint against last N fingerprints. Returns max similarity 0.0-1.0."""
    if not recent_fingerprints:
        return 0.0

    max_sim = 0.0
    current_kw = set(fingerprint.get("keywords", []))
    current_verb = fingerprint.get("intent_verb", "")
    current_target = fingerprint.get("target", "")

    for prev in recent_fingerprints:
        prev_kw = set(prev.get("keywords", []))
        # Keyword Jaccard (60% weight)
        union = current_kw | prev_kw
        if union:
            jaccard = len(current_kw & prev_kw) / len(union)
        else:
            jaccard = 0.0
        # Intent verb exact match (20% weight)
        verb_match = 1.0 if current_verb == prev.get("intent_verb", "") else 0.0
        # Target exact match (20% weight)
        target_match = 1.0 if current_target == prev.get("target", "") else 0.0

        sim = jaccard * 0.6 + verb_match * 0.2 + target_match * 0.2
        max_sim = max(max_sim, sim)

    return round(max_sim, 3)


def classify_prompt(fingerprint, retry_similarity, recent_prompts):
    """Classify prompt as new_task, continuation, retry, or refinement."""
    if not recent_prompts:
        return "new_task"

    # Tool-loading / very short prompts
    kw_count = len(fingerprint.get("keywords", []))
    if kw_count == 0:
        return "continuation"

    if retry_similarity >= 0.6:
        return "retry"

    # Check if task_id changed from previous prompt
    prev = recent_prompts[-1]
    prev_fp = prev.get("content_fingerprint")
    if prev_fp:
        prev_files = set(prev_fp.get("files", []))
        curr_files = set(fingerprint.get("files", []))
        prev_kw = set(prev_fp.get("keywords", []))
        curr_kw = set(fingerprint.get("keywords", []))
        # If files and keywords are completely different, it's a new task
        file_overlap = bool(prev_files & curr_files) if (prev_files and curr_files) else False
        kw_overlap = len(prev_kw & curr_kw) / max(len(prev_kw | curr_kw), 1)
        if not file_overlap and kw_overlap < 0.2:
            return "new_task"

    if retry_similarity >= 0.3:
        return "refinement"

    return "continuation"


def infer_task_id(fingerprint):
    """Infer a task_id from files and keywords."""
    files = fingerprint.get("files", [])
    keywords = fingerprint.get("keywords", [])
    if files:
        # Use the first file's directory or name as task anchor
        parts = files[0].replace("\\", "/").split("/")
        # Take last two meaningful path segments
        meaningful = [p for p in parts if p and p != "."]
        return "-".join(meaningful[-2:]) if len(meaningful) >= 2 else meaningful[-1] if meaningful else None
    if keywords:
        return "-".join(keywords[:3])
    return None


def is_tool_loading_prompt(text):
    """Detect tool-loading or trivial system prompts."""
    return len(text.split()) < 5 and not '?' in text


def read_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            # Fail open to defaults: a partially-written config (crash,
            # disk-full) must not turn every subsequent prompt into a
            # traceback. The file is left for the user to repair or replace.
            pass
    return {
        "pomodoro_interval_minutes": 60,
        "min_break_duration_minutes": 5,
        "session_timeout_minutes": None,
        "prompt_tracking_mode": "fingerprint",
    }


def migrate_closed_sessions():
    """Move legacy/stray top-level session files into closed/.

    Lazy migration: runs on every hook scan. Idempotent — only iterates
    top-level sessions/, not closed/ (which contains already-migrated files).

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


def find_open_sessions():
    """Return all open session paths at the top level of sessions/.

    Runs lazy migration first so that pre-existing closed sessions (end_time
    set but still at top level) are moved to closed/ before the scan.
    Returns a list of Path objects — empty list means no open sessions.
    Closed sessions in sessions/closed/ are never returned.
    """
    migrate_closed_sessions()
    files = sorted(glob(str(SESSIONS_DIR / "*.json")))
    return [Path(f) for f in files]


def get_latest_session_path():
    """Return the path of the most recent OPEN session (compatibility shim).

    Wraps find_open_sessions() and returns the last path or None. Kept for
    backward compatibility with tests that reference this function name.
    """
    paths = find_open_sessions()
    return paths[-1] if paths else None


def _load_session_hydrated(path):
    """Load a session from its metadata file and merge events from the event log.

    Uses module-level EVENTS_DIR so tests can patch it correctly.

    Event sourcing priority (mirrors state.py read_session):
    1. If EVENTS_DIR/<session_id>/ exists with .jsonl files, use those (sorted by ts).
    2. Otherwise fall back to session["prompts"] array (old data format).
    """
    session = json.loads(path.read_text())
    session_id = session["session_id"]

    # Ensure every session returned by a read path has a state key,
    # even if the file predates the field (legacy derivation).
    session["state"] = derive_state(session)

    events = _read_session_events(session_id)
    if events is not None:
        session["prompts"] = events
    # else: fall back to session["prompts"] as-is (old format)

    return session


def _read_session_events(session_id):
    """Merged, timestamp-sorted prompt events for a session.

    Returns None when no event files exist (callers fall back to the embedded
    prompts array — old data format); otherwise the merged (possibly empty)
    list. Legacy events without a timestamp are dropped — readers sort and
    compute gaps from it.
    """
    log_dir = EVENTS_DIR / session_id
    jsonl_files = sorted(log_dir.glob("*.jsonl")) if log_dir.exists() else []
    if not jsonl_files:
        return None
    events = []
    for log_path in jsonl_files:
        text = log_path.read_text().strip()
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # crash-resilient — skip malformed lines
    events = [e for e in events if e.get("timestamp")]
    events.sort(key=lambda e: e["timestamp"])
    return events


def _read_session_signals(session_id):
    """Layer-2 signals for a session (events/<sid>/signals/*.jsonl, merged)."""
    signals = []
    signals_dir = EVENTS_DIR / session_id / "signals"
    if not signals_dir.exists():
        return signals
    for log_path in sorted(signals_dir.glob("*.jsonl")):
        for line in log_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    signals.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # crash-resilient — skip malformed lines
    return signals


def _snapshot_aggregates(session_id, data):
    """Fill data["aggregates"] from the event store if no snapshot exists yet.

    Returns (data, changed). Mirrors what end-session does on the explicit
    close: the snapshot is all that dailies/retros can read once the event
    store is deleted. An existing snapshot is never overwritten here — the
    mechanical edges (sweep, GC) lack the checkin-time context that
    end-session has, so the freshest deliberate snapshot wins.
    """
    if data.get("aggregates"):
        return data, False
    events = _read_session_events(session_id) or []
    if not events:
        return data, False
    data["aggregates"] = compute_aggregates(
        events, data.get("duration_minutes"), _read_session_signals(session_id)
    )
    return data, True


def gc_closed_event_dirs():
    """Delete event stores whose session reached the terminal `closed` state.

    Convergence pass: covers the crash window (session finalized but the
    rmtree never ran) and legacy accumulation from before deletion existed.
    Fingerprints are working memory for an open session — nothing
    prompt-derived outlives the session except the aggregates snapshot.
    Open and `closing` sessions keep their events (`closing` is escapable via
    reopen-session); a dir with no session file anywhere is left alone, since
    a concurrent instance may be mid-creation.
    """
    if not EVENTS_DIR.exists():
        return
    for d in sorted(EVENTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        sid = d.name
        path = SESSIONS_DIR / f"{sid}.json"
        if not path.exists():
            path = CLOSED_DIR / f"{sid}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if derive_state(data) != "closed":
            continue
        data, changed = _snapshot_aggregates(sid, data)
        if changed:
            try:
                path.write_text(json.dumps(data, indent=2))
            except OSError:
                continue  # keep the events rather than lose the only copy
        shutil.rmtree(d, ignore_errors=True)


def create_session():
    """Create a new session file, returning (path, session).

    Uses os.open(path, O_CREAT | O_EXCL | O_WRONLY) so that simultaneous
    creates from two instances pick a single winner deterministically — whichever
    process wins the O_EXCL race gets the ID; the loser re-scans and retries.
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
            "prompts": [],
            "aggregates": {},
            "interventions": {"pomodoro_nudges": [], "hard_boundary_nudges": []},
            "checkin": None,
            "meta_scores": [],
            "start_checkin": None,
            "goal": None,
            "goals": [],
            "quiet_mode": False,
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

    return path, session


def _latest_event_timestamp(session):
    """Return the latest event timestamp from the session's prompts list.

    Returns a datetime (timezone-aware) or None if no timestamped prompts.
    """
    prompts = session.get("prompts", [])
    timestamps = [p.get("timestamp") for p in prompts if p.get("timestamp")]
    if not timestamps:
        return None
    return datetime.fromisoformat(max(timestamps))


def sweep_stale_closing_sessions(paths, config, now):
    """Finalize abandoned wrap-ups: every "closing" session whose end_time is
    beyond the closing window (or unparseable) is mechanically closed.

    Transition (distinct from end_session — no checkin is stamped):
      state → "closed", end_reason → "wrap_up_abandoned",
      checkin untouched (stays null if null), end_time/duration untouched,
      file moved to closed/.

    Returns (surviving_paths, swept_signals):
      surviving_paths — paths not swept (open, in-window closing, or unreadable)
      swept_signals — one {"type": "stale_closing_finalized", ...} per sweep

    Crash-proof: an unreadable/corrupt file is skipped silently (left in
    place for the candidate loop to deal with); a failed write/rename leaves
    a self-describing file that migration/sweep converges on a later scan.
    """
    closing_window = get_closing_window(config)
    survivors = []
    swept_signals = []
    for p in paths:
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            survivors.append(p)
            continue
        if derive_state(data) != "closing":
            survivors.append(p)
            continue
        end_time_raw = data.get("end_time")
        try:
            end_dt = datetime.fromisoformat(end_time_raw)
            stale = (now - end_dt).total_seconds() > closing_window
        except (ValueError, TypeError):
            # Garbage end_time: the clock can't be read, so this session could
            # never become in-window — sweep it rather than leave it immortal.
            stale = True
        if not stale:
            survivors.append(p)
            continue
        data["state"] = "closed"
        data["end_reason"] = "wrap_up_abandoned"
        # Reopen-window bookkeeping: the undo offer is announced on this turn,
        # but the user's objection arrives on the NEXT turn. These fields let
        # that turn recognise the situation and avoid creating a phantom
        # session (see _check_reopen_window).
        data["swept_at"] = now.isoformat()
        data["reopen_offer_pending"] = True
        # Finalization edge into `closed`: snapshot aggregates (unless one
        # was already persisted while open) so dailies/retros keep their
        # counts after the event store is deleted below.
        data, _ = _snapshot_aggregates(data.get("session_id", p.stem), data)
        try:
            # State is written before the rename (crash-safe: a "closed" file
            # at top level is self-describing and migration converges it).
            p.write_text(json.dumps(data, indent=2))
            CLOSED_DIR.mkdir(parents=True, exist_ok=True)
            os.rename(p, CLOSED_DIR / p.name)
        except OSError:
            continue
        # Terminal state reached: the per-prompt event store does not outlive
        # the session. Failure tolerated — gc_closed_event_dirs converges it.
        shutil.rmtree(EVENTS_DIR / data.get("session_id", p.stem), ignore_errors=True)
        swept_signals.append({
            "type": "stale_closing_finalized",
            "session_id": data.get("session_id", p.stem),
            "end_time": end_time_raw,
            "had_checkin": data.get("checkin") is not None,
        })
    return survivors, swept_signals


def _attach_swept(signal, swept_signals):
    """Append sweep signals to a turn's primary signal(s) as a flat list."""
    if not swept_signals:
        return signal
    if signal is None:
        base = []
    elif isinstance(signal, list):
        base = list(signal)
    else:
        base = [signal]
    return base + swept_signals


def _check_reopen_window(config, now):
    """One-shot reopen window: detect the undo turn right after a stale-
    closing sweep, so honouring "no, bring it back" never leaves a phantom
    session behind.

    Called only at the no-open-sessions create sites, just before creating.
    Candidate selection is by max st_mtime over closed/*.json — closed/ can
    grow large, so only the single newest file is ever parsed.

    The window fires when the candidate has reopen_offer_pending == true,
    end_reason == "wrap_up_abandoned", and now − swept_at is within
    closing_window_seconds (missing/unparseable swept_at → expired). The flag
    is cleared in place on consult — both when firing (one-shot: at most one
    REOPEN WINDOW per sweep) and when expired (harmless tidying; a stale flag
    from a user who never returned must not linger).

    Returns a reopen_window signal dict, or None (create normally). Any
    corruption or I/O failure → None; the hook must never crash here.
    """
    newest = None
    newest_mtime = None
    try:
        for p in CLOSED_DIR.glob("*.json"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if newest_mtime is None or m > newest_mtime:
                newest, newest_mtime = p, m
    except OSError:
        return None
    if newest is None:
        return None
    try:
        data = json.loads(newest.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("reopen_offer_pending") is not True:
        return None
    if data.get("end_reason") != "wrap_up_abandoned":
        return None
    try:
        swept_dt = datetime.fromisoformat(data.get("swept_at"))
        seconds_since_sweep = (now - swept_dt).total_seconds()
        expired = seconds_since_sweep > get_closing_window(config)
    except (ValueError, TypeError):
        seconds_since_sweep = None
        expired = True
    # Consume the flag (one-shot when firing, tidying when expired). If the
    # clear can't be persisted, don't fire — an unconsumed offer could fire
    # again next turn, violating the at-most-once contract.
    data["reopen_offer_pending"] = False
    try:
        newest.write_text(json.dumps(data, indent=2))
    except OSError:
        return None
    if expired:
        return None
    return {
        "type": "reopen_window",
        "session_id": data.get("session_id", newest.stem),
        "seconds_since_sweep": round(seconds_since_sweep),
    }


def get_or_create_session(config, now=None):
    """Get the active session or create a new one.

    Returns (path, session, signal) where signal is:
    - None: resumed existing session, no action needed
    - "new_session": created a brand new session
    - dict with type "timeout": idle gap exceeded, session returned as-is for skill to confirm
    - dict with type "multi_session": multiple open sessions found; picked the most-recently-
      active; signal includes "orphan_session_ids" for #10's user-facing wrap-up flow
    - dict with type "wrap_up_pending": the most-recently-active session is in the
      "closing" state within the closing window — its Layer 3 wrap-up (2x2) is still
      pending, so this prompt belongs to the wrap-up conversation; nothing is created
    - dict with type "stale_closing_finalized": a closing session whose wrap-up was
      abandoned (beyond the window) was mechanically finalized by the sweep. If no
      open session remains, (path, session) are (None, None) — a turn that touches
      a closing session never creates a session.
    - dict with type "reopen_window": no open sessions, but the most recently
      swept closed session's reopen offer is still live (one-shot, within the
      closing window of swept_at) — this is the undo turn after a sweep
      announcement; (path, session) are (None, None) and nothing is created.
    - list of dicts: coexisting signals (e.g. timeout + multi_session,
      wrap_up_pending + multi_session, anything + stale_closing_finalized)

    Discovery uses find_open_sessions() (top-level glob only — closed/ excluded).
    The stale-closing sweep runs first; candidate selection and orphan collection
    operate over the survivors. When multiple open sessions remain, we pick the
    one with the most recent event timestamp and surface the others as orphans
    via a TANK signal.

    The returned session is hydrated (metadata + merged events from event log).
    now= is injectable for deterministic boundary tests; defaults to wall clock.
    """
    timeout_threshold = get_session_timeout(config)
    if now is None:
        now = datetime.now().astimezone()

    open_paths = find_open_sessions()

    # Stale-closing sweep (#22): finalize abandoned wrap-ups before any
    # candidate/orphan evaluation, so signals compose over the survivors.
    open_paths, swept_signals = sweep_stale_closing_sessions(open_paths, config, now)

    if not open_paths:
        if swept_signals:
            # Invariant: a turn that touches a closing session never creates
            # a session. The next prompt creates one via the normal path.
            return None, None, swept_signals
        reopen_sig = _check_reopen_window(config, now)
        if reopen_sig is not None:
            # The undo turn after a sweep announcement: create nothing so a
            # reopen-session doesn't leave a phantom session behind.
            return None, None, reopen_sig
        p, s = create_session()
        return p, s, "new_session"

    # Load all open sessions (hydrated) to pick most-recently-active
    candidates = []
    for p in open_paths:
        try:
            session = _load_session_hydrated(p)
        except (json.JSONDecodeError, OSError, KeyError):
            continue
        # Determine last-activity time: latest event ts, or session start_time
        latest_ts = _latest_event_timestamp(session)
        if latest_ts is not None:
            last_active = latest_ts
        else:
            last_active = datetime.fromisoformat(session["start_time"])
        candidates.append((last_active, p, session))

    if not candidates:
        if swept_signals:
            # All survivors were unreadable; the sweep still fired this turn,
            # so create nothing (same invariant as above).
            return None, None, swept_signals
        reopen_sig = _check_reopen_window(config, now)
        if reopen_sig is not None:
            return None, None, reopen_sig
        p, s = create_session()
        return p, s, "new_session"

    # Sort by last_active descending — most recently active is first
    candidates.sort(key=lambda t: t[0], reverse=True)
    _, path, session = candidates[0]

    # If multiple open sessions: surface the extras as orphans (issue #10 handles UX)
    extra_signal = None
    if len(candidates) > 1:
        orphan_ids = [c[2]["session_id"] for c in candidates[1:]]
        # Include per-orphan last-activity timestamps so the SKILL can show
        # "X (last activity 18 hours ago)" without re-reading session files.
        orphan_last_active = {
            c[2]["session_id"]: c[0].isoformat()  # c[0] is last_active datetime
            for c in candidates[1:]
        }
        extra_signal = {
            "type": "multi_session",
            "active_session": session["session_id"],
            "orphan_session_ids": orphan_ids,
            "orphan_last_active": orphan_last_active,
        }

    # Classify the candidate with derive_state — the single oracle for
    # closed-ness (handles legacy files without a state key).
    state = derive_state(session)

    if state == "closing" and session.get("end_time"):
        # The session's work has ended (close-goal parked it) but its Layer 3
        # wrap-up is still pending. The sweep above already finalized stale
        # closings, so any closing survivor is in-window (measured with the
        # same `now`): this prompt is part of the wrap-up conversation —
        # return the session with a wrap_up_pending signal and create nothing.
        # The idle-gap/timeout logic below must NOT run — a wrap-up reply is
        # never a timeout.
        # Guard: a hand-corrupted end_time must never crash the hook.
        try:
            end_time = datetime.fromisoformat(session["end_time"])
        except (ValueError, TypeError):
            end_time = None  # treat as not-in-window; fall through below
        seconds_since_close = (now - end_time).total_seconds() if end_time else None
        if end_time is not None and seconds_since_close <= get_closing_window(config):
            wrap_up_signal = {
                "type": "wrap_up_pending",
                "session_id": session["session_id"],
                "seconds_since_close": round(seconds_since_close),
            }
            if extra_signal is not None:
                return path, session, _attach_swept([wrap_up_signal, extra_signal], swept_signals)
            return path, session, _attach_swept(wrap_up_signal, swept_signals)

    if state != "open":
        # Defensive: the candidate can't accept new work (e.g. a "closed" file
        # written after the migration scan). Stale closings no longer reach
        # here — the sweep finalized them above. If the sweep fired this turn,
        # honour the no-creation invariant; otherwise start fresh.
        if swept_signals:
            return None, None, swept_signals
        p, s = create_session()
        return p, s, "new_session"

    # Check idle gap from last prompt (or session start if no prompts)
    prompts = session.get("prompts", [])
    if prompts:
        last_ts = prompts[-1].get("timestamp")
        if last_ts:
            last_time = datetime.fromisoformat(last_ts)
            gap_minutes = (now - last_time).total_seconds() / 60
        else:
            gap_minutes = 0
    else:
        start_time = datetime.fromisoformat(session["start_time"])
        gap_minutes = (now - start_time).total_seconds() / 60

    if gap_minutes > timeout_threshold:
        # Idle gap exceeded — do NOT create a new session.
        # Return existing session with timeout signal for the skill to handle.
        had_checkin = session.get("checkin") is not None
        timeout_signal = {
            "type": "timeout",
            "gap_minutes": round(gap_minutes),
            "previous_session": session["session_id"],
            "had_checkin": had_checkin,
        }
        # Coexist with multi_session: if both apply, return both as a list so
        # the skill can wrap up orphans AND confirm/close the timed-out active.
        if extra_signal is not None:
            return path, session, _attach_swept([timeout_signal, extra_signal], swept_signals)
        return path, session, _attach_swept(timeout_signal, swept_signals)

    # Return multi_session signal if we found orphans; otherwise None
    # (plus any sweep signals from this turn).
    return path, session, _attach_swept(extra_signal, swept_signals)


def time_since_last_break(session, config):
    """Calculate minutes since last break (gap >= min_break_duration)."""
    min_break = config.get("min_break_duration_minutes", 5)
    prompts = session.get("prompts", [])
    now = datetime.now().astimezone()

    if not prompts:
        # No prompts yet — measure from session start
        start = datetime.fromisoformat(session["start_time"])
        return (now - start).total_seconds() / 60

    # Walk backwards to find the last break
    last_break_time = datetime.fromisoformat(session["start_time"])

    for i in range(1, len(prompts)):
        prev_ts = datetime.fromisoformat(prompts[i - 1]["timestamp"])
        curr_ts = datetime.fromisoformat(prompts[i]["timestamp"])
        gap_minutes = (curr_ts - prev_ts).total_seconds() / 60
        if gap_minutes >= min_break:
            last_break_time = curr_ts

    return (now - last_break_time).total_seconds() / 60


def check_pomodoro(session, config):
    """Check if pomodoro nudge should fire. Returns nudge text or None."""
    interval = config.get("pomodoro_interval_minutes", 60)
    minutes = time_since_last_break(session, config)

    # Check if we already nudged in this stretch
    nudges = session.get("interventions", {}).get("pomodoro_nudges", [])
    min_break = config.get("min_break_duration_minutes", 5)
    prompts = session.get("prompts", [])

    # Find last break time to know which stretch we're in
    last_break_time = datetime.fromisoformat(session["start_time"])
    for i in range(1, len(prompts)):
        prev_ts = datetime.fromisoformat(prompts[i - 1]["timestamp"])
        curr_ts = datetime.fromisoformat(prompts[i]["timestamp"])
        gap = (curr_ts - prev_ts).total_seconds() / 60
        if gap >= min_break:
            last_break_time = curr_ts

    # Check if we already nudged after the last break
    for nudge in nudges:
        nudge_time = datetime.fromisoformat(nudge["fired_at"])
        if nudge_time > last_break_time:
            # Already nudged in this stretch, don't nag
            return None

    if minutes >= interval - 5:  # Fire at 55+ minutes
        return minutes

    return None


def check_hard_boundary_gate(session, config, now):
    """Deterministic Layer-2 GATE: should the hook ask the model to run the
    hard-boundary check this prompt? Returns the in-window retry count if the
    gate trips, else None.

    This is only a cheap mechanical trigger. The hook cannot compute the real
    hard-boundary score — six of the eight signals are semantic (see ADR-0005).
    So it gates on the one strong proxy it owns deterministically: the number of
    retry-classified prompts in the last 20 minutes. When that crosses a low bar,
    the hook emits `[TANK — HARD BOUNDARY CHECK]` and the model does the actual
    detect/score/validate/fire. A loose gate is fine: a false trigger only costs
    a model check, never a false nudge (the model still validates before firing).

    Suppressed in quiet mode, consistent with the pomodoro nudge."""
    if session.get("quiet_mode", False):
        return None
    window_min = config.get("hard_boundary_window_minutes", 20)
    threshold = config.get("hard_boundary_gate_retries", 2)
    cutoff = now - timedelta(minutes=window_min)
    retries = 0
    for p in session.get("prompts", []):
        ts = p.get("timestamp")
        if not ts:
            continue
        if datetime.fromisoformat(ts) >= cutoff and p.get("classification") == "retry":
            retries += 1
    return retries if retries >= threshold else None


def get_open_goal_id_for_instance(session, instance_id):
    """Return the goal_id of the open goal owned by instance_id, or None.

    An "open" goal has ended_at == None (or ended_at absent).
    If the instance has no open goal in this session, returns None.
    """
    for goal in session.get("goals", []):
        if goal.get("instance_id") == instance_id and goal.get("ended_at") is None:
            return goal["id"]
    return None


def instance_needs_goal(session, instance_id, session_signal):
    """Return True when this instance should be prompted for a goal.

    Conditions:
    - There IS an active Tank session (not a brand new session being created now)
    - The instance does NOT have an open goal in that session

    A brand new session (session_signal == "new_session") is excluded because
    the SKILL's startup ritual handles goal collection in that flow.
    """
    # Don't fire GOAL NEEDED on a brand new session — the startup ritual handles it
    if session_signal == "new_session":
        return False
    # Only fire for existing active sessions (resumed, timeout, or multi-session)
    return get_open_goal_id_for_instance(session, instance_id) is None


def render_multi_session_context(sig, now):
    """Render the MULTI_SESSION additionalContext block for a multi_session signal."""
    orphan_last_active = sig.get("orphan_last_active", {})
    orphan_parts = []
    for oid in sig.get("orphan_session_ids", []):
        ts_str = orphan_last_active.get(oid)
        if ts_str:
            try:
                last_dt = datetime.fromisoformat(ts_str)
                age_seconds = (now - last_dt).total_seconds()
                if age_seconds < 3600:
                    age_label = f"{int(age_seconds // 60)} minutes ago"
                else:
                    hours = age_seconds / 3600
                    age_label = f"{hours:.1f} hours ago"
                orphan_parts.append(f"{oid} (last activity {age_label})")
            except ValueError:
                orphan_parts.append(oid)
        else:
            orphan_parts.append(oid)
    orphans_str = ", ".join(orphan_parts)
    orphan_ids_str = ", ".join(sig.get("orphan_session_ids", []))
    return (
        f"[TANK — MULTI_SESSION DETECTED | active_session: {sig['active_session']} "
        f"| orphan_session_ids: {orphan_ids_str} "
        f"| orphan_last_active: {json.dumps(orphan_last_active)}] "
        f"Multiple open Tank sessions found. The most recently active session "
        f"({sig['active_session']}) has been selected. The others are still open: "
        f"{orphans_str}. "
        f"Surface all open sessions to the user with a wrap-up offer "
        f"(see SKILL.md: Multi-Orphan Handling)."
    )


def render_wrap_up_pending_context(sig):
    """Render the WRAP-UP PENDING additionalContext block for a wrap_up_pending signal."""
    return (
        f"[TANK — WRAP-UP PENDING | session_id: {sig['session_id']} "
        f"| seconds_since_close: {sig['seconds_since_close']}] "
        f"The session's work has ended; its wrap-up (2x2 check-in) is still pending. "
        f"This prompt is part of that wrap-up conversation. "
        f"Route a 2x2 reply via state.py end-session. "
        f"Do NOT start a new session, startup ritual, or goal ritual. "
        f"If the user is clearly starting new work instead, "
        f"run state.py start-session explicitly."
    )


def render_stale_closing_context(sig):
    """Render the STALE CLOSING FINALIZED additionalContext block for a
    stale_closing_finalized signal.

    Include '(no check-in recorded)' only when had_checkin is False/absent —
    a session that somehow carried a non-null checkin must not be falsely
    described as having no check-in.
    """
    sid = sig["session_id"]
    had_checkin = sig.get("had_checkin", False)
    checkin_clause = " (no check-in recorded)" if not had_checkin else ""
    return (
        f"[TANK — STALE CLOSING FINALIZED | session_id: {sid} "
        f"| end_time: {sig.get('end_time')}] "
        f"Session {sid}'s wrap-up was never completed; it has been finalized "
        f'with end_reason "wrap_up_abandoned"{checkin_clause}. '
        f'Announce briefly: "Closing session {sid}. Skipping end-of-session '
        f'check-out ritual." If the user objects, run state.py reopen-session '
        f"{sid} to restore it. Do not ask a question — this is an announcement "
        f"with an undo, not a decision point."
    )


def render_reopen_window_context(sig):
    """Render the REOPEN WINDOW additionalContext block for a reopen_window
    signal — the one-shot undo turn after a stale-closing sweep announcement.
    """
    sid = sig["session_id"]
    return (
        f"[TANK — REOPEN WINDOW | session_id: {sid} "
        f"| seconds_since_sweep: {sig['seconds_since_sweep']}] "
        f"Session {sid} was finalized as abandoned moments ago. "
        f"If the user is objecting to that closure, run state.py "
        f"reopen-session {sid} to restore it. Otherwise respond normally — "
        f"a new session will start on the next prompt. "
        f"If the user is clearly starting new work right now, run state.py start-session explicitly. "
        f"Do not log this prompt or start any ritual."
    )


def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    # Ensure tank directory exists
    if not TANK_DIR.exists():
        # First run — don't do anything, let the skill handle onboarding
        sys.exit(0)

    # Skip subagent prompts before ANY session logic.
    # Session lifecycle transitions (sweep, creation, logging) must only happen
    # on user-visible turns because their announcements carry an undo contract:
    # a STALE CLOSING FINALIZED emitted here would be silently swallowed by the
    # subagent harness — the next real user prompt would then see a brand-new
    # session with no explanation. Full no-op: no sweep, no creation, no logging,
    # no signals.
    if hook_input.get("agent_id"):
        sys.exit(0)

    config = read_config()
    now = datetime.now().astimezone()

    # Convergence GC before any session logic: event stores of closed
    # sessions are deleted (crash leftovers, legacy accumulation).
    gc_closed_event_dirs()

    # Get or create session
    path, session, session_signal = get_or_create_session(config, now=now)

    # Normalize session_signal to a list so coexisting signals are uniform
    # (timeout + multi_session, wrap_up_pending + multi_session,
    # anything + stale_closing_finalized).
    if session_signal is None:
        signals = []
    elif isinstance(session_signal, list):
        signals = session_signal
    else:
        signals = [session_signal]

    # Session-is-None early path: either the sweep finalized abandoned
    # wrap-up(s) this turn, or this is the one-shot reopen window right after
    # a sweep announcement. In both cases create nothing (the next prompt
    # starts a session via the normal path). Same hygiene as the wrap-up
    # path: no event logging, no metadata write-back (the one-shot flag clear
    # already happened inside get_or_create_session), no pomodoro, no GOAL
    # NEEDED. Just announce.
    if session is None:
        context_parts = []
        for sig in signals:
            if not isinstance(sig, dict):
                continue
            if sig.get("type") == "stale_closing_finalized":
                context_parts.append(render_stale_closing_context(sig))
            elif sig.get("type") == "reopen_window":
                context_parts.append(render_reopen_window_context(sig))
        if context_parts:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n\n".join(context_parts),
                }
            }
            print(json.dumps(output))
        sys.exit(0)

    # WRAP-UP PENDING early path: the selected session is in the closing
    # window — this prompt is part of the wrap-up conversation, not session
    # work. Emit the context (plus any coexisting multi_session orphans) and
    # exit: no event-log write, no session metadata write-back, no pomodoro
    # check, no GOAL NEEDED.
    wrap_up_signal = next(
        (s for s in signals
         if isinstance(s, dict) and s.get("type") == "wrap_up_pending"),
        None,
    )
    if wrap_up_signal is not None:
        context_parts = [render_wrap_up_pending_context(wrap_up_signal)]
        for sig in signals:
            if isinstance(sig, dict) and sig.get("type") == "multi_session":
                context_parts.append(render_multi_session_context(sig, now))
            elif isinstance(sig, dict) and sig.get("type") == "stale_closing_finalized":
                context_parts.append(render_stale_closing_context(sig))
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(context_parts),
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    # Instance identity: Claude Code's session_id is the stable per-conversation
    # identifier. We use it as instance_id to name the per-instance event log.
    instance_id = hook_input.get("session_id") or "unknown"
    cwd = hook_input.get("cwd")

    # Extract prompt text
    prompt_text = hook_input.get("prompt", "")
    tracking_mode = config.get("prompt_tracking_mode", "fingerprint")

    # Look up the open goal owned by this instance (may be None)
    goal_id_for_instance = get_open_goal_id_for_instance(session, instance_id)

    # Log the prompt event — seed with instance identity fields
    prompt_event = {
        "timestamp": now.isoformat(),
        "type": "prompt",
        "instance_id": instance_id,
        "goal_id": goal_id_for_instance,
    }
    if cwd is not None:
        prompt_event["cwd"] = cwd

    # Calculate gap since last prompt
    prompts = session.get("prompts", [])
    if prompts:
        last_ts = datetime.fromisoformat(prompts[-1]["timestamp"])
        gap_seconds = (now - last_ts).total_seconds()
        prompt_event["gap_since_last_seconds"] = round(gap_seconds)

        # Detect if the gap was a break
        min_break = config.get("min_break_duration_minutes", 5)
        if gap_seconds / 60 >= min_break:
            prompt_event["after_break"] = True

    # Content fingerprinting (skip for tool-loading prompts)
    if prompt_text and not is_tool_loading_prompt(prompt_text):
        fingerprint = compute_fingerprint(prompt_text)
        files = fingerprint["files"]

        # Specificity score
        specificity = compute_specificity_score(prompt_text, files)

        # Retry detection — compare against last 5 fingerprints
        recent_fingerprints = [
            p["content_fingerprint"]
            for p in prompts[-5:]
            if p.get("content_fingerprint")
        ]
        retry_sim = compute_retry_similarity(fingerprint, recent_fingerprints)

        # Classification
        classification = classify_prompt(fingerprint, retry_sim, prompts[-5:])

        # Task ID
        task_id = infer_task_id(fingerprint)

        # Token count approximation
        prompt_length_tokens = round(len(prompt_text.split()) * 1.3)

        # Has questions
        has_questions = '?' in prompt_text or any(
            w in prompt_text.lower() for w in ["why ", "how ", "what ", "when ", "where "]
        )

        # Signal detection
        signals_fired = []

        # Retry loop signal: 2+ retries in last 5 prompts
        if classification == "retry":
            recent_retries = sum(
                1 for p in prompts[-5:]
                if p.get("classification") == "retry"
            )
            if recent_retries >= 1:  # this one + 1 previous = 2+
                signals_fired.append("retry_loop")

        # Narrowing curiosity: question rate dropping
        if len(prompts) >= 15:
            baseline_q = sum(1 for p in prompts[:10] if p.get("has_questions"))
            recent_q = sum(1 for p in prompts[-5:] if p.get("has_questions"))
            baseline_rate = baseline_q / 10
            recent_rate = recent_q / 5
            if baseline_rate > 0 and (baseline_rate - recent_rate) / baseline_rate >= 0.5:
                signals_fired.append("narrowing_curiosity")

        # Decision deferral
        lower_text = prompt_text.lower()
        if any(phrase in lower_text for phrase in DEFERRAL_PHRASES):
            signals_fired.append("decision_deferral")

        # Completion fixation (after pomodoro nudge)
        pomodoro_nudges = session.get("interventions", {}).get("pomodoro_nudges", [])
        if pomodoro_nudges and any(phrase in lower_text for phrase in FIXATION_PHRASES):
            signals_fired.append("completion_fixation")

        # Populate event
        if tracking_mode == "fingerprint":
            prompt_event["content_fingerprint"] = fingerprint
            prompt_event["content_summary"] = None
        else:
            # Summary mode — store fingerprint anyway for retry detection,
            # leave content_summary for the skill to fill
            prompt_event["content_fingerprint"] = fingerprint
            prompt_event["content_summary"] = None

        prompt_event["specificity_score"] = round(specificity, 2)
        prompt_event["retry_similarity"] = retry_sim
        prompt_event["classification"] = classification
        prompt_event["task_id"] = task_id
        prompt_event["prompt_length_tokens"] = prompt_length_tokens
        prompt_event["files_referenced"] = files
        prompt_event["has_questions"] = has_questions
        prompt_event["signals_fired"] = signals_fired
    else:
        # Tool-loading or empty prompt
        prompt_event["classification"] = "continuation"
        prompt_event["content_fingerprint"] = None
        prompt_event["content_summary"] = None
        prompt_event["signals_fired"] = []

    # Write prompt event to per-instance append-only event log.
    # instance_id is the Claude Code session_id from the hook payload —
    # stable across all prompts in one conversation, unique per running instance.
    session_id = session["session_id"]
    log_dir = EVENTS_DIR / session_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{instance_id}.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(prompt_event, separators=(",", ":")) + "\n")

    # Also append the event to the in-memory session so that pomodoro/signal
    # checks below see the current prompt without needing to re-read the log.
    session["prompts"].append(prompt_event)

    # Update duration in session metadata (not the full hydrated session)
    start = datetime.fromisoformat(session["start_time"])
    session["duration_minutes"] = round((now - start).total_seconds() / 60, 1)

    # Build additionalContext parts
    context_parts = []

    # Session signal context (signals normalized to a list above)
    for sig in signals:
        if isinstance(sig, dict) and sig.get("type") == "timeout":
            context_parts.append(
                f"[TANK — TIMEOUT DETECTED | gap_minutes: {sig['gap_minutes']} "
                f"| previous_session: {sig['previous_session']} "
                f"| had_checkin: {'true' if sig['had_checkin'] else 'false'}]"
            )
        elif isinstance(sig, dict) and sig.get("type") == "multi_session":
            context_parts.append(render_multi_session_context(sig, now))
        elif isinstance(sig, dict) and sig.get("type") == "stale_closing_finalized":
            context_parts.append(render_stale_closing_context(sig))
        elif sig == "new_session":
            context_parts.append(
                f"[TANK — NEW SESSION | session_id: {session['session_id']}]"
            )

    # Goal-needed signal: fires when an existing instance joins an active Tank
    # session but doesn't yet have an open goal. NOT fired on brand new sessions
    # (the startup ritual handles goal collection there).
    if instance_needs_goal(session, instance_id, session_signal):
        context_parts.append(
            f"[TANK — GOAL NEEDED | session_id: {session['session_id']} "
            f"| instance_id: {instance_id}] "
            f"This instance has no open goal in the active Tank session. "
            f"Run the goal-only mini-ritual: ask the user what they are working on here, "
            f"then call `state.py add-goal {session['session_id']} {instance_id} <text>`."
        )

    # Check pomodoro (suppressed in quiet mode)
    quiet_mode = session.get("quiet_mode", False)
    nudge_minutes = None if quiet_mode else check_pomodoro(session, config)

    if nudge_minutes is not None:
        mins = round(nudge_minutes)
        context_parts.append(
            f"[TANK — POMODORO NUDGE] "
            f"The developer has been working for ~{mins} minutes without a break "
            f"(threshold: {config.get('pomodoro_interval_minutes', 60)} min). "
            f"Prepend the following nudge to your response:\n\n"
            f"───────────────────────────────────────\n"
            f"⏱  {mins} minutes in. Good stopping point.\n"
            f"   Take 5 — I'll be here when you get back.\n"
            f"───────────────────────────────────────\n\n"
            f"Then continue with your normal response."
        )

        # Record the nudge in session metadata
        session.setdefault("interventions", {}).setdefault("pomodoro_nudges", []).append({
            "fired_at": now.isoformat(),
            "response": "pending",
        })

    # Layer 2 gate: a cheap mechanical trigger that asks the model to run the
    # hard-boundary check. The hook does NOT decide to fire — the model does the
    # semantic scoring and validation (see check_hard_boundary_gate / ADR-0005).
    gate_retries = check_hard_boundary_gate(session, config, now)
    if gate_retries is not None:
        sid = session["session_id"]
        context_parts.append(
            f"[TANK — HARD BOUNDARY CHECK | session_id: {sid} | retry_loops: {gate_retries}] "
            f"Recent prompts show possible grinding. Before responding, run the Layer 2 "
            f"hard-boundary check (do not skip it):\n"
            f"1. Run `state.py get-session-summary {sid}` to read accumulated signals.\n"
            f"2. Detect any new form-degradation signals in the current prompt "
            f"(see references/signals.md) and record each via `state.py log-signal`.\n"
            f"3. Score the last 20 minutes per references/signals.md: fire if score >= 6, "
            f"OR >= 5 with at least one high-confidence signal.\n"
            f"4. Validate with the meta-prompt. If the evidence is ambiguous, do NOT fire.\n"
            f"5. If it fires, prepend the Hard Boundary nudge; otherwise respond normally."
        )

    # Write session metadata back (without the events — those live in the event log).
    # Strip the in-memory prompts before persisting so events aren't double-stored.
    session_to_persist = {k: v for k, v in session.items() if k != "prompts"}
    session_to_persist["prompts"] = []  # keep the key for old-format compat
    path.write_text(json.dumps(session_to_persist, indent=2))

    # Output hook response
    if context_parts:
        context = "\n\n".join(context_parts)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
        print(json.dumps(output))

    sys.exit(0)


if __name__ == "__main__":
    main()
