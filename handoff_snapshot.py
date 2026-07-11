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
STATE_PREFIX = "<!-- PASSATION_PASSIVE_STATE_V1:"
STATE_SUFFIX = "-->"
MAX_PROMPTS = 4
MAX_ACTIONS = 10
MAX_FILES = 10
MAX_COMMITS = 100
MAX_DIFF_CHARS = 30000
MAX_TRANSCRIPT_TAIL_BYTES = 3_000_000
MAX_TRANSCRIPT_FILES = 24
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
    diff = run_cmd(["git", "diff", "HEAD", "--no-ext-diff", "--unified=3"], root, timeout=30).stdout

    # All commits from today across visible local refs.
    commits_raw = run_cmd(
        [
            "git", "log", "--all", "--since=midnight",
            "--pretty=%h%x09%ad%x09%an%x09%s", "--date=iso-local",
            f"--max-count={MAX_COMMITS}",
        ],
        root,
        timeout=20,
    ).stdout
    commits: list[dict[str, str]] = []
    for line in commits_raw.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            commits.append({"sha": parts[0], "at": parts[1], "author": parts[2], "subject": parts[3]})

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
        "diff": clip(diff, MAX_DIFF_CHARS),
        "commits_today": commits,
        "untracked_previews": untracked_previews,
        "signature": signature,
    }


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


def candidate_transcripts() -> list[tuple[str, Path]]:
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
    return [(source, path) for source, path, _ in candidates[:MAX_TRANSCRIPT_FILES]]


def repo_markers(root: Path) -> list[str]:
    values = {
        str(root),
        str(root).replace("\\", "/"),
        root.name,
    }
    # Claude often encodes the repository path in the project directory name.
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(root)).strip("-")
    if encoded:
        values.add(encoded)
    return [value.lower() for value in values if value]


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

    # Common Claude message records.
    if obj_type in {"user", "assistant"} or role in {"user", "assistant"}:
        content = text_from_content(message.get("content") if message else obj.get("content"))
        if content:
            events.append({"kind": "prompt" if (obj_type == "user" or role == "user") else "message", "source": source, "at": at, "text": clip(content, 8000), "order": line_index})

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


