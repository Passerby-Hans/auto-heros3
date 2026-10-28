# -*- coding: utf-8 -*-
"""Mock tests for the D-007 trust rules (no game, no client, no network).

Covered:
  T1  seq-based reply matching: a stale OK from an earlier command is not accepted
  T2  state freshness / session identity / dead process rejection
  T3  execute() reports UNKNOWN when the reply arrives but the post-condition never holds
  T4  stop() only reports owned processes and removes the session file
Run: python -X utf8 tools/test_protocol_mock.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import vcmi_agent  # noqa: E402
from vcmi_agent import CommandResult, Session, StaleStateError, VcmiAgent  # noqa: E402

RESULTS = []


def check(name: str, cond: bool, detail: str = '') -> None:
    RESULTS.append((name, cond, detail))
    print(('PASS ' if cond else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def make_agent(tmp: Path) -> VcmiAgent:
    agent = VcmiAgent(str(tmp), log=False, max_state_age=3.0)
    sess = Session(session_id='test-session', dir=str(tmp), map_name=None,
                   client_pid=os.getpid(), client_create_time=None, exe=str(vcmi_agent.EXE),
                   created_at=time.time())
    # mock: pretend our python process is the owned client
    Session.alive = lambda self: True          # type: ignore[assignment]
    agent.session = sess
    agent.session_path.write_text(sess.to_json(), encoding='utf-8')
    (tmp / 'commands.txt').write_text('', encoding='utf-8')
    return agent


def write_state(tmp: Path, session_id: str = 'test-session', age_s: float = 0.0,
                in_game: bool = True) -> None:
    now_ms = int((time.time() - age_s) * 1000)
    st = {'in_game': in_game, 'session_id': session_id, 'ts_wall': now_ms, 'protocol': 2,
          'date': {'day': 1}, 'resources': {}, 'heroes': [], 'towns': [], 'objects': []}
    (tmp / 'state.json').write_text(json.dumps(st), encoding='utf-8')


def append_result(tmp: Path, line: str) -> None:
    with open(tmp / 'results.log', 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def test_seq(tmp: Path) -> None:
    agent = make_agent(tmp)
    write_state(tmp)
    seq1 = agent.submit('ping')
    seq2 = agent.submit('build 0 8')
    # reply for the *first* command appears after the second was submitted
    append_result(tmp, f'seq={seq1} OK ping')
    try:
        kind, line = agent.wait_seq(seq2, timeout=1.0)
        check('T1 seq matching', False, f'wrongly accepted {line}')
    except TimeoutError:
        check('T1 seq matching', True, 'stale seq=1 OK ignored for seq=2')
    append_result(tmp, f'seq={seq2} OK build 8')
    kind, line = agent.wait_seq(seq2, timeout=2.0)
    check('T1b seq matching', kind == 'OK' and f'seq={seq2}' in line, line)


def test_freshness(tmp: Path) -> None:
    agent = make_agent(tmp)
    write_state(tmp, age_s=60)
    try:
        agent.state(timeout=1.0)
        check('T2 stale state rejected', False, 'old snapshot accepted')
    except StaleStateError as exc:
        check('T2 stale state rejected', True, str(exc)[:60])
    write_state(tmp, session_id='other-session')
    try:
        agent.state(timeout=1.0)
        check('T2b foreign session rejected', False)
    except StaleStateError as exc:
        check('T2b foreign session rejected', True, str(exc)[:60])
    write_state(tmp)
    st = agent.state(timeout=1.0)
    check('T2c fresh state accepted', st.get('session_id') == 'test-session')


def test_unknown(tmp: Path) -> None:
    agent = make_agent(tmp)
    write_state(tmp)

    def never():
        return False, 'no effect'

    # Case A: no reply at all -> UNKNOWN (no reply before timeout).
    res = agent.execute('recruit 0 1 5', verify=never, verify_timeout=0.5, result_timeout=1.0)
    check('T3 unknown on no reply',
          res.status == 'UNKNOWN' and 'no reply' in (res.detail or ''), str(res))

    # Case B: the *matching* OK reply is present, but the post-condition never holds.
    # Capture the seq submit() takes and append the reply immediately after it, so the reply is
    # genuinely "after the recorded results offset" (an earlier pre-appended line is now
    # correctly ignored as a stale reply of another attempt).
    original_submit = agent.submit
    actual_sent: list[str] = []

    def submit_and_reply(line: str) -> int:
        seq = original_submit(line)
        append_result(tmp, f'seq={seq} OK {line}')
        actual_sent.append(line)
        return seq

    agent.submit = submit_and_reply  # type: ignore[assignment]
    res2 = agent.execute('build 0 9', verify=never, verify_timeout=0.8, result_timeout=2.0)
    check('T3b unknown when effect unproven',
          res2.status == 'UNKNOWN' and 'effect not observed' in (res2.detail or '')
          and actual_sent == ['build 0 9'], str(res2))


def test_epoch_and_injection(tmp: Path) -> None:
    agent = make_agent(tmp)
    # State older than the current engine start must be rejected even when it is inside the
    # generic freshness window (leftover state.json from a previous run).
    agent.max_state_age = 10.0
    (tmp / vcmi_agent.EPOCH_FILE).write_text(
        json.dumps({'started_at': time.time(), 'map_name': None}), encoding='utf-8')
    write_state(tmp, age_s=7)
    try:
        agent.state(timeout=1.0)
        check('T5 epoch stale file rejected', False, 'old engine state accepted')
    except StaleStateError as exc:
        check('T5 epoch stale file rejected', True, str(exc)[:60])

    # Single-line contract: embedded newlines would be a second bridge command.
    try:
        agent.submit('build 0 8\nend_turn')
        check('T5b newline injection rejected', False, 'multi-line command accepted')
    except vcmi_agent.BridgeError as exc:
        check('T5b newline injection rejected', True, str(exc)[:60])

    # Host command injection: illegal map names are refused before Popen is reached.
    bad = VcmiAgent(str(tmp), "Maps/x'; Remove-Item -Recurse C:\\; '", log=False)
    bad.session = agent.session
    called = []
    original_popen = vcmi_agent.subprocess.Popen
    vcmi_agent.subprocess.Popen = lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
        AssertionError('Popen must not be called'))
    try:
        bad.start(timeout=0.1)
        check('T5c illegal map name rejected', False)
    except (vcmi_agent.BridgeError, AssertionError) as exc:
        check('T5c illegal map name rejected', not called, str(exc)[:60])
    finally:
        vcmi_agent.subprocess.Popen = original_popen


def test_submit_lock(tmp: Path) -> None:
    import threading as th
    agent = make_agent(tmp)
    seqs: list[int] = []
    lock = th.Lock()

    def worker(i: int) -> None:
        seq = agent.submit(f'ping {i}')
        with lock:
            seqs.append(seq)

    threads = [th.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp / 'commands.txt').read_text(encoding='utf-8').splitlines()
    check('T6 concurrent submit unique seqs',
          sorted(seqs) == list(range(1, 9)) and len(lines) == 8,
          f'seqs={sorted(seqs)} lines={len(lines)}')


def test_stop(tmp: Path) -> None:
    agent = make_agent(tmp)
    agent.session.client_pid = 99999999          # not a live owned process
    res = agent.stop()
    check('T4 stop reports owned pid only', res.get('owned_pid') == 99999999, str(res))
    check('T4b session file removed', not agent.session_path.exists())


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / 'results.log').write_text('# mock\n', encoding='utf-8')
        test_seq(td)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / 'results.log').write_text('# mock\n', encoding='utf-8')
        test_freshness(td)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / 'results.log').write_text('# mock\n', encoding='utf-8')
        test_unknown(td)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / 'results.log').write_text('# mock\n', encoding='utf-8')
        test_epoch_and_injection(td)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / 'results.log').write_text('# mock\n', encoding='utf-8')
        test_submit_lock(td)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        test_stop(td)
    failed = [r for r in RESULTS if not r[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
