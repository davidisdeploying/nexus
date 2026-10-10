"""
Worker Activity — the CLI-over-the-shoulder view.

The module keeps its historical filename so transcript parsing and imports do
not churn during the user-facing information-architecture rename.

Renders a headless `claude -p` run's FULL session transcript as a readable
conversation scroll (assistant text · thinking · tool_use with inputs ·
tool_result with outputs) and live-tails an in-flight run so it streams in real
time. This is a SEPARATE subsystem from the scan-log (the lightweight hook
ticker on the dashboard) — different route, different channel, own parser.

--- PHASE 0 ground truth (the transcript shape, inspected on a real 1.23 MB run
    `LEGACY-BUILD-20260704-scanlog-worker2-aware.json`, NOT remembered) ---
The transcript is JSONL: ONE json object per line (331 lines / 1.23 MB here).
Record `type` values seen:
  system   — subtypes: init (session header: model, cwd, tools), thinking_tokens
             (pure token-estimate noise, skipped), task_started, task_notification.
  rate_limit_event — skipped.
  assistant — message.content is a LIST of blocks: {type:thinking, thinking, signature},
              {type:text, text}, {type:tool_use, id, name, input(dict)}.
  user      — message.content is a LIST of blocks: {type:tool_result, tool_use_id,
              content, is_error}. `content` is usually a STR but can be a LIST of
              sub-blocks ({type:text|tool_reference|image, ...}).
  result    — one final record; `result` holds the closing assistant text.
So: assistant carries thinking/text/tool_use; the paired tool_result arrives in the
NEXT user record, keyed by tool_use_id. We render events in file order (a tool_use
row is immediately followed by its tool_result row) — no cross-record join needed.

--- Path safety (non-negotiable) ---
We ONLY ever open `<token>.json` directly under one of the three known transcript
dirs (from-{worker1,worker3,localworker,worker2}/transcripts). The token is validated against a strict
charset and must not contain a path separator; the resolved file must live inside a
whitelisted dir. No caller-supplied path is ever accepted. Mirrors wiki.py's
resolve()+relative_to() approach.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .config import settings
from .seats import RUN_SOURCE_IDS, RUN_SOURCE_TO_CARD, SEAT_CLASS

# from-{seat}/{runs,transcripts} live under the relay root, split from
# vault_root (loupe-vault, heartbeats only) on 2026-07-10.
RELAY = settings.relay_root

# Current node roots, legacy aliases, and palette classes come from app/seats.py.

# The ONLY dirs a transcript may be read from. resolve() once so every safety
# check compares against the real (symlink-collapsed) path.
TRANSCRIPT_DIRS = {
    source: (RELAY / f"from-{source}" / "transcripts").resolve()
    for source in RUN_SOURCE_IDS
}

# Strict token charset. Excludes '/', so a separator can never slip through; the
# resolve()+relative_to() check below is the belt to this suspenders.
TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TRANSCRIPT_SUFFIXES = (".json", ".txt")

# Per-block output caps (Pi is an 8 GB box; transcripts run ~1.2 MB and a single
# tool_result can be a whole file dump). We hard-cap what lands in the DOM and note
# how many chars were dropped rather than blasting the raw blob into one node.
MAX_TOOL_RESULT = 8000
MAX_TOOL_INPUT = 4000
MAX_TEXT = 20000

# Default number of most-recent events rendered on first paint ("load earlier"
# reloads with a higher cap). Keeps a long run from painting thousands of rows.
DEFAULT_EVENT_CAP = 220
HARD_EVENT_CAP = 6000  # absolute ceiling even for ?limit=all


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #
def valid_token(token: str) -> bool:
    """A token is safe only if it matches the strict charset and is not a dotfile
    traversal. '/' and whitespace are already excluded by the charset."""
    if not token or len(token) > 200:
        return False
    if token in (".", ".."):
        return False
    return bool(TOKEN_RE.match(token))


def resolve_transcript(token: str) -> Optional[tuple[str, Path]]:
    """Resolve a token to (seat, real path) for an existing transcript, or None.

    Returns a Path ONLY when the token is charset-valid AND `<token>.json`
    (or a legacy Gemini `<token>.txt`)
    resolves strictly inside one of the whitelisted transcript dirs AND exists as
    a regular file. Never hands back a path outside those three dirs.
    """
    if not valid_token(token):
        return None
    for seat, base in TRANSCRIPT_DIRS.items():
        for suffix in TRANSCRIPT_SUFFIXES:
            candidate = (base / f"{token}{suffix}")
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            try:
                resolved.relative_to(base)  # strictly inside this transcript dir?
            except ValueError:
                continue
            if resolved.suffix.lower() not in TRANSCRIPT_SUFFIXES:
                continue
            if resolved.is_file():
                return seat, resolved
    return None


# --------------------------------------------------------------------------- #
# Run list  — transcripts that exist, newest first, with run state.
# --------------------------------------------------------------------------- #
def _run_state(seat: str, token: str, mtime: float) -> str:
    """State for a run, from the `done` sentinel (its content is the exit code),
    falling back to status.json, then to age (mirrors work.read_relay_runs)."""
    d = RELAY / f"from-{seat}" / "runs" / token
    done_f = d / "done"
    try:
        if done_f.exists():
            code = done_f.read_text(errors="replace").strip()[:32]
            return "done" if code in ("", "0") else "died"
    except Exception:
        pass
    try:
        sj = (d / "status.json").read_text(errors="replace")[:2000]
        m = re.search(r'"status"\s*:\s*"([^"]+)"', sj)
        if m and m.group(1) != "running":
            return m.group(1)
    except Exception:
        pass
    # No sentinel: running unless the transcript has gone stale (>6h untouched).
    if (time.time() - mtime) > 6 * 3600:
        return "died"
    return "running"


def _rel_age(mtime: float) -> str:
    secs = max(0, time.time() - mtime)
    if secs < 90:
        return "just now"
    mins = secs / 60
    if mins < 90:
        return f"{int(mins)}m ago"
    hrs = mins / 60
    if hrs < 36:
        return f"{int(hrs)}h ago"
    return f"{int(hrs / 24)}d ago"


def list_runs(limit: int = 60) -> list[dict[str, Any]]:
    """Every run that has a transcript file, newest first. token · seat · state ·
    age. Cheap: a stat per file, no parse."""
    rows: list[dict[str, Any]] = []
    for seat, base in TRANSCRIPT_DIRS.items():
        try:
            # Prefer structured JSON when both a new transcript and a legacy
            # Gemini text transcript exist for the same token.
            by_token = {f.stem: f for f in base.glob("*.txt")}
            by_token.update({f.stem: f for f in base.glob("*.json")})
            entries = list(by_token.values())
        except Exception:
            continue
        for f in entries:
            try:
                st = f.stat()
            except Exception:
                continue
            token = f.stem
            if not valid_token(token):
                continue
            display_seat = RUN_SOURCE_TO_CARD.get(seat, seat)
            rows.append({
                "token": token,
                "seat": display_seat,
                "seat_class": SEAT_CLASS.get(display_seat, ""),
                "state": _run_state(seat, token, st.st_mtime),
                "age": _rel_age(st.st_mtime),
                "size": st.st_size,
                "_mtime": st.st_mtime,
            })
    rows.sort(key=lambda r: r["_mtime"], reverse=True)
    for r in rows:
        r.pop("_mtime", None)
    return rows[:limit]


def run_state_now(token: str) -> Optional[str]:
    """State for a single token (used by the tailer to decide when a run ends)."""
    hit = resolve_transcript(token)
    if hit is None:
        return None
    seat, path = hit
    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = time.time()
    return _run_state(seat, token, mtime)


# --------------------------------------------------------------------------- #
# Parser  — one JSONL line -> zero or more render events, in file order.
# --------------------------------------------------------------------------- #
def _truncate(s: str, cap: int) -> tuple[str, int]:
    """Return (clipped, dropped_chars). dropped 0 when it fit."""
    if s is None:
        return "", 0
    if len(s) <= cap:
        return s, 0
    return s[:cap], len(s) - cap


def _flatten_tool_result(content: Any) -> str:
    """tool_result.content is usually a str; sometimes a list of sub-blocks
    (text / tool_reference / image). Flatten to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                parts.append(str(b))
                continue
            bt = b.get("type")
            if bt == "text":
                parts.append(b.get("text", ""))
            elif bt == "image":
                parts.append("[image]")
            else:
                parts.append(json.dumps(b) if bt else str(b))
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def record_to_events(obj: dict) -> list[dict[str, Any]]:
    """Turn one parsed JSONL record into zero+ render events (file order)."""
    # Codex exec JSONL. It records lifecycle events plus item payloads rather
    # than Claude-style role messages.
    codex_type = obj.get("type")
    if codex_type == "thread.started":
        return [{"kind": "session", "model": "codex", "cwd": ""}]
    if codex_type in ("item.started", "item.completed"):
        item = obj.get("item") or {}
        item_type = item.get("type")
        completed = codex_type == "item.completed"
        if item_type == "agent_message" and completed:
            text, dropped = _truncate(str(item.get("text", "")), MAX_TEXT)
            return [{
                "kind": "text",
                "role": "assistant",
                "text": text,
                "dropped": dropped,
            }] if text else []
        if item_type == "command_execution":
            if not completed:
                text, dropped = _truncate(str(item.get("command", "")), MAX_TOOL_INPUT)
                return [{
                    "kind": "tool_use",
                    "name": "command_execution",
                    "input": text,
                    "dropped": dropped,
                }]
            text, dropped = _truncate(
                str(item.get("aggregated_output", "")), MAX_TOOL_RESULT
            )
            code = item.get("exit_code")
            return [{
                "kind": "tool_result",
                "tool_use_id": str(item.get("id", "")),
                "text": text,
                "dropped": dropped,
                "is_error": code not in (None, 0),
            }]
        if item_type == "file_change":
            changes = item.get("changes") or []
            payload = json.dumps(changes, indent=2, ensure_ascii=False)
            text, dropped = _truncate(
                payload, MAX_TOOL_RESULT if completed else MAX_TOOL_INPUT
            )
            if not completed:
                return [{
                    "kind": "tool_use",
                    "name": "file_change",
                    "input": text,
                    "dropped": dropped,
                }]
            return [{
                "kind": "tool_result",
                "tool_use_id": str(item.get("id", "")),
                "text": text,
                "dropped": dropped,
                "is_error": item.get("status") not in (None, "completed"),
            }]
        if item_type == "reasoning" and completed:
            text, dropped = _truncate(str(item.get("text", "")), MAX_TEXT)
            return [{
                "kind": "thinking",
                "text": text,
                "dropped": dropped,
            }] if text else []
        return []

    # Gemini stream-json. Its event-oriented schema maps onto the same
    # timeline primitives used by Claude and Codex.
    ag_event = obj.get("event")
    if ag_event == "init":
        init = obj.get("init") or {}
        return [{
            "kind": "session",
            "model": init.get("model", ""),
            "cwd": init.get("cwd", ""),
        }]
    if ag_event == "step_update":
        step = obj.get("step_update") or {}
        step_type = step.get("step_type")
        if step_type == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                text, dropped = _truncate(delta, MAX_TEXT)
                return [{
                    "kind": "text",
                    "role": "assistant",
                    "text": text,
                    "dropped": dropped,
                }]
            return []
        if step_type == "tool":
            info = step.get("tool_info") or {}
            name = step.get("tool_name") or info.get("name") or "tool"
            if step.get("state") == "ACTIVE":
                raw_input = info.get("parameters") or {}
                inp = json.dumps(raw_input, indent=2, ensure_ascii=False)
                text, dropped = _truncate(inp, MAX_TOOL_INPUT)
                return [{
                    "kind": "tool_use",
                    "name": name,
                    "input": text,
                    "dropped": dropped,
                }]
            if step.get("state") == "DONE":
                text, dropped = _truncate(str(info.get("output", "")), MAX_TOOL_RESULT)
                return [{
                    "kind": "tool_result",
                    "tool_use_id": str(step.get("step_index", "")),
                    "text": text,
                    "dropped": dropped,
                    "is_error": bool(step.get("error")),
                }]
        return []
    if ag_event == "result":
        result = obj.get("result") or {}
        text, dropped = _truncate(str(result.get("response", "")), MAX_TEXT)
        return [{
            "kind": "result",
            "text": text,
            "dropped": dropped,
        }] if text else []

    t = obj.get("type")
    out: list[dict[str, Any]] = []

    if t == "system":
        sub = obj.get("subtype")
        if sub == "init":
            out.append({
                "kind": "session",
                "model": obj.get("model", ""),
                "cwd": obj.get("cwd", ""),
                "session_id": obj.get("session_id", ""),
            })
        elif sub == "task_started":
            out.append({"kind": "task", "status": "started",
                        "desc": obj.get("description", "")})
        elif sub == "task_notification":
            out.append({"kind": "task", "status": obj.get("status", ""),
                        "desc": obj.get("summary", "")})
        # thinking_tokens: pure noise, dropped.
        return out

    if t == "result":
        text, dropped = _truncate(obj.get("result", "") or "", MAX_TEXT)
        out.append({"kind": "result", "text": text, "dropped": dropped})
        return out

    if t in ("assistant", "user"):
        content = obj.get("message", {}).get("content")
        if isinstance(content, str):
            text, dropped = _truncate(content, MAX_TEXT)
            out.append({"kind": "text", "role": t, "text": text, "dropped": dropped})
            return out
        if not isinstance(content, list):
            return out
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "thinking":
                th = b.get("thinking", "") or ""
                if not th.strip():
                    continue  # empty leading thinking block — skip
                text, dropped = _truncate(th, MAX_TEXT)
                out.append({"kind": "thinking", "text": text, "dropped": dropped})
            elif bt == "text":
                txt = b.get("text", "") or ""
                if not txt.strip():
                    continue
                text, dropped = _truncate(txt, MAX_TEXT)
                out.append({"kind": "text", "role": t, "text": text, "dropped": dropped})
            elif bt == "tool_use":
                inp = b.get("input", {})
                try:
                    inp_s = json.dumps(inp, indent=2, ensure_ascii=False)
                except Exception:
                    inp_s = str(inp)
                inp_s, dropped = _truncate(inp_s, MAX_TOOL_INPUT)
                out.append({
                    "kind": "tool_use",
                    "id": b.get("id", ""),
                    "name": b.get("name", "tool"),
                    "input": inp_s,
                    "dropped": dropped,
                })
            elif bt == "tool_result":
                raw = _flatten_tool_result(b.get("content"))
                text, dropped = _truncate(raw, MAX_TOOL_RESULT)
                out.append({
                    "kind": "tool_result",
                    "tool_use_id": b.get("tool_use_id", ""),
                    "text": text,
                    "dropped": dropped,
                    "is_error": bool(b.get("is_error")),
                })
        return out

    # rate_limit_event and anything unknown: dropped.
    return out


def iter_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Stream JSONL lines -> render events. A malformed line is skipped, not fatal."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        for ev in record_to_events(obj):
            yield ev


def read_session(path: Path, cap: int = DEFAULT_EVENT_CAP) -> dict[str, Any]:
    """Parse a transcript file to a capped tail of render events.

    Streams the file (never json.load-the-world), keeps only the last `cap`
    events in a bounded deque plus a running total, and returns the byte size at
    end-of-read so the live tail can resume from exactly there with no gap/dup.
    """
    cap = max(1, min(cap, HARD_EVENT_CAP))
    tail: deque = deque(maxlen=cap)
    total = 0
    session: Optional[dict[str, Any]] = None
    try:
        if path.suffix.lower() == ".txt":
            # Compatibility for Gemini runs created before Tower switched to
            # native stream-json. These contain the final response only.
            raw = path.read_text(encoding="utf-8", errors="replace")
            text, dropped = _truncate(raw, MAX_TEXT)
            if text:
                tail.append({"kind": "result", "text": text, "dropped": dropped})
                total = 1
            raise StopIteration
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ev in iter_events(f):
                if ev["kind"] == "session" and session is None:
                    session = ev
                total += 1
                tail.append(ev)
    except StopIteration:
        pass
    except Exception:
        pass
    try:
        size = path.stat().st_size
    except Exception:
        size = 0
    events = list(tail)
    return {
        "events": events,
        "total": total,
        "shown": len(events),
        "truncated_head": total - len(events),
        "size": size,
        "session": session,
    }


