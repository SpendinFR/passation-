# Passation

A passive, on-demand handoff snapshot between **Claude Code** and **Codex CLI**.

When one coding agent stops unexpectedly because of a quota limit, crash, terminal closure, or interrupted step, run this script inside the same Git repository. It creates a single `passationlive.md` file that the other agent can read before continuing.

The script does **not** install hooks, modify Claude/Codex configuration, or run permanently in the background.

## What it captures

- the 4 latest recovered prompts/tasks;
- the 10 latest recovered tool actions;
- the latest recovered agent message and explicit next step;
- the 10 most recently modified files;
- the current uncommitted Git diff;
- previews of small untracked files;
- all local commits made today;
- whether Claude Code or Codex CLI still appears to be running.

Git is treated as the source of truth. Local Claude and Codex session files are read on a best-effort basis to reconstruct the interrupted task.

## Requirements

- Python 3.10 or newer;
- Git;
- Claude Code and/or Codex CLI using their normal local session storage.

No Python package needs to be installed.

## Download

```bash
curl -L -o handoff_snapshot.py \
  https://raw.githubusercontent.com/SpendinFR/passation-/main/handoff_snapshot.py
```

Or download `handoff_snapshot.py` directly from this repository.

## Usage

Immediately after Claude Code or Codex CLI stops, run this from the affected repository:

```bash
python3 /path/to/handoff_snapshot.py --repo .
```

Windows PowerShell:

```powershell
py C:\path\to\handoff_snapshot.py --repo .
```

The command creates or replaces:

```text
passationlive.md
```

It exits immediately. No daemon or watcher remains active.

To keep the generated handoff local without changing the project's shared `.gitignore`:

```bash
echo passationlive.md >> .git/info/exclude
```

PowerShell:

```powershell
Add-Content .git/info/exclude "passationlive.md"
```

## Continue with the other agent

Open the other agent in the **same repository, working tree, and branch**, then use a prompt such as:

```text
Read passationlive.md, then verify git status and git diff.
Identify the interrupted step and any partial file changes.
Continue from the existing implementation without repeating completed work.
Run the relevant tests and commit only when the step is complete.
```

This works in both directions:

```text
Claude Code interrupted -> generate snapshot -> Codex CLI continues
Codex CLI interrupted   -> generate snapshot -> Claude Code continues
```

Do not run both agents simultaneously against the same working tree.

## What it does not recover

The script cannot recover:

- text that was never saved to disk;
- hidden model reasoning;
- an intention that was never written to a transcript, task, command, or file;
- the final seconds of a session if the CLI never flushed them to local storage.

It can still recover partial saved edits through Git and can often reconstruct the next step from the latest prompt, tool call, and agent message.

## Privacy and safety

`passationlive.md` can contain excerpts from prompts, commands, diffs, and local source files. Review it before sharing or committing it. The script does not upload anything and does not make network requests.

Session storage formats are internal implementation details of Claude Code and Codex CLI and may change. Transcript extraction is therefore best effort; Git capture remains reliable as long as changes were saved to disk.

## Test

```bash
python3 tests/test_snapshot.py
```

The test covers both handoff directions with simulated local transcripts, a partially modified tracked file, an unfinished untracked file, and commits from the current day.

## License

MIT
