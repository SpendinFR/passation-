#!/usr/bin/env python3
"""Passive handoff snapshot for Codex CLI and Claude Code.

This script does NOT modify Codex or Claude configuration, install hooks,
or inject anything into either agent loop. It only reads:
- the repository's Git state;
- local session files already written by Codex/Claude (best effort);
- the local process list to detect an active agent.

It writes a single file in the repository: passationlive.md.

Usage:
  python3 handoff_snapshot.py --repo .
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

OUTPUT_NAME = "passationlive.md"
SCRIPT_VERSION = "1.2.0-passive-timeline"
STATE_PREFIX = "<!-- PASSATION_PASSIVE_STATE_V1:"
STATE_SUFFIX = "-->"
MAX_ACTIONS = 20
MAX_FILES = 50
MAX_COMMITS = 10
MAX_COMMIT_FILE_DETAILS = 3
MAX_FILES_PER_COMMIT = 20
MAX_TRANSCRIPT_TAIL_BYTES = 3_000_000
MAX_TRANSCRIPT_HEAD_BYTES = 500_000
MAX_TRANSCRIPT_FILES = 200
MAX_MATCHED_TRANSCRIPTS = 8
MAX_EXCHANGES = 4
MAX_TIMELINE_MILESTONES = 8
DEFAULT_INTERVAL = 8.0
DEFAULT_STALE_SECONDS = 120


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso(value: dt.datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds")


def run_cmd(args: list[str], cwd: Path, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def git_root(start: str | Path) -> Path:
    cwd = Path(start).expanduser().resolve()
    result = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        raise SystemExit(f"Not a Git repository : {cwd}\n{result.stderr.strip()}")
    return Path(result.stdout.strip()).resolve()


def clip(text: Any, limit: int) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    text = text.replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… (+{len(text) - limit} characters)"


NOISE_PROMPT_MARKERS = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<environment_context>",
    "<task-notification>",
)


def is_real_prompt(text: Any) -> bool:
    value = clip(text, 12000).strip()
    if not value:
        return False
    lowered = value.lower()
    if any(marker in lowered for marker in NOISE_PROMPT_MARKERS):
        return False
    if re.fullmatch(r"/?model(?:\s+.*)?", lowered):
        return False
    return True


def atomic_write(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def default_state() -> dict[str, Any]:
    return {
        "pid": None,
        "started_at": None,
        "updated_at": None,
        "interval": DEFAULT_INTERVAL,
        "last_git_signature": None,
        "git_events": [],
    }


def encode_state(state: dict[str, Any]) -> str:
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return STATE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii") + STATE_SUFFIX


def decode_state(text: str) -> dict[str, Any]:
    match = re.search(re.escape(STATE_PREFIX) + r"([^<]+)" + re.escape(STATE_SUFFIX), text)
    if not match:
        return default_state()
    try:
        return json.loads(base64.urlsafe_b64decode(match.group(1)).decode("utf-8"))
    except Exception:
        return default_state()


def load_state(root: Path) -> dict[str, Any]:
    try:
        return decode_state((root / OUTPUT_NAME).read_text(encoding="utf-8"))
    except OSError:
        return default_state()


def pid_alive(pid: Any) -> bool:
    try:
        pid_int = int(pid)
        if pid_int <= 0:
            return False
        os.kill(pid_int, 0)
        return True
    except (ValueError, TypeError, OSError):
        return False


def status_entries(root: Path) -> list[dict[str, Any]]:
    result = run_cmd(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    parts = result.stdout.split("\0")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if not record:
            continue
        code = record[:2]
        path_text = record[3:] if len(record) > 3 else ""
        old_path = None
        if "R" in code or "C" in code:
            old_path = path_text
            if index < len(parts):
                path_text = parts[index]
                index += 1
        if path_text == OUTPUT_NAME:
            continue
        path = root / path_text
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        entries.append({"code": code, "path": path_text, "old_path": old_path, "mtime": mtime})
    entries.sort(key=lambda item: item.get("mtime") or 0, reverse=True)
    return entries


def collect_git(root: Path) -> dict[str, Any]:
    branch = run_cmd(["git", "branch", "--show-current"], root).stdout.strip()
    head_line = run_cmd(["git", "log", "-1", "--pretty=%h%x09%s"], root).stdout.strip()
    head, _, subject = head_line.partition("\t")
    entries = status_entries(root)

    stat = run_cmd(["git", "diff", "HEAD", "--stat"], root).stdout.strip()
    commits_raw = run_cmd(
        [
            "git", "log", "--all",
            "--pretty=%h%x09%ad%x09%an%x09%s", "--date=iso-local",
            f"--max-count={MAX_COMMITS}",
        ],
        root,
        timeout=20,
    ).stdout
    commits: list[dict[str, Any]] = []
    for line in commits_raw.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        commit: dict[str, Any] = {
            "sha": parts[0], "at": parts[1], "author": parts[2], "subject": parts[3]
        }
        if len(commits) < MAX_COMMIT_FILE_DETAILS:
            commit["stat"] = run_cmd(
                ["git", "show", "--format=", "--shortstat", parts[0]], root, timeout=20
            ).stdout.strip()
            names = run_cmd(
                ["git", "show", "--format=", "--name-only", parts[0]], root, timeout=20
            ).stdout.splitlines()
            commit["files"] = [name.strip() for name in names if name.strip()][:MAX_FILES_PER_COMMIT]
        commits.append(commit)

    untracked_previews: list[dict[str, str]] = []
    for entry in entries:
        if entry["code"] != "??" or len(untracked_previews) >= 3:
            continue
        path = root / entry["path"]
        try:
            if path.is_file() and path.stat().st_size <= 200_000:
                content = path.read_text(encoding="utf-8", errors="replace")
                untracked_previews.append({"path": entry["path"], "content": clip(content, 5000)})
        except OSError:
            pass

    signature = json.dumps(
        {
            "head": head,
            "entries": [(e["code"], e["path"], int(e["mtime"] or 0)) for e in entries],
            "stat": stat,
        },
        sort_keys=True,
    )

    return {
        "branch": branch,
        "head": head,
        "head_subject": subject,
        "dirty": bool(entries),
        "entries": entries,
        "stat": stat,
        "commits_recent": commits,
        "untracked_previews": untracked_previews,
        "signature": signature,
    }


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def local_timestamp(value: Any) -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return str(value or "?")
    return parsed.astimezone().isoformat(sep=" ", timespec="seconds")


def file_timestamp(timestamp: float | None) -> str:
    if timestamp is None:
        return "unknown date"
    return dt.datetime.fromtimestamp(timestamp).astimezone().isoformat(sep=" ", timespec="seconds")


def duration_text(start: dt.datetime | None, end: dt.datetime | None) -> str:
    if start is None or end is None or end < start:
        return "unknown"
    total = int((end - start).total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def human_age(timestamp: float | None) -> str:
    if timestamp is None:
        return "unknown date"
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def detect_agents(root: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if os.name == "nt":
        # Without external dependencies, Windows may not expose the full command line.
        result = run_cmd(["tasklist", "/FO", "CSV", "/NH"], root)
        for line in result.stdout.splitlines():
            lower = line.lower()
            if "codex" in lower or "claude" in lower:
                source = "Codex" if "codex" in lower else "Claude"
                found.append({"source": source, "command": line[:500]})
        return found

    result = run_cmd(["ps", "-eo", "pid=,etimes=,args="], root)
    this_pid = os.getpid()
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == this_pid:
            continue
        command = parts[2]
        lower = command.lower()
        if "handoff_snapshot" in lower:
            continue
        source = None
        if re.search(r"(^|[/\\\s])codex(?:\s|$)", lower):
            source = "Codex"
        elif re.search(r"(^|[/\\\s])claude(?:\s|$)", lower):
            source = "Claude"
        if source:
            found.append({"source": source, "pid": pid, "age_seconds": int(parts[1]), "command": clip(command, 500)})
    return found


def read_head(path: Path, max_bytes: int = MAX_TRANSCRIPT_HEAD_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_tail(path: Path, max_bytes: int = MAX_TRANSCRIPT_TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # discard a partial line
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def candidate_transcripts() -> list[tuple[str, Path, float]]:
    """Return the newest local transcript files across Claude and Codex."""
    home = Path.home()
    roots: list[tuple[str, Path]] = [
        ("Claude", home / ".claude" / "projects"),
        ("Codex", home / ".codex" / "sessions"),
    ]
    candidates: list[tuple[str, Path, float]] = []
    for source, directory in roots:
        if not directory.exists():
            continue
        try:
            for path in directory.rglob("*.jsonl"):
                try:
                    candidates.append((source, path, path.stat().st_mtime))
                except OSError:
                    pass
        except OSError:
            pass
    candidates.sort(key=lambda item: item[2], reverse=True)
    return candidates[:MAX_TRANSCRIPT_FILES]


def repo_markers(root: Path) -> dict[str, list[str]]:
    """Build strong, exact-ish markers for this repository path.

    A repository basename alone is intentionally not accepted: two unrelated
    repositories can easily share the same folder name.
    """
    raw = str(root.resolve())
    slash = raw.replace("\\", "/")
    escaped = json.dumps(raw, ensure_ascii=False)[1:-1]
    encoded = re.sub(r"[^A-Za-z0-9]", "-", raw).strip("-")
    return {
        "content": [value.lower() for value in {raw, slash, escaped} if len(value) > 4],
        "path": [encoded.lower()] if len(encoded) > 4 else [],
    }


def transcript_match_score(root: Path, source: str, path: Path, tail: str) -> int:
    """Score whether a transcript belongs to the exact repository.

    Full repository-path markers are required. The folder name alone never
    qualifies a session.
    """
    markers = repo_markers(root)
    path_text = str(path).lower()
    tail_text = tail.lower()
    score = 0

    if any(marker in tail_text for marker in markers["content"]):
        score += 100
    if source == "Claude" and any(marker in path_text for marker in markers["path"]):
        score += 80

    return score


def text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
                    texts.append(item["text"])
        return "\n".join(texts)
    if isinstance(value, dict):
        for key in ("text", "message", "content", "prompt"):
            if key in value:
                result = text_from_content(value[key])
                if result:
                    return result
    return ""


def nested_get(obj: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = obj
        valid = True
        for key in path:
            if not isinstance(current, dict) or key not in current:
                valid = False
                break
            current = current[key]
        if valid:
            return current
    return None


def extract_event(source: str, obj: dict[str, Any], file_mtime: float, line_index: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    timestamp = nested_get(
        obj,
        ("timestamp",), ("created_at",), ("time",),
        ("payload", "timestamp"), ("message", "timestamp"),
    )
    at = str(timestamp) if timestamp else dt.datetime.fromtimestamp(file_mtime, tz=dt.timezone.utc).astimezone().isoformat(timespec="seconds")

    obj_type = str(obj.get("type") or "").lower()
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "").lower()
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    role = str(message.get("role") or obj.get("role") or payload.get("role") or "").lower()

    # Common Claude message records. Task notifications are generated results,
    # not real user prompts; preserve them as observed actions instead.
    if obj_type in {"user", "assistant"} or role in {"user", "assistant"}:
        content = text_from_content(message.get("content") if message else obj.get("content"))
        if content:
            is_user_record = obj_type == "user" or role == "user"
            if is_user_record and "<task-notification>" in content.lower():
                events.append({
                    "kind": "action",
                    "source": source,
                    "at": at,
                    "tool": "task-notification",
                    "text": clip(content, 1800),
                    "order": line_index,
                })
            else:
                events.append({
                    "kind": "prompt" if is_user_record else "message",
                    "source": source,
                    "at": at,
                    "text": clip(content, 8000),
                    "order": line_index,
                })

    # Common Codex rollout records and generic variants.
    if payload_type in {"user_message", "input_text"}:
        content = text_from_content(payload.get("message") or payload.get("text") or payload.get("content"))
        if content:
            events.append({"kind": "prompt", "source": source, "at": at, "text": clip(content, 8000), "order": line_index})
    elif payload_type in {"agent_message", "assistant_message", "output_text"}:
        content = text_from_content(payload.get("message") or payload.get("text") or payload.get("content"))
        if content:
            events.append({"kind": "message", "source": source, "at": at, "text": clip(content, 8000), "order": line_index})

    # Claude tool calls inside message.content.
    content_items = message.get("content") if isinstance(message.get("content"), list) else []
    for item in content_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use":
            name = str(item.get("name") or "tool")
            tool_input = item.get("input")
            events.append({"kind": "action", "source": source, "at": at, "tool": name, "text": clip(tool_input, 1800), "order": line_index})

    # Codex and generic tool calls.
    toolish = {"function_call", "custom_tool_call", "tool_call", "computer_call", "command_execution"}
    if obj_type in toolish or payload_type in toolish:
        body = payload if payload else obj
        name = str(body.get("name") or body.get("tool") or body.get("command") or obj_type or payload_type)
        arguments = body.get("arguments") or body.get("input") or body.get("command") or body.get("args")
        events.append({"kind": "action", "source": source, "at": at, "tool": name, "text": clip(arguments, 1800), "order": line_index})

    # Additional heuristic for apply_patch / shell_command records embedded in JSON.
    if not any(event["kind"] == "action" for event in events):
        serialized = json.dumps(obj, ensure_ascii=False, default=str)
        if any(token in serialized for token in ('"apply_patch"', '"shell_command"', '"exec_command"', '"Edit"', '"Write"')):
            name_match = re.search(r'"(?:name|tool)"\s*:\s*"([^"]+)"', serialized)
            name = name_match.group(1) if name_match else "tool"
            command = nested_get(obj, ("input",), ("tool_input",), ("payload", "arguments"), ("payload", "command"))
            events.append({"kind": "action", "source": source, "at": at, "tool": name, "text": clip(command or serialized, 1800), "order": line_index})

    return events


def is_substantive_message(event: dict[str, Any]) -> bool:
    text = clip(event.get("text"), 12000).strip()
    if len(text) < 80:
        return False
    lowered = text.lower()
    if "you've hit your monthly spend limit" in lowered:
        return False
    return True


def evenly_distributed(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[-1]]
    indexes = [round(index * (len(items) - 1) / (limit - 1)) for index in range(limit)]
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index in indexes:
        if index not in seen:
            selected.append(items[index])
            seen.add(index)
    return selected


def meaningful_actions(actions: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Collapse passive transcript noise without interpreting task semantics."""
    prepared: list[dict[str, Any]] = []
    wait_group: dict[str, Any] | None = None

    def flush_wait() -> None:
        nonlocal wait_group
        if wait_group and wait_group["count"] >= 2:
            prepared.append({
                "kind": "action",
                "source": wait_group["source"],
                "at": wait_group["end"],
                "tool": "wait-group",
                "text": (
                    f"Waited/polled {wait_group['count']} times between "
                    f"{local_timestamp(wait_group['start'])} and {local_timestamp(wait_group['end'])}."
                ),
            })
        wait_group = None

    for action in actions:
        tool = str(action.get("tool") or "").lower()
        if tool in {"wait", "sleep"}:
            if wait_group and wait_group["source"] == action.get("source"):
                wait_group["count"] += 1
                wait_group["end"] = action.get("at")
            else:
                flush_wait()
                wait_group = {
                    "source": action.get("source", "agent"),
                    "start": action.get("at"),
                    "end": action.get("at"),
                    "count": 1,
                }
            continue

        flush_wait()
        normalized = re.sub(r"\s+", " ", clip(action.get("text"), 4000)).strip()
        if prepared:
            previous = prepared[-1]
            previous_key = (
                previous.get("source"), previous.get("tool"),
                re.sub(r"\s+", " ", clip(previous.get("text"), 4000)).strip(),
            )
            current_key = (action.get("source"), action.get("tool"), normalized)
            if current_key == previous_key:
                continue
        prepared.append(action)

    flush_wait()
    return prepared[-limit:]