# --------------------------------------------------------------------------- #
# HTML render  — ONE renderer, used by both the server page (join all) and the
# live-tail WS (per-event fragment). Keeps Python the single source of truth so
# streamed rows and first-paint rows are byte-identical.
# --------------------------------------------------------------------------- #
def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _dropnote(n: int) -> str:
    if not n:
        return ""
    return (f'<span class="hp-drop">… {n:,} more chars truncated</span>')


def render_event_html(ev: dict[str, Any]) -> str:
    """Render one event to a timeline row. Tool blocks are collapsible <details>
    (closed by default) so large outputs stay out of layout until expanded."""
    kind = ev.get("kind")

    if kind == "session":
        model = _esc(ev.get("model"))
        cwd = _esc(ev.get("cwd"))
        return (f'<div class="hp-row hp-session"><div class="hp-node"></div>'
                f'<div class="hp-body"><span class="hp-label">session start</span>'
                f'<span class="hp-meta">{model}'
                f'{" · " + cwd if cwd else ""}</span></div></div>')

    if kind == "thinking":
        return (f'<div class="hp-row hp-thinking"><div class="hp-node"></div>'
                f'<div class="hp-body"><span class="hp-label">thinking</span>'
                f'<div class="hp-think-text">{_esc(ev.get("text"))}'
                f'{_dropnote(ev.get("dropped", 0))}</div></div></div>')

    if kind == "text":
        role = ev.get("role", "assistant")
        who = "assistant" if role == "assistant" else "user"
        return (f'<div class="hp-row hp-text hp-{who}"><div class="hp-node"></div>'
                f'<div class="hp-body"><span class="hp-label">{who}</span>'
                f'<div class="hp-prose">{_esc(ev.get("text"))}'
                f'{_dropnote(ev.get("dropped", 0))}</div></div></div>')

    if kind == "tool_use":
        name = _esc(ev.get("name"))
        return (f'<div class="hp-row hp-tooluse"><div class="hp-node"></div>'
                f'<div class="hp-body"><details class="hp-tool">'
                f'<summary><span class="hp-label">tool</span>'
                f'<span class="hp-toolname">{name}</span></summary>'
                f'<pre class="hp-pre">{_esc(ev.get("input"))}'
                f'{_dropnote(ev.get("dropped", 0))}</pre></details></div></div>')

    if kind == "tool_result":
        err = ev.get("is_error")
        cls = "hp-toolresult" + (" hp-error" if err else "")
        label = "error" if err else "result"
        return (f'<div class="hp-row {cls}"><div class="hp-node"></div>'
                f'<div class="hp-body"><details class="hp-tool">'
                f'<summary><span class="hp-label">{label}</span>'
                f'<span class="hp-connector">└ tool output</span></summary>'
                f'<pre class="hp-pre">{_esc(ev.get("text"))}'
                f'{_dropnote(ev.get("dropped", 0))}</pre></details></div></div>')

    if kind == "task":
        return (f'<div class="hp-row hp-task"><div class="hp-node"></div>'
                f'<div class="hp-body"><span class="hp-label">task '
                f'{_esc(ev.get("status"))}</span>'
                f'<span class="hp-meta">{_esc(ev.get("desc"))}</span></div></div>')

    if kind == "result":
        return (f'<div class="hp-row hp-result"><div class="hp-node"></div>'
                f'<div class="hp-body"><span class="hp-label">run complete</span>'
                f'<div class="hp-prose">{_esc(ev.get("text"))}'
                f'{_dropnote(ev.get("dropped", 0))}</div></div></div>')

    return ""


