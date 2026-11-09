"""Explicit LIVE default-routing, missing-artifact and manual-fallback acceptance.

Never collected offline. Requires --execute; uses real MCP stdio and VCMI.
Temporarily hides only the pinned private outputs module, removes this test's
ready marker, and restores exactly the saved files in finally. No playback,
endpoint setters, display changes, cheats, or game actions are performed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid

import test_native_recording_e2e as H

sys.path.insert(0, str(H.ROOT / 'src'))
from auto_heros3.recording.native_artifacts import load_native_artifacts  # noqa: E402


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def contents(path):
    return path.read_bytes() if path.is_file() else None


def restore(path, data):
    if contents(path) == data:
        return
    if data is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(data)


def native_processes():
    import psutil
    rows = []
    for process in psutil.process_iter(['pid', 'name', 'exe', 'create_time']):
        if (process.info.get('name') or '').lower() in (
                'auto-heros3-recorder.exe', 'obs-media-selftest.exe'):
            rows.append(process.info)
    return rows


async def phase(args, out, mode, evidence, baseline, all_pids, artifacts):
    from mcp import StdioServerParameters
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    out.mkdir()
    env = H.isolated_env(out, default_path=True)
    if mode == 'legacy_missing_module':
        env['AUTO_HEROS3_AUDIO_DRIVER'] = 'disk'
    target, evidence['game_target'] = H.select_map(out, args.save)
    evidence.update(mode=mode, started_at=time.time(), ok=False)
    before_settings = contents(H.SETTINGS)
    owned_game = owned_recorder = None
    session_attempted = recording_attempted = False

    def observe(label):
        snapshot = H.endpoint_snapshot()
        comparison = H.endpoint_comparison(baseline, snapshot, all_pids)
        evidence.setdefault('endpoint_samples', []).append({
            'label': label, 'snapshot': snapshot, 'comparison': comparison})
        H.write_json(out / 'result.json', evidence)
        need(not H.observation_requires_abort(comparison), 'endpoint/session observation failed')

    params = StdioServerParameters(command=sys.executable,
        args=['-X', 'utf8', str(H.SERVER)], env=env, cwd=str(H.ROOT))
    with (out / 'mcp-stderr.log').open('w', encoding='utf-8') as stderr:
        async with stdio_client(params, errlog=stderr) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                async def call(name, arguments=None, timeout=180, allow_error=False):
                    result = await asyncio.wait_for(client.call_tool(name, arguments or {}), timeout)
                    raw = result.model_dump(mode='json')
                    with (out / 'transcript.jsonl').open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps({'at': time.time(), 'tool': name,
                            'arguments': arguments or {}, 'result': raw}, ensure_ascii=False) + '\n')
                    try:
                        payload = H.tool_payload(result)
                    except ValueError:
                        if not allow_error:
                            raise
                        payload = {'ok': False, 'error': raw}
                    if getattr(result, 'is_error', getattr(result, 'isError', False)) and not allow_error:
                        raise RuntimeError(f'MCP tool error: {name}: {payload}')
                    return payload

                try:
                    await asyncio.wait_for(client.initialize(), 90)
                    listed = await asyncio.wait_for(client.list_tools(), 90)
                    schema = next(tool.model_dump(mode='json', by_alias=True)
                                  for tool in listed.tools if tool.name == 'recording_start')
                    evidence['recording_schema'] = schema
                    properties = (schema.get('inputSchema') or schema.get('input_schema'))['properties']
                    need(properties['backend'].get('default') == 'libobs', 'MCP default is not libobs')
                    need(not H.vcmi_processes() and not native_processes(), 'unexpected existing media process')
                    session_attempted = True
                    started = await call('session_start', H.session_arguments(target, args.display_index),
                                         timeout=360, allow_error=(mode == 'default_missing_module'))
                    evidence['session_start'] = started
                    if mode == 'default_missing_module':
                        need(started.get('ok') is not True, 'missing native module silently started game')
                        text = json.dumps(started)
                        need('native_artifact_unreadable' in text and 'a3-obs-outputs.dll' in text,
                             'default failed for a different reason than missing private module')
                        need(not H.vcmi_processes() and not native_processes(), 'failed start left processes')
                        need(contents(H.SETTINGS) == before_settings, 'failed start changed settings')
                        observe('missing_module_rejected')
                        evidence['expected_rejection'] = True
                    else:
                        need(started.get('ok') is True and started.get('client_pid'), 'session_start failed')
                        owned_game = H.process_identity(int(started['client_pid']), H.GAME)
                        all_pids.add(owned_game['pid'])
                        evidence['owned_game'] = owned_game
                        session = json.loads((out / 'state/session.json').read_text(encoding='utf-8'))
                        expected_driver = 'disk' if mode == 'legacy_missing_module' else 'a3capture'
                        need(session.get('audio_driver') == expected_driver, 'session audio driver mismatch')
                        expected_sdl = artifacts['original_sdl_sha256'] if mode == 'legacy_missing_module' else artifacts['private_sdl_sha256']
                        evidence['runtime_sdl_by_product'] = H.sha(H.GAME.parent / 'SDL2.dll')
                        need(evidence['runtime_sdl_by_product'] == expected_sdl, 'product did not stage selected SDL')
                        observe('session_started')
                        for index in range(1 if mode == 'legacy_missing_module' else 2):
                            if index == 1:
                                framed = Path(session['audio_capture_file']).resolve()
                                need(framed.is_relative_to(out), 'framed file escaped isolated test state')
                                ready = Path(str(framed) + '.ready')
                                ready_bytes = ready.read_bytes()
                                stale = out / 'state/game-audio.raw'
                                stale.write_bytes(b'stale raw must never be selected' * 128)
                                inventory = sorted(str(path) for path in (out / 'recordings').glob('*'))
                                try:
                                    ready.unlink()
                                    rejected = await call('recording_start')
                                    evidence['ready_rejection'] = rejected
                                    need(rejected.get('ok') is False and rejected.get('error') == 'native_audio_not_ready',
                                         'missing ready silently chose stale raw')
                                    status = await call('recording_status')
                                    need(status.get('recording') is False and not native_processes(), 'rejected start created recorder')
                                    need(sorted(str(path) for path in (out / 'recordings').glob('*')) == inventory,
                                         'rejected start created recording artifacts')
                                finally:
                                    ready.write_bytes(ready_bytes)
                            arguments = {'backend': 'legacy'} if mode == 'legacy_missing_module' else {}
                            recording_attempted = True
                            recording = await call('recording_start', arguments)
                            need(recording.get('ok') is True, f'recording_start failed: {recording}')
                            expected_exe = Path(sys.executable) if mode == 'legacy_missing_module' else H.ARTIFACTS['recorder']
                            owned_recorder = H.process_identity(int(recording.get('pid') or recording['recorder_pid']), expected_exe)
                            all_pids.add(owned_recorder['pid'])
                            evidence['owned_recorder'] = owned_recorder
                            if mode != 'legacy_missing_module':
                                need(recording.get('backend') == 'libobs' and recording.get('audio_route') == 'session_a3capture',
                                     'default did not select session framed stream')
                            for second in range(args.seconds):
                                await asyncio.sleep(1)
                                observe(f'recording_{index}_{second}')
                            stopped = await call('recording_stop', timeout=180)
                            recording_attempted = False
                            need(stopped.get('ok') is True, f'recording_stop failed: {stopped}')
                            if mode == 'legacy_missing_module':
                                need(stopped.get('audio_muxed') is True, 'legacy fallback did not mux game audio')
                            else:
                                need(stopped.get('checks_ok') is True and stopped.get('native_checks_ok') is True,
                                     'native media checks failed')
                            evidence.setdefault('recordings', []).append({'start': recording, 'stop': stopped})
                            need(not H.identity_alive(owned_recorder), 'stopped recorder remains alive')
                    evidence['operations_completed'] = True
                finally:
                    if owned_game is None and session_attempted:
                        try:
                            saved = json.loads((out / 'state/session.json').read_text(encoding='utf-8'))
                            if Path(saved['dir']).resolve() == out / 'state' and saved['created_at'] >= evidence['started_at']:
                                candidate = H.process_identity(int(saved['client_pid']), H.GAME)
                                if abs(candidate['create_time'] - saved['client_create_time']) <= 1:
                                    owned_game = candidate
                                    all_pids.add(candidate['pid'])
                        except Exception as exc:
                            evidence['identity_recovery_note'] = str(exc)
                    if owned_recorder is None and recording_attempted:
                        try:
                            saved = json.loads((out / 'state/recording.json').read_text(encoding='utf-8'))
                            expected_exe = Path(sys.executable) if mode == 'legacy_missing_module' else H.ARTIFACTS['recorder']
                            candidate = H.process_identity(int(saved['pid']), expected_exe)
                            if (Path(saved['dir']).resolve().is_relative_to(out / 'recordings')
                                    and candidate['create_time'] >= evidence['started_at']):
                                owned_recorder = candidate
                                all_pids.add(candidate['pid'])
                        except Exception as exc:
                            evidence['recorder_recovery_note'] = str(exc)
                    if recording_attempted:
                        try:
                            evidence['cleanup_recording_stop'] = await call('recording_stop')
                        except BaseException as exc:
                            evidence['cleanup_recording_error'] = str(exc)
                    if session_attempted:
                        try:
                            evidence['session_stop'] = await call('session_stop', timeout=120, allow_error=True)
                        except BaseException as exc:
                            evidence['cleanup_session_error'] = str(exc)
                    for label, identity in (('recorder', owned_recorder), ('game', owned_game)):
                        if H.identity_alive(identity):
                            evidence.setdefault('forced_cleanup', {})[label] = H.stop_owned_fallback(identity)
                    evidence['settings_restored_by_product'] = contents(H.SETTINGS) == before_settings
                    evidence['remaining_vcmi'] = H.vcmi_processes()
                    evidence['remaining_native'] = native_processes()
                    observe('phase_finished')
                    evidence['ok'] = bool(evidence.get('operations_completed') and evidence['settings_restored_by_product']
                        and not evidence['remaining_vcmi'] and not evidence['remaining_native']
                        and (mode == 'default_missing_module' or evidence.get('session_stop', {}).get('ok') is True)
                        and not any(evidence.get(key) for key in ('forced_cleanup', 'cleanup_recording_error', 'cleanup_session_error')))
                    H.write_json(out / 'result.json', evidence)
    need(evidence['ok'], f'{mode} failed lifecycle checks')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--seconds', type=int, default=8)
    parser.add_argument('--display-index', default='2')
    parser.add_argument('--save', type=Path)
    args = parser.parse_args()
    if not args.execute:
        parser.error('LIVE lifecycle test requires --execute')
    if not 2 <= args.seconds <= 60:
        parser.error('--seconds must be 2..60')
    if args.display_index != 'default' and not args.display_index.isdecimal():
        parser.error('display index must be nonnegative or default')
    out = args.out.resolve()
    allowed = (H.ROOT / 'var').resolve()
    if not out.is_relative_to(allowed) or out == allowed or out.exists():
        parser.error('out must be a fresh directory under var')
    if H.vcmi_processes() or native_processes():
        parser.error('existing game/native recorder; refusing lifecycle test')
    artifacts = load_native_artifacts(H.ROOT)
    out.mkdir(parents=True)
    module = artifacts['outputs_module']
    hidden = module.with_name(f'.{module.name}.lifecycle-{uuid.uuid4().hex}')
    snapshots = {H.SETTINGS: contents(H.SETTINGS), module: contents(module)}
    for name in ('SDL2.dll', 'SDL2.dll.original', '.a3-sdl-deployment.json', '.a3-sdl-deployment.lock'):
        path = H.GAME.parent / name
        snapshots[path] = contents(path)
    for index, (path, content) in enumerate(snapshots.items()):
        if content is not None:
            (out / f'baseline-{index}.bin').write_bytes(content)
    H.write_json(out / 'baseline-files.json', {str(path): index for index, path in enumerate(snapshots)})
    evidence = {'ok': False, 'phases': [], 'started_at': time.time(),
                'scope': 'default routing/manual recovery, not real-game event A/V or audible office proof'}
    baseline = None
    pids = set()
    try:
        baseline = H.endpoint_snapshot()
        H.write_json(out / 'endpoint-before.json', baseline)
        for mode in ('default', 'default_missing_module', 'legacy_missing_module'):
            if mode == 'default_missing_module':
                need(not H.vcmi_processes() and not native_processes(), 'cannot hide module while media process is alive')
                os.replace(module, hidden)
            current = {}
            evidence['phases'].append(current)
            asyncio.run(phase(args, out / mode, mode, current, baseline, pids, artifacts))
        evidence['all_phases_passed'] = True
    except BaseException as exc:
        evidence['error'] = f'{type(exc).__name__}: {exc}'
        evidence['traceback'] = traceback.format_exc()
    finally:
        try:
            if hidden.exists():
                need(not module.exists(), 'private module replaced externally; refusing overwrite')
                os.replace(hidden, module)
            need(not H.vcmi_processes() and not native_processes(), 'live processes block baseline restore')
            for path, original in snapshots.items():
                restore(path, original)
            evidence['baseline_files_restored'] = all(contents(path) == data for path, data in snapshots.items())
            if baseline is not None:
                after = H.endpoint_snapshot()
                evidence['endpoint_after'] = H.endpoint_comparison(baseline, after, pids)
            evidence['ok'] = bool(evidence.get('all_phases_passed') and evidence.get('baseline_files_restored')
                                  and evidence.get('endpoint_after', {}).get('ok'))
        except BaseException as exc:
            evidence['restore_error'] = f'{type(exc).__name__}: {exc}'
        evidence['finished_at'] = time.time()
        H.write_json(out / 'result.json', evidence)
    print(json.dumps({'ok': evidence['ok'], 'result': str(out / 'result.json'),
                      'error': evidence.get('error'), 'restore_error': evidence.get('restore_error')}))
    return 0 if evidence['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