def parse_transcripts(root: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []

    for source, path, mtime in candidate_transcripts():
        head = read_head(path)
        tail = read_tail(path)
        if not head and not tail:
            continue
        match_sample = head + "\n" + tail[:300_000]
        score = transcript_match_score(root, source, path, match_sample)
        if score <= 0:
            continue
        matched.append({
            "source": source,
            "path": str(path),
            "mtime": mtime,
            "score": score,
            "tail": tail,
        })

    matched.sort(key=lambda item: (item["mtime"], item["score"]), reverse=True)
    matched = matched[:MAX_MATCHED_TRANSCRIPTS]
    primary_path = matched[0]["path"] if matched else None

    for item in matched:
        source = item["source"]
        path_text = item["path"]
        mtime = item["mtime"]
        for index, line in enumerate(item["tail"].splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                extracted = extract_event(source, obj, mtime, index)
                for event in extracted:
                    event["session_path"] = path_text
                    event["session_mtime"] = mtime
                events.extend(extracted)

    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        key = json.dumps(
            {k: event.get(k) for k in ("kind", "source", "at", "tool", "text", "session_path")},
            ensure_ascii=False,
            sort_keys=True,
        )
        unique[key] = event
    events = list(unique.values())
    events.sort(
        key=lambda item: (
            parse_timestamp(item.get("at"))
            or dt.datetime.fromtimestamp(item.get("session_mtime") or 0, tz=dt.timezone.utc),
            str(item.get("session_path") or ""),
            item.get("order") or 0,
        )
    )

    actions_all = [event for event in events if event["kind"] == "action"]
    actions = meaningful_actions(actions_all, MAX_ACTIONS)
    messages_all = [event for event in events if event["kind"] == "message"]

    primary_events = [event for event in events if event.get("session_path") == primary_path]
    primary_events.sort(key=lambda item: item.get("order") or 0)
    primary_prompts = [
        event for event in primary_events
        if event["kind"] == "prompt" and is_real_prompt(event.get("text"))
    ]
    primary_messages = [event for event in primary_events if event["kind"] == "message"]
    primary_actions = [event for event in primary_events if event["kind"] == "action"]

    exchanges: list[dict[str, Any]] = []
    by_session: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_session.setdefault(str(event.get("session_path") or ""), []).append(event)
    for session_path, session_events in by_session.items():
        session_events.sort(key=lambda item: item.get("order") or 0)
        current_prompt: dict[str, Any] | None = None
        current_messages: list[dict[str, Any]] = []

        def flush_exchange() -> None:
            nonlocal current_prompt, current_messages
            if current_prompt and current_messages:
                exchanges.append({
                    "source": current_prompt.get("source"),
                    "session_path": session_path,
                    "prompt": current_prompt,
                    "messages": list(current_messages),
                    "at": current_messages[-1].get("at") or current_prompt.get("at"),
                    "session_mtime": current_prompt.get("session_mtime") or 0,
                })
            current_prompt = None
            current_messages = []

        for event in session_events:
            if event["kind"] == "prompt" and is_real_prompt(event.get("text")):
                flush_exchange()
                current_prompt = event
            elif event["kind"] == "message" and current_prompt is not None:
                current_messages.append(event)
        flush_exchange()

    exchanges.sort(
        key=lambda item: (
            parse_timestamp(item.get("at"))
            or dt.datetime.fromtimestamp(item.get("session_mtime") or 0, tz=dt.timezone.utc)
        )
    )
    primary_exchanges = [item for item in exchanges if item.get("session_path") == primary_path]
    recent_exchanges = (primary_exchanges or exchanges)[-MAX_EXCHANGES:]

    unanswered_prompt = None
    if primary_prompts:
        latest_prompt = primary_prompts[-1]
        prompt_order = latest_prompt.get("order") or 0
        if not any((message.get("order") or 0) > prompt_order for message in primary_messages):
            unanswered_prompt = latest_prompt

    cutoff_order = None
    if primary_exchanges and recent_exchanges:
        orders = [
            (exchange.get("prompt") or {}).get("order")
            for exchange in recent_exchanges
            if (exchange.get("prompt") or {}).get("order") is not None
        ]
        if orders:
            cutoff_order = min(orders)
    earlier_messages = [
        message for message in primary_messages
        if is_substantive_message(message)
        and (cutoff_order is None or (message.get("order") or 0) < cutoff_order)
    ]
    timeline = evenly_distributed(earlier_messages, MAX_TIMELINE_MILESTONES)

    primary_times = [parse_timestamp(event.get("at")) for event in primary_events]
    primary_times = [value for value in primary_times if value is not None]
    coverage = {
        "start": min(primary_times).isoformat() if primary_times else None,
        "end": max(primary_times).isoformat() if primary_times else None,
        "prompts": len(primary_prompts),
        "messages": len(primary_messages),
        "actions": len(primary_actions),
        "exchanges": len(primary_exchanges),
    }

    matched_files = [
        {"source": item["source"], "path": item["path"], "mtime": item["mtime"], "score": item["score"]}
        for item in matched
    ]
    return {
        "actions": actions,
        "exchanges": recent_exchanges,
        "timeline": timeline,
        "unanswered_prompt": unanswered_prompt,
        "latest_prompt": primary_prompts[-1] if primary_prompts else None,
        "latest_message": primary_messages[-1] if primary_messages else (messages_all[-1] if messages_all else None),
        "primary_messages": primary_messages,
        "coverage": coverage,
        "primary_session": matched_files[0] if matched_files else None,
        "matched_files": matched_files,
    }


def extract_planned_next(messages: list[dict[str, Any]]) -> str | None:
    patterns = [
        r"(?i)(?:^|[.!?]\s+)(?:prochaine étape|prochaine etape|next step|resume point|reprise exacte|à faire maintenant|a faire maintenant)\s*[:\-]\s*(.+)$",
        r"(?i)^(?:je vais maintenant|je vais ensuite)\s+(.+)$",
    ]
    for message in reversed(messages):
        text = message.get("text") or ""
        for line in reversed(text.splitlines() or [text]):
            candidate = line.strip().lstrip("-* ")
            for pattern in patterns:
                match = re.search(pattern, candidate)
                if match:
                    return clip(match.group(1).strip(), 700).replace("\n", " ")
    return None


def git_events_from_change(state: dict[str, Any], git: dict[str, Any]) -> list[dict[str, Any]]:
    events = state.get("git_events") if isinstance(state.get("git_events"), list) else []
    signature = git.get("signature")
    if signature != state.get("last_git_signature"):
        latest_files = [entry["path"] for entry in git.get("entries", [])[:3]]
        if git.get("dirty"):
            summary = "Git state changed"
            if latest_files:
                summary += " : " + ", ".join(latest_files)
        else:
            summary = "Working tree is clean again"
        events.append({"kind": "git", "source": "Watcher", "at": iso(), "tool": "git", "text": summary})
        events = events[-MAX_ACTIONS:]
        state["last_git_signature"] = signature
    state["git_events"] = events
    return events


def diagnostic(git: dict[str, Any], agents: list[dict[str, Any]], transcript: dict[str, Any], stale_seconds: int) -> str:
    if agents and git.get("dirty"):
        return "STEP IN PROGRESS — active agent with uncommitted changes"
    if agents:
        return "AGENT ACTIVE — no uncommitted Git changes detected"
    if git.get("dirty"):
        newest = max((entry.get("mtime") or 0 for entry in git.get("entries", [])), default=0)
        age = time.time() - newest if newest else 999999
        if age <= stale_seconds:
            return "POSSIBLE INTERRUPTION — recent uncommitted changes and no active agent detected"
        return "LIKELY INCOMPLETE STEP — uncommitted changes remain on disk"
    if transcript.get("latest_prompt"):
        return "NO ACTIVE AGENT — latest known task is available in local transcripts"
    return "NO ACTIVE STEP DETECTED"


def render(root: Path, state: dict[str, Any], git: dict[str, Any], agents: list[dict[str, Any]], transcript: dict[str, Any], stale_seconds: int) -> str:
    state["updated_at"] = iso()
    next_step = extract_planned_next(transcript.get("primary_messages", [])[-12:])

    combined_actions = list(transcript.get("actions", [])) + list(state.get("git_events", []))
    combined_actions.sort(key=lambda item: parse_timestamp(item.get("at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    combined_actions = meaningful_actions(combined_actions, MAX_ACTIONS)

    process_counts: dict[str, int] = {}
    for agent in agents:
        source = agent.get("source", "Other")
        process_counts[source] = process_counts.get(source, 0) + 1
    process_summary = ", ".join(f"{name} {count}" for name, count in sorted(process_counts.items())) or "none"

    lines = [
        "# Live handoff — passive snapshot",
        "",
        "> Generated without hooks, model calls, or changes to Claude Code/Codex CLI configuration.",
        "> Git and existing local session files are read passively; Git remains the source of truth.",
        "",
        f"- **Updated:** {state['updated_at']}",
        f"- **Generator:** handoff_snapshot.py `{SCRIPT_VERSION}` — `{Path(__file__).resolve()}`",
        f"- **Repository:** `{root}`",
        f"- **Status:** {diagnostic(git, agents, transcript, stale_seconds)}",
        f"- **OS agent processes present:** {process_summary}",
        "",
        "## Immediate resume context",
        "",
    ]

    unanswered = transcript.get("unanswered_prompt")
    latest = transcript.get("latest_prompt")
    if unanswered:
        lines.extend([
            f"**Latest unanswered instruction ({unanswered.get('source', 'agent')} — {local_timestamp(unanswered.get('at'))}):**",
            "",
            clip(unanswered.get("text"), 2500),
            "",
        ])
    elif latest:
        lines.extend([
            f"**Latest instruction is answered** ({latest.get('source', 'agent')} — {local_timestamp(latest.get('at'))}).",
            "",
        ])
    else:
        lines.extend(["**No user instruction recovered from the primary session.**", ""])

    if next_step:
        lines.extend([f"**Explicit next step recovered:** {next_step}", ""])
    else:
        lines.extend(["**Explicit next step:** none recovered from recent agent messages.", ""])

    lines.extend([
        "Recommended resume flow: read this file, then run `git status --short`, `git diff --stat`, and `git diff` before editing anything.",
        "",
        "## Recovered session coverage",
        "",
    ])

    coverage = transcript.get("coverage", {})
    start = parse_timestamp(coverage.get("start"))
    end = parse_timestamp(coverage.get("end"))
    primary = transcript.get("primary_session")
    matched = transcript.get("matched_files", [])
    claude_main = sum(1 for item in matched if item.get("source") == "Claude" and "subagents" not in item.get("path", "").lower())
    claude_sub = sum(1 for item in matched if item.get("source") == "Claude" and "subagents" in item.get("path", "").lower())
    codex_sessions = sum(1 for item in matched if item.get("source") == "Codex")
    lines.extend([
        f"- **Primary matching session:** {primary.get('source') if primary else 'none'}",
        f"- **Recovered activity:** {local_timestamp(coverage.get('start'))} → {local_timestamp(coverage.get('end'))}",
        f"- **Duration covered:** {duration_text(start, end)}",
        f"- **Primary-session records:** {coverage.get('prompts', 0)} prompts, {coverage.get('messages', 0)} agent messages, {coverage.get('actions', 0)} tool actions, {coverage.get('exchanges', 0)} complete exchanges",
        f"- **Matching transcript files:** Codex {codex_sessions}, Claude main {claude_main}, Claude subagents {claude_sub}",
        "",
    ])

    exchanges = transcript.get("exchanges", [])
    lines.extend([f"## {len(exchanges)} latest complete exchanges", ""])
    for index, exchange in enumerate(reversed(exchanges), 1):
        prompt = exchange.get("prompt") or {}
        answer_parts = [clip(message.get("text"), 2200) for message in exchange.get("messages", [])]
        answer = "\n\n".join(part for part in answer_parts if part)
        lines.extend([
            f"### Exchange {index} — {exchange.get('source', 'agent')} — {local_timestamp(exchange.get('at'))}",
            "",
            "**User / instruction:**",
            "",
            clip(prompt.get("text"), 1800),
            "",
            "**Agent:**",
            "",
            clip(answer, 5000) if answer else "No agent response recovered.",
            "",
        ])
    if not exchanges:
        lines.append("No complete prompt/response exchange recovered.")

    timeline = transcript.get("timeline", [])
    lines.extend(["", f"## Earlier primary-session timeline — {len(timeline)} milestones before the latest exchanges", ""])
    for index, milestone in enumerate(timeline, 1):
        lines.extend([
            f"### Milestone {index} — {milestone.get('source', 'agent')} — {local_timestamp(milestone.get('at'))}",
            "",
            clip(milestone.get("text"), 2200),
            "",
        ])
    if not timeline:
        lines.append("No earlier substantive agent milestones recovered.")

    lines.extend(["", f"## {len(combined_actions)} latest meaningful observed actions (limit {MAX_ACTIONS})", ""])
    for index, action in enumerate(reversed(combined_actions), 1):
        text = clip(action.get("text"), 700).replace("\n", " ")
        tool = action.get("tool") or action.get("kind") or "action"
        lines.append(f"{index}. `{local_timestamp(action.get('at'))}` — **{action.get('source', 'agent')} / {tool}** — {text}")
    if not combined_actions:
        lines.append("No action found.")

    entries = git.get("entries", [])
    shown_entries = entries[:MAX_FILES]
    lines.extend([
        "",
        "## Current Git worktree",
        "",
        f"- **Branch:** `{git.get('branch') or '?'}`",
        f"- **HEAD:** `{git.get('head') or '?'}` — {git.get('head_subject') or ''}",
        f"- **Uncommitted changes:** {'yes' if git.get('dirty') else 'no'}",
        f"- **Worktree entries:** {len(entries)} total; showing {len(shown_entries)} (limit {MAX_FILES})",
        "",
        f"### Current modified or untracked files — {len(shown_entries)} shown",
        "",
    ])
    for entry in shown_entries:
        old = f" from `{entry['old_path']}`" if entry.get("old_path") else ""
        lines.append(
            f"- `{entry.get('code', '??')}` `{entry.get('path')}`{old} — "
            f"{file_timestamp(entry.get('mtime'))} ({human_age(entry.get('mtime'))})"
        )
    if len(entries) > MAX_FILES:
        lines.append(f"- … {len(entries) - MAX_FILES} additional worktree entries omitted.")
    if not entries:
        lines.append("No modified files.")

    if git.get("stat"):
        lines.extend(["", "### Diff summary", "", "```text", git["stat"], "```"])
    for preview in git.get("untracked_previews", []):
        lines.extend(["", f"### New untracked file: `{preview['path']}`", "", "```text", preview["content"], "```"])

    commits = git.get("commits_recent", [])
    lines.extend(["", f"## Last {len(commits)} local commits (limit {MAX_COMMITS})", ""])
    for index, commit in enumerate(commits, 1):
        lines.append(f"### {index}. `{commit['sha']}` — {commit['at']} — {commit['subject']} — _{commit['author']}_")
        if commit.get("stat"):
            lines.append(f"- {commit['stat']}")
        if commit.get("files"):
            lines.append("- Files:")
            for name in commit["files"]:
                lines.append(f"  - `{name}`")
        lines.append("")
    if not commits:
        lines.append("No local commit found.")

    lines.extend(["", "## Agent/session activity", ""])
    if primary:
        lines.append(f"- **Primary matching transcript:** {primary['source']} — `{primary['path']}`")
        lines.append(f"- **Primary transcript file modified:** {file_timestamp(primary.get('mtime'))}")
    else:
        lines.append("- **Primary matching transcript:** none")
    lines.append(f"- **Last recovered primary-session event:** {local_timestamp(coverage.get('end'))}")
    lines.append(f"- **Matching transcripts:** Codex {codex_sessions}, Claude main {claude_main}, Claude subagents {claude_sub}")
    lines.append(f"- **OS processes present:** {process_summary}")
    lines.append("- Process presence alone does not prove that a process is actively working on this repository; transcript recency is the stronger signal.")

    lines.extend(["", "## Passive sources inspected", ""])
    if matched:
        for item in matched:
            role = "Claude subagent" if item.get("source") == "Claude" and "subagents" in item.get("path", "").lower() else item.get("source")
            lines.append(f"- {role}: `{item['path']}` — file modified {file_timestamp(item.get('mtime'))}")
    else:
        lines.append("No transcript matching this repository was identified.")

    lines.extend(["", encode_state(state), ""])
    return "\n".join(lines)


def snapshot_once(root: Path, stale_seconds: int) -> int:
    """Generate one handoff snapshot without leaving a background process."""
    state = load_state(root)
    state["pid"] = None
    state["started_at"] = state.get("started_at") or iso()
    state["interval"] = None

    git = collect_git(root)
    agents = detect_agents(root)
    transcript = parse_transcripts(root)
    git_events_from_change(state, git)
    state["pid"] = None
    state["updated_at"] = iso()
    atomic_write(root / OUTPUT_NAME, render(root, state, git, agents, transcript, stale_seconds))

    print(f"Handoff snapshot written: {root / OUTPUT_NAME}")
    print(f"Generator: {SCRIPT_VERSION} — {Path(__file__).resolve()}")
    print("No background process remains active, and no Claude/Codex hook or configuration was modified.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a passive Claude Code ↔ Codex CLI handoff snapshot."
    )
    parser.add_argument("--repo", default=".", help="Path to the Git repository")
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=DEFAULT_STALE_SECONDS,
        help="How recent a file modification must be to be marked as a possible interruption",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = git_root(args.repo)
    return snapshot_once(root, max(30, args.stale_seconds))


if __name__ == "__main__":
    raise SystemExit(main())