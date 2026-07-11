#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'handoff_snapshot.py'

def run(cmd, cwd, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=True)

def git(repo, *args):
    return run(['git', *args], repo)

def setup_repo(base: Path, name: str) -> Path:
    repo = base / name
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'Test User')
    (repo / 'app.py').write_text('def value():\n    return 1\n')
    git(repo, 'add', 'app.py')
    git(repo, 'commit', '-m', 'initial implementation')
    return repo

def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r) for r in records) + '\n')

def assert_common(out: str, repo: Path):
    assert 'app.py' in out
    assert 'return 2' in out
    assert 'partial_test.py' in out
    assert 'initial implementation' in out
    assert 'Uncommitted changes:** yes' in out
    assert not (repo / '.claude').exists()
    assert not (repo / '.codex').exists()

def test_claude_to_codex(base: Path, home: Path):
    repo = setup_repo(base, 'claude-project')
    (repo / 'app.py').write_text('def value():\n    return 2\n')
    (repo / 'partial_test.py').write_text('def test_value():\n    assert value() == ')
    records = [
        {'type':'user','timestamp':'2026-07-11T10:00:00+02:00','cwd':str(repo),'message':{'role':'user','content':[{'type':'text','text':'Fix value() and add a regression test without changing the public API.'}]}},
        {'type':'assistant','timestamp':'2026-07-11T10:00:05+02:00','cwd':str(repo),'message':{'role':'assistant','content':[{'type':'tool_use','name':'Edit','input':{'file_path':str(repo/'app.py'),'old_string':'return 1','new_string':'return 2'}}]}},
        {'type':'assistant','timestamp':'2026-07-11T10:00:08+02:00','cwd':str(repo),'message':{'role':'assistant','content':[{'type':'text','text':'The implementation is changed. Next step: finish partial_test.py and run the focused test.'}]}}
    ]
    write_jsonl(home/'.claude/projects'/f'-{repo.name}'/'session.jsonl', records)
    env={**os.environ,'HOME':str(home)}
    run([sys.executable,str(SCRIPT),'--repo',str(repo)], repo, env)
    out=(repo/'passationlive.md').read_text()
    assert 'Fix value() and add a regression test' in out
    assert 'finish partial_test.py and run the focused test' in out
    assert 'Claude / Edit' in out
    assert_common(out, repo)

def test_codex_to_claude(base: Path, home: Path):
    repo = setup_repo(base, 'codex-project')
    (repo / 'app.py').write_text('def value():\n    return 2\n')
    (repo / 'partial_test.py').write_text('def test_value():\n    assert value() == ')
    records = [
        {'type':'event_msg','timestamp':'2026-07-11T11:00:00+02:00','cwd':str(repo),'payload':{'type':'user_message','message':'Update value() and add the unfinished regression test.'}},
        {'type':'function_call','timestamp':'2026-07-11T11:00:05+02:00','cwd':str(repo),'payload':{'type':'function_call','name':'apply_patch','arguments':{'path':'app.py','change':'return 1 -> return 2'}}},
        {'type':'event_msg','timestamp':'2026-07-11T11:00:08+02:00','cwd':str(repo),'payload':{'type':'agent_message','message':'I changed app.py. Next step: complete partial_test.py, run tests, then commit.'}}
    ]
    write_jsonl(home/'.codex/sessions/2026/07/11'/'rollout.jsonl', records)
    env={**os.environ,'HOME':str(home)}
    run([sys.executable,str(SCRIPT),'--repo',str(repo)], repo, env)
    out=(repo/'passationlive.md').read_text()
    assert 'Update value() and add the unfinished regression test' in out
    assert 'complete partial_test.py, run tests, then commit' in out
    assert 'Codex / apply_patch' in out
    assert_common(out, repo)

def test_newest_exact_repo_session(base: Path, home: Path):
    repo = setup_repo(base, 'shared-name')
    other = base / 'other' / 'shared-name'
    other.mkdir(parents=True)

    older = home / '.claude/projects' / 'older-correct' / 'session.jsonl'
    write_jsonl(older, [
        {'type':'user','timestamp':'2026-07-11T09:00:00+02:00','cwd':str(repo),'message':{'role':'user','content':'OLDER CORRECT TASK'}},
    ])
    os.utime(older, (1000, 1000))

    wrong = home / '.codex/sessions/2026/07/11' / 'wrong.jsonl'
    write_jsonl(wrong, [
        {'type':'event_msg','timestamp':'2026-07-11T12:00:00+02:00','cwd':str(other),'payload':{'type':'user_message','message':'WRONG TASK'}},
    ])
    os.utime(wrong, (3000, 3000))

    newest = home / '.codex/sessions/2026/07/11' / 'newest.jsonl'
    write_jsonl(newest, [
        {'type':'event_msg','timestamp':'2026-07-11T11:00:00+02:00','cwd':str(repo),'payload':{'type':'user_message','message':'NEWEST CORRECT TASK'}},
        {'type':'event_msg','timestamp':'2026-07-11T11:00:05+02:00','cwd':str(repo),'payload':{'type':'agent_message','message':'Next step: finish app.py and run tests.'}},
    ])
    os.utime(newest, (2000, 2000))

    env = {**os.environ, 'HOME': str(home)}
    run([sys.executable, str(SCRIPT), '--repo', str(repo)], repo, env)
    out = (repo / 'passationlive.md').read_text()
    assert 'NEWEST CORRECT TASK' in out
    assert 'WRONG TASK' not in out
    assert '**Primary (newest matching session):** Codex' in out
    assert 'finish app.py and run tests' in out


def main():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        home=root/'home'; home.mkdir()
        base=root/'repos'; base.mkdir()
        test_claude_to_codex(base, home)
        test_codex_to_claude(base, home)
        test_newest_exact_repo_session(base, home)
    print('PASS: both handoff directions and exact newest-session selection')

if __name__ == '__main__':
    main()