def render_events_html(events: list[dict[str, Any]]) -> str:
    return "".join(render_event_html(ev) for ev in events)


# --------------------------------------------------------------------------- #
# Live tail  — read only appended bytes since `offset`, buffer an incomplete
# trailing line, emit only COMPLETE records. Returns (new_events, new_offset,
# saw_result). Pure/synchronous; the WS route drives it on a poll cadence and
# runs it off-thread.
# --------------------------------------------------------------------------- #
def tail_since(path: Path, offset: int, carry: str) -> dict[str, Any]:
    """Read bytes [offset, EOF); split into lines; parse complete lines to events;
    keep the final incomplete line as `carry` for next call.

    Handles truncation/rotation: if the file shrank below `offset`, resync to the
    new EOF (a growing transcript never shrinks, so this only guards against a
    delete/replace and prevents re-streaming the whole file)."""
    try:
        size = path.stat().st_size
    except Exception:
        return {"events": [], "offset": offset, "carry": carry, "saw_result": False}

    if size < offset:  # rotated/truncated -> resync, drop stale carry
        offset = size
        carry = ""
        return {"events": [], "offset": offset, "carry": carry, "saw_result": False}
    if size == offset:
        return {"events": [], "offset": offset, "carry": carry, "saw_result": False}

    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
        offset = size
    except Exception:
        return {"events": [], "offset": offset, "carry": carry, "saw_result": False}

    data = carry + chunk.decode("utf-8", errors="replace")
    # Keep the trailing (possibly incomplete) segment as carry unless it ends on a
    # newline, in which case there is no incomplete tail.
    if data.endswith("\n"):
        complete, new_carry = data, ""
    else:
        idx = data.rfind("\n")
        if idx == -1:
            # no complete line yet — hold everything
            return {"events": [], "offset": offset, "carry": data, "saw_result": False}
        complete, new_carry = data[: idx + 1], data[idx + 1:]

    events: list[dict[str, Any]] = []
    saw_result = False
    for ev in iter_events(complete.splitlines()):
        events.append(ev)
        if ev["kind"] == "result":
            saw_result = True
    return {"events": events, "offset": offset, "carry": new_carry,
            "saw_result": saw_result}