def parse_transcripts(root: Path) -> dict[str, Any]:
    markers = repo_markers(root)
    events: list[dict[str, Any]] = []
    matched_files: list[dict[str, Any]] = []

    for source, path in candidate_transcripts():
        tail = read_tail(path)
        if not tail:
            continue
        haystack = (str(path) + "\n" + tail[:300_000]).lower()
        # A repository name alone is too weak; require a path-like marker or workspace marker.
        strong_markers = [marker for marker in markers if len(marker) > 4]
        if not any(marker in haystack for marker in strong_markers):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        matched_files.append({"source": source, "path": str(path), "mtime": mtime})
        for index, line in enumerate(tail.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.extend(extract_event(source, obj, mtime, index))

    # Stable deduplication.
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        key = json.dumps({k: event.get(k) for k in ("kind", "source", "at", "tool", "text")}, ensure_ascii=False, sort_keys=True)
        unique[key] = event
    events = list(unique.values())
    events.sort(key=lambda item: (item.get("at") or "", item.get("order") or 0))

    prompts = [event for event in events if event["kind"] == "prompt"][-MAX_PROMPTS:]
    actions = [event for event in events if event["kind"] == "action"][-MAX_ACTIONS:]
    messages = [event for event in events if event["kind"] == "message"][-3:]

    matched_files.sort(key=lambda item: item["mtime"], reverse=True)
    return {
        "prompts": prompts,
        "actions": actions,
        "messages": messages,
        "matched_files": matched_files[:4],
    }


def extract_planned_next(messages: list[dict[str, Any]]) -> tuple[str, bool]:
    if not messages:
        return "Review the latest task, the most recently modified file, and the diff before continuing.", True

    # Search from newest to oldest. Capture to end-of-line because filenames such as test_app.py contain periods.
    patterns = [
        r"(?i)(?:^|\b)(?:prochaine étape|next step)\s*[:\-]\s*(.+)$",
        r"(?i)(?:^|\b)(?:je vais maintenant|je vais ensuite)\s+(.+)$",
        r"(?i)(?:^|\b)(?:ensuite|puis)\s*[:,\-]?\s*(.+)$",
    ]
    for message in reversed(messages):
        text = message.get("text") or ""
        for line in reversed(text.splitlines() or [text]):
            candidate = line.strip().lstrip("-* ")
            for pattern in patterns:
                match = re.search(pattern, candidate)
                if match:
                    result = match.group(1).strip().rstrip()
                    return clip(result, 700).replace("\n", " "), False

    return "Review the latest task, the most recently modified file, and the diff; finish the uncommitted step before starting another one.", True


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
    if transcript.get("prompts"):
        return "NO ACTIVE AGENT — latest known task is available in local transcripts"
    return "NO ACTIVE STEP DETECTED"


def render(root: Path, state: dict[str, Any], git: dict[str, Any], agents: list[dict[str, Any]], transcript: dict[str, Any], stale_seconds: int) -> str:
    state["updated_at"] = iso()
    next_step, inferred = extract_planned_next(transcript.get("messages", []))

    combined_actions = list(transcript.get("actions", [])) + list(state.get("git_events", []))
    combined_actions.sort(key=lambda item: item.get("at") or "")
    combined_actions = combined_actions[-MAX_ACTIONS:]

    lines = [
        "# Live handoff — passive snapshot",
        "",
        "> Generated without hooks and without modifying Claude Code or Codex CLI configuration.",
        "> Git and local session files are read passively; Git remains the source of truth.",
        "",
        f"- **Updated:** {state['updated_at']}",
        f"- **Repository:** `{root}`",
        f"- **Status:** {diagnostic(git, agents, transcript, stale_seconds)}",
        f"- **Detected agents:** {', '.join(agent['source'] for agent in agents) if agents else 'none'}",
        "",
        "## Immediate resume context",
        "",
    ]

    prompts = transcript.get("prompts", [])
    if prompts:
        latest = prompts[-1]
        lines.extend([
            f"**Latest captured task ({latest.get('source', 'agent')}):**",
            "",
            clip(latest.get("text"), 2500),
            "",
        ])
    else:
        lines.extend([
            "**Latest captured task:** not found in local session files.",
            "",
            "The next agent must rely on Git and your latest manual prompt.",
            "",
        ])

    label = "Inferred next step" if inferred else "Next step found in the latest agent message"
    lines.extend([
        f"**{label}:** {next_step}",
        "",
        "Recommended resume flow: read this file, then run `git status --short`, `git diff --stat`, and `git diff` before editing anything.",
        "",
        f"## {MAX_PROMPTS} latest tasks/prompts",
        "",
    ])

    for index, prompt in enumerate(reversed(prompts), 1):
        text = clip(prompt.get("text"), 900).replace("\n", " ")
        lines.append(f"{index}. **{prompt.get('source', 'agent')} — {prompt.get('at', '?')}** — {text}")
    if not prompts:
        lines.append("No task found.")

    lines.extend(["", f"## {MAX_ACTIONS} latest observed actions", ""])
    for index, action in enumerate(reversed(combined_actions), 1):
        text = clip(action.get("text"), 700).replace("\n", " ")
        tool = action.get("tool") or action.get("kind") or "action"
        lines.append(f"{index}. `{action.get('at', '?')}` — **{action.get('source', 'agent')} / {tool}** — {text}")
    if not combined_actions:
        lines.append("No action found.")

    lines.extend([
        "",
        "## Live Git state",
        "",
        f"- **Branch:** `{git.get('branch') or '?'}`",
        f"- **HEAD:** `{git.get('head') or '?'}` — {git.get('head_subject') or ''}",
        f"- **Uncommitted changes:** {'yes' if git.get('dirty') else 'no'}",
        "",
        f"### {MAX_FILES} most recently modified files",
        "",
    ])
    entries = git.get("entries", [])
    for entry in entries[:MAX_FILES]:
        old = f" from `{entry['old_path']}`" if entry.get("old_path") else ""
        lines.append(f"- `{entry.get('code', '??')}` `{entry.get('path')}`{old} — {human_age(entry.get('mtime'))}")
    if not entries:
        lines.append("No modified files.")

    if git.get("stat"):
        lines.extend(["", "### Diff summary", "", "```text", git["stat"], "```"])
    if git.get("diff"):
        lines.extend(["", "### Current diff", "", "```diff", git["diff"], "```"])

    for preview in git.get("untracked_previews", []):
        lines.extend(["", f"### New untracked file: `{preview['path']}`", "", "```text", preview["content"], "```"])

    lines.extend(["", "## All commits from today", ""])
    commits = git.get("commits_today", [])
    for commit in commits:
        lines.append(f"- `{commit['sha']}` — {commit['at']} — {commit['subject']} — _{commit['author']}_")
    if not commits:
        lines.append("No commits today.")

    messages = transcript.get("messages", [])
    lines.extend(["", "## Latest recovered agent message", ""])
    if messages:
        last = messages[-1]
        lines.extend([f"**{last.get('source', 'agent')} — {last.get('at', '?')}**", "", clip(last.get("text"), 5000)])
    else:
        lines.append("No agent message found in recent local session files.")

    lines.extend(["", "## Passive sources inspected", ""])
    matched = transcript.get("matched_files", [])
    if matched:
        for item in matched:
            lines.append(f"- {item['source']}: `{item['path']}`")
    else:
        lines.append("No transcript matching this repository was identified.")

    if agents:
        lines.extend(["", "## Detected agent processes", ""])
        for agent in agents:
            lines.append(f"- **{agent['source']}** — `{agent.get('command', '')}`")

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
