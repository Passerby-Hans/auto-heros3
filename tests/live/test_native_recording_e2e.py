"""Explicit LIVE MCP stdio recording acceptance; never part of offline runs.

Requires --execute and a new output directory. Refuses existing VCMI processes.
Uses the existing display index 2; does not configure displays or audio endpoints.
No game_act, cheats, audio playback, automatic backend fallback, or default changes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "tools/mcp_server_v2.py"
GAME = ROOT / "vendor/vcmi-src/build/bin/VCMI_client.exe"
SETTINGS = Path.home() / "Documents/My Games/vcmi/config/settings.json"
SAVE_ROOT = Path.home() / "Documents/My Games/vcmi/Saves"
ARTIFACTS = {
    "recorder": ROOT / "platform/obs-recorder/build-media/bin/auto-heros3-recorder.exe",
    "outputs": ROOT / "platform/obs-recorder/bin/a3-obs-outputs.dll",
    "private_sdl": ROOT / "platform/sdl-capture-patch/stage/SDL2.dll",
    "game": GAME,
}


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def sha(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def artifact_snapshot() -> dict:
    return {name: {"path": str(path), "sha256": sha(path)} for name, path in ARTIFACTS.items()}


def vcmi_processes() -> list[dict]:
    import psutil
    rows = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            if (process.info.get("name") or "").lower().startswith("vcmi"):
                rows.append(process.info)
        except psutil.NoSuchProcess:
            continue
    return rows


def process_identity(pid: int, exe: Path) -> dict:
    import psutil
    process = psutil.Process(pid)
    if Path(process.exe()).resolve() != exe.resolve():
        raise RuntimeError(f"unexpected process executable: {pid}")
    return {"pid": pid, "create_time": process.create_time(), "exe": str(exe.resolve())}


def identity_alive(identity: dict | None) -> bool:
    import psutil
    if not identity:
        return False
    try:
        process = psutil.Process(identity["pid"])
        return (process.is_running() and process.create_time() == identity["create_time"] and
                Path(process.exe()).resolve() == Path(identity["exe"]))
    except psutil.NoSuchProcess:
        return False


def stop_owned_fallback(identity: dict | None) -> dict:
    """Only a saved PID + creation time + executable identity may be stopped."""
    import psutil
    if not identity_alive(identity):
        return {"needed": False}
    process = psutil.Process(identity["pid"])
    children = [{"pid": child.pid, "create_time": child.create_time(), "exe": child.exe()}
                for child in process.children(recursive=True)]
    process.terminate()
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        if identity_alive(identity):
            process.kill()
            process.wait(timeout=5)
    # Only children observed under our verified owner, never a name-wide kill.
    for child in children:
        if identity_alive(child):
            psutil.Process(child["pid"]).terminate()
    return {"needed": True, "identity": identity, "children": children}


def endpoint_snapshot() -> dict:
    """Read every active render endpoint and sessions; no Set* API is used."""
    from pycaw.utils import AudioUtilities, AudioSession
    from pycaw.api.audiopolicy import IAudioSessionControl2
    from pycaw.constants import EDataFlow, ERole, DEVICE_STATE
    enumerator = AudioUtilities.GetDeviceEnumerator()
    defaults = {}
    for role in (ERole.eConsole, ERole.eMultimedia, ERole.eCommunications):
        defaults[role.name] = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value, role.value).GetId()
    devices, sessions = [], []
    for device in AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value):
        volume = device.EndpointVolume
        devices.append({"id": device.id, "name": device.FriendlyName,
                        "mute": bool(volume.GetMute()),
                        "volume": float(volume.GetMasterVolumeLevelScalar())})
        entries = device.AudioSessionManager.GetSessionEnumerator()
        for index in range(entries.GetCount()):
            session = AudioSession(entries.GetSession(index).QueryInterface(IAudioSessionControl2))
            control = session.SimpleAudioVolume
            sessions.append({"device": device.id, "instance": session.InstanceIdentifier,
                             "pid": session.ProcessId, "state": session.State,
                             "mute": bool(control.GetMute()), "volume": float(control.GetMasterVolume())})
    return {"observed_at": time.time(), "defaults": defaults,
            "devices": sorted(devices, key=lambda row: row["id"]), "sessions": sessions}


def endpoint_comparison(before: dict, after: dict, owned_pids: set[int]) -> dict:
    base = {row["id"]: row for row in before["devices"]}
    current = {row["id"]: row for row in after["devices"]}
    stable = before["defaults"] == after["defaults"] and base.keys() == current.keys()
    for key in base.keys() & current.keys():
        stable &= (base[key]["mute"] == current[key]["mute"] and
                   abs(base[key]["volume"] - current[key]["volume"]) <= 1e-6)
    prior_sessions = {(row["device"], row["instance"]): row for row in before["sessions"]}
    office_changes = []
    for row in after["sessions"]:
        old = prior_sessions.get((row["device"], row["instance"]))
        if old and row["pid"] not in owned_pids and (
                old["mute"] != row["mute"] or abs(old["volume"] - row["volume"]) > 1e-6):
            office_changes.append({"before": old, "after": row})
    physical = [row for row in after["sessions"] if row["pid"] in owned_pids]
    return {"endpoint_controls_unchanged": bool(stable), "office_control_changes": office_changes,
            "owned_physical_sessions": physical,
            "ok": bool(stable and not office_changes and not physical),
            "audible_office_continuity_proven": False}


def observation_requires_abort(comparison: dict, observe_endpoint_changes: bool = False) -> bool:
    """Observation mode never permits game/recorder physical audio sessions."""
    return bool(comparison.get("owned_physical_sessions") or
                (not observe_endpoint_changes and comparison.get("ok") is not True))


def final_verdict(evidence: dict) -> dict:
    """Separate media acceptance from environment observations without hiding either."""
    comparisons = [row.get("comparison", {}) for row in evidence.get("endpoint_samples", [])]
    after = evidence.get("endpoint_after_comparison", {})
    endpoint_ok = bool(comparisons and all(row.get("ok") is True for row in comparisons) and
                       after.get("ok") is True)
    physical_seen = any(row.get("owned_physical_sessions") for row in comparisons + [after])
    stops = evidence.get("recording_stop") or []
    formal_stop_ok = bool(stops and all(row.get("ok") is True and
        row.get("checks_ok") is True and row.get("native_checks_ok") is True for row in stops))
    if evidence.get("double_stop"):
        formal_stop_ok = formal_stop_ok and len(stops) == 2 and stops[0] == stops[1]
    media_ok = bool(evidence.get("run_session_completed") is True and formal_stop_ok and
        evidence.get("tail_verifier", {}).get("v2_ok") is True and
        evidence.get("artifact_hashes_unchanged") is True and
        evidence.get("settings_restored_by_product") is True and
        evidence.get("settings_bytes_restored") is True and
        evidence.get("runtime_sdl_bytes_restored") is True and
        evidence.get("session_stop", {}).get("ok") is True and
        evidence.get("owned_game_left_alive") is False and
        evidence.get("owned_recorder_left_alive") is False and
        evidence.get("remaining_vcmi_processes") == [] and not physical_seen and
        not any(evidence.get(key) for key in ("error", "finalization_error",
            "cleanup_recording_error", "cleanup_session_error", "forced_cleanup_errors", "forced_cleanup")))
    ok = media_ok and endpoint_ok
    return {"media_checks_ok": media_ok, "endpoint_checks_ok": endpoint_ok, "ok": ok,
            "exit_code": 0 if ok else 2 if media_ok else 1}


def isolated_env(out: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AUTO_HEROS3_", "A3_", "A3CAPTURE_", "SDL_AUDIO"))}
    env.update({"PYTHONIOENCODING": "utf8", "AUTO_HEROS3_AUDIO_DRIVER": "a3capture",
                "AUTO_HEROS3_STATE_DIR": str(out / "state"),
                "AUTO_HEROS3_SAVES_DIR": str(out / "saves"),
                "AUTO_HEROS3_SAVE_SUBDIR": "native_e2e",
                "AUTO_HEROS3_RECORDINGS_DIR": str(out / "recordings"),
                "AUTO_HEROS3_MEMORY_ROOT": str(out / "player")})
    return env


def select_map(out: Path, requested_save: Path | None) -> tuple[str, dict]:
    candidates = ([requested_save.resolve()] if requested_save else
                  sorted(SAVE_ROOT.rglob("*d05*.vsgm1")) if SAVE_ROOT.exists() else [])
    if not candidates:
        return "Maps/Arrogance", {"map": "Maps/Arrogance", "reason": "no existing d05 save"}
    source = candidates[0]
    if not source.is_file() or source.suffix.lower() != ".vsgm1":
        raise ValueError("--save must name an existing .vsgm1")
    dest = out / "saves/native_e2e/d05.vsgm1"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    companion = source.with_suffix(".vcgm1")
    companion_copy = None
    if companion.exists():
        companion_copy = dest.with_suffix(".vcgm1")
        shutil.copy2(companion, companion_copy)
    # session_start explicitly supports absolute .vsgm1 paths. No formal saves
    # are overwritten and no save_game command is issued.
    return str(dest), {"source": str(source), "copy": str(dest), "sha256": sha(source),
                       "client_save_copy": str(companion_copy) if companion_copy else None}


def tool_payload(result) -> dict:
    structured = getattr(result, "structured_content", getattr(result, "structuredContent", None))
    if isinstance(structured, dict):
        return structured
    for entry in getattr(result, "content", []):
        text = getattr(entry, "text", None)
        if text:
            try:
                decoded = json.loads(text)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                return decoded
    raise ValueError("MCP tool returned no JSON object")


async def run_session(args, out: Path, evidence: dict, baseline: dict) -> None:
    from mcp import StdioServerParameters
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client
    sys.path.insert(0, str(ROOT / "platform/audio-protocol/python"))
    from verify_tail_contract import check_run

    env = isolated_env(out)
    target, evidence["game_target"] = select_map(out, args.save)
    write_json(out / "environment.json", {key: value for key, value in env.items()
                                         if key.startswith("AUTO_HEROS3_")})
    session_attempted = recording_attempted = stopped = False
    owned_game = owned_recorder = None
    owned_pids = set()
    result_dir = None

    def log(method, params, response=None, error=None, started=None):
        row = {"method": method, "params": params, "started_at": started,
               "finished_at": time.time(), "response": response, "error": error}
        with (out / "transcript.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def observe(label):
        snapshot = endpoint_snapshot()
        comparison = endpoint_comparison(baseline, snapshot, owned_pids)
        evidence.setdefault("endpoint_samples", []).append({"label": label, "snapshot": snapshot,
                                                            "comparison": comparison})
        write_json(out / "endpoint-samples.json", evidence["endpoint_samples"])
        if observation_requires_abort(comparison, args.observe_endpoint_changes):
            raise RuntimeError("endpoint/session observation failed; no endpoint restore attempted")

    params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", str(SERVER)],
                                    env=env, cwd=str(ROOT))
    with (out / "mcp-stderr.log").open("w", encoding="utf-8") as stderr:
        async with stdio_client(params, errlog=stderr) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                async def call(name, arguments=None, timeout=180):
                    arguments = arguments or {}
                    started = time.time()
                    try:
                        result = await asyncio.wait_for(client.call_tool(name, arguments), timeout)
                        log("tools/call", {"name": name, "arguments": arguments},
                            result.model_dump(mode="json"), started=started)
                        if getattr(result, "is_error", getattr(result, "isError", False)):
                            raise RuntimeError(f"MCP tool error: {name}")
                        return tool_payload(result)
                    except BaseException as exc:
                        log("tools/call", {"name": name, "arguments": arguments},
                            error=f"{type(exc).__name__}: {exc}", started=started)
                        raise

                try:
                    init = await asyncio.wait_for(client.initialize(), 90)
                    log("initialize", {}, init.model_dump(mode="json"))
                    listed = await asyncio.wait_for(client.list_tools(), 90)
                    log("tools/list", {}, listed.model_dump(mode="json"))
                    surface = {}
                    for tool in listed.tools:
                        dumped = tool.model_dump(mode="json", by_alias=True)
                        surface[tool.name] = dumped.get("inputSchema") or dumped.get("input_schema") or {}
                    required = {"session_start", "session_stop", "recording_start", "recording_stop",
                                "recording_status"}
                    if not required.issubset(surface):
                        raise RuntimeError(f"missing MCP tools: {required - surface.keys()}")
                    for tool, fields in (("session_start", ("map_name", "display_index")),
                                         ("recording_start", ("backend", "rollover_seconds"))):
                        if not set(fields).issubset(surface[tool].get("properties", {})):
                            raise RuntimeError(f"MCP schema missing expected arguments: {tool}")
                    if vcmi_processes():
                        raise RuntimeError("VCMI appeared before launch; refusing to touch it")
                    session_attempted = True
                    start = await call("session_start", {"map_name": target, "display_index": 2}, timeout=360)
                    evidence["session_start"] = start
                    if start.get("ok") is not True or not start.get("client_pid"):
                        raise RuntimeError("session_start failed")
                    owned_game = process_identity(int(start["client_pid"]), GAME)
                    owned_pids.add(owned_game["pid"])
                    evidence["owned_game"] = owned_game
                    evidence["staged_sdl_during"] = sha(GAME.parent / "SDL2.dll")
                    if evidence["staged_sdl_during"] != evidence["artifacts_before"]["private_sdl"]["sha256"]:
                        raise RuntimeError("VCMI staged SDL does not match private artifact")
                    observe("session_started")
                    recording_attempted = True
                    recording = await call("recording_start", {"backend": "libobs", "rollover_seconds": 10})
                    evidence["recording_start"] = recording
                    if recording.get("ok") is not True or recording.get("backend") != "libobs":
                        raise RuntimeError("explicit native recording_start failed")
                    result_dir = Path(recording["dir"]).resolve()
                    if not result_dir.is_relative_to(out / "recordings"):
                        raise RuntimeError("recorder output escaped test directory")
                    owned_recorder = process_identity(int(recording["pid"]), ARTIFACTS["recorder"])
                    owned_pids.add(owned_recorder["pid"])
                    evidence["owned_recorder"] = owned_recorder
                    started = time.monotonic()
                    killed_once = False
                    while time.monotonic() - started < args.seconds:
                        observe("recording")
                        status = await call("recording_status", timeout=15)
                        if not status.get("recording"):
                            raise RuntimeError("recording_status became inactive")
                        if args.kill_native_once and not killed_once and time.monotonic() - started >= args.seconds / 2:
                            import psutil
                            state_path = out / "state/recording.json"
                            prior = json.loads(state_path.read_text(encoding="utf-8"))
                            if (int(prior.get("pid") or 0) != owned_recorder["pid"] or
                                    Path(prior.get("dir") or "").resolve() != result_dir or
                                    int(status.get("pid") or 0) != owned_recorder["pid"] or
                                    not identity_alive(owned_recorder)):
                                raise RuntimeError("refusing kill: native owner/state identity changed")
                            # Revalidate all three identity components immediately
                            # before terminating exactly this recorder process.
                            if process_identity(owned_recorder["pid"], ARTIFACTS["recorder"]) != owned_recorder:
                                raise RuntimeError("refusing kill: native PID was reused")
                            fault = {"kind": "kill_native_once", "requested_at": time.time(),
                                     "elapsed_s": time.monotonic() - started,
                                     "old_identity": dict(owned_recorder), "old_state": prior,
                                     "old_status": status, "status_samples": [],
                                     "lossless_continuity_claim": False}
                            evidence["fault_injection"] = fault
                            write_json(out / "fault-injection.json", fault)
                            victim = psutil.Process(owned_recorder["pid"])
                            if (victim.create_time() != owned_recorder["create_time"] or
                                    Path(victim.exe()).resolve() != Path(owned_recorder["exe"])):
                                raise RuntimeError("refusing kill: final native process identity mismatch")
                            victim.terminate()
                            victim.wait(timeout=5)
                            fault["terminated_at"] = time.time()
                            killed_once = True
                            restart_deadline = time.monotonic() + 45
                            while time.monotonic() < restart_deadline:
                                recovered_status = await call("recording_status", timeout=15)
                                try:
                                    persisted = json.loads(state_path.read_text(encoding="utf-8"))
                                except (OSError, ValueError):
                                    persisted = {}
                                fault["status_samples"].append({"at": time.time(),
                                    "mcp_status": recovered_status, "persisted_state": persisted})
                                write_json(out / "fault-injection.json", fault)
                                # recording_status does not currently expose
                                # restart_count. Its liveness/PID must agree with
                                # this test's persisted, atomically written state.
                                new_pid = int(persisted.get("pid") or 0)
                                if (recovered_status.get("recording") is True and
                                        int(persisted.get("restart_count") or 0) >= 1 and
                                        new_pid != owned_recorder["pid"] and
                                        int(recovered_status.get("pid") or 0) == new_pid and
                                        persisted.get("dir") == recovered_status.get("dir")):
                                    new_dir = Path(persisted["dir"]).resolve()
                                    if not new_dir.is_relative_to(out / "recordings"):
                                        raise RuntimeError("restarted output escaped isolated directory")
                                    owned_recorder = process_identity(new_pid, ARTIFACTS["recorder"])
                                    evidence["owned_recorder"] = owned_recorder
                                    owned_pids.add(new_pid)
                                    result_dir = new_dir
                                    fault.update({"recovered_at": time.time(),
                                                  "new_identity": dict(owned_recorder),
                                                  "new_state": persisted, "new_status": recovered_status,
                                                  "restart_count_source": "isolated state/recording.json"})
                                    write_json(out / "fault-injection.json", fault)
                                    break
                                await asyncio.sleep(.5)
                            else:
                                raise RuntimeError("watchdog did not recover within 45s after injected kill")
                        await asyncio.sleep(min(5, max(0, args.seconds - (time.monotonic() - started))))
                    evidence["recording_wall_seconds"] = time.monotonic() - started
                    if args.double_stop:
                        replies = await asyncio.gather(call("recording_stop"), call("recording_stop"))
                    else:
                        replies = [await call("recording_stop")]
                    evidence["recording_stop"] = replies
                    stopped = True
                    if not all(row.get("ok") is True and row.get("checks_ok") is True and
                               row.get("native_checks_ok") is True for row in replies):
                        raise RuntimeError("recording_stop validation failed")
                    if args.double_stop and replies[0] != replies[1]:
                        raise RuntimeError("concurrent stop did not return the same run result")
                    if args.kill_native_once:
                        if not killed_once or int(replies[0].get("restart_count") or 0) < 1:
                            raise RuntimeError("kill injection/restart missing from final stop result")
                        old_dir = Path(evidence["fault_injection"]["old_state"]["dir"]).resolve()
                        old_runs = [row for row in replies[0].get("runs", [])
                                    if Path(row.get("dir") or "").resolve() == old_dir and
                                    row.get("manifest_state") in ("failed", "partial")]
                        old_files = [row for row in replies[0].get("files", [])
                                     if Path(row.get("run_dir") or "").resolve() == old_dir]
                        if not old_runs or any(row.get("state") == "complete" for row in old_files):
                            raise RuntimeError("killed run was omitted or promoted to complete")
                        old_manifest = json.loads((old_dir / "s4b/manifest.json").read_text(encoding="utf-8"))
                        if old_manifest.get("state") not in ("failed", "partial"):
                            raise RuntimeError("killed run manifest lost its failed/partial state")
                        evidence["recovery_validation"] = {"restart_count": replies[0]["restart_count"],
                            "old_runs": old_runs, "old_files": old_files,
                            "old_manifest_state": old_manifest["state"],
                            "current_run": str(result_dir), "current_checks_ok": True,
                            "lossless_continuity_claim": False}
                    evidence["tail_verifier"] = check_run(result_dir)
                    report = json.loads((result_dir / "selftest.json").read_text(encoding="utf-8"))
                    audio = report.get("media", {}).get("audio", {})
                    none = audio.get("monitoring_type_none_value")
                    evidence["monitoring"] = audio
                    if (none != 0 or audio.get("source_monitoring_type") != none or
                            audio.get("raw_audio_source_monitoring_type") != none):
                        raise RuntimeError("monitoring NONE was not observed")
                    if not evidence["tail_verifier"].get("v2_ok"):
                        raise RuntimeError("v2 tail/interval contract failed")
                    observe("recording_stopped")
                finally:
                    # A timed-out session_start may have created our session
                    # before its response was lost. Recover only the fresh
                    # isolated session identity, with PID reuse protection.
                    if owned_game is None and session_attempted:
                        try:
                            saved = json.loads((out / "state/session.json").read_text(encoding="utf-8"))
                            if (Path(saved["dir"]).resolve() == out / "state" and
                                    float(saved["created_at"]) >= evidence["started_at"]):
                                candidate = process_identity(int(saved["client_pid"]), GAME)
                                if abs(candidate["create_time"] - float(saved["client_create_time"])) <= 1:
                                    owned_game = candidate
                                    evidence["owned_game"] = candidate
                                    owned_pids.add(candidate["pid"])
                        except Exception as exc:
                            evidence["identity_recovery_note"] = f"{type(exc).__name__}: {exc}"
                    if recording_attempted and not stopped:
                        try:
                            evidence["cleanup_recording_stop"] = await call("recording_stop")
                        except BaseException as exc:
                            evidence["cleanup_recording_error"] = f"{type(exc).__name__}: {exc}"
                    if session_attempted:
                        try:
                            # Isolated server state owns only the session started
                            # above, including a start whose reply was lost.
                            evidence["session_stop"] = await call("session_stop", timeout=120)
                        except BaseException as exc:
                            evidence["cleanup_session_error"] = f"{type(exc).__name__}: {exc}"
                    for key, identity in (("recorder", owned_recorder), ("game", owned_game)):
                        try:
                            if identity_alive(identity):
                                evidence.setdefault("forced_cleanup", {})[key] = stop_owned_fallback(identity)
                        except Exception as exc:
                            evidence.setdefault("forced_cleanup_errors", {})[key] = str(exc)
                    evidence["owned_game_left_alive"] = identity_alive(owned_game)
                    evidence["owned_recorder_left_alive"] = identity_alive(owned_recorder)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--execute", action="store_true", help="explicitly start the live game and recording")
    parser.add_argument("--seconds", type=int, choices=(60, 600), default=60)
    parser.add_argument("--double-stop", action="store_true")
    parser.add_argument("--kill-native-once", action="store_true",
                        help="terminate only the verified owned native PID halfway through; require watchdog recovery")
    parser.add_argument("--observe-endpoint-changes", action="store_true",
                        help="record environment changes without aborting media checks; owned physical audio still aborts")
    parser.add_argument("--save", type=Path, help="existing .vsgm1 copied into isolated saves")
    args = parser.parse_args()
    if not args.execute:
        parser.error("LIVE test requires --execute; no game or recorder started")
    if vcmi_processes():
        parser.error("existing VCMI process detected; stop it separately before this test")
    out = args.out.resolve()
    allowed = (ROOT / "var").resolve()
    if not out.is_relative_to(allowed) or out == allowed:
        parser.error("out must be a fresh directory below the repository var directory")
    out.mkdir(parents=True, exist_ok=False)
    evidence = {"requested_seconds": args.seconds, "double_stop": args.double_stop,
                "kill_native_once": args.kill_native_once,
                 "observe_endpoint_changes": args.observe_endpoint_changes,
                "default_backend_changed": False, "physical_playback_requested": False,
                "scope": "real game transport/structure; no game pulse or audible office continuity proof",
                 "started_at": time.time(), "run_session_completed": False,
                 "media_checks_ok": False, "endpoint_checks_ok": False, "ok": False}
    settings_before = SETTINGS.read_bytes() if SETTINGS.exists() else None
    if settings_before is not None:
        (out / "settings-before.bin").write_bytes(settings_before)
    runtime_sdl_before = {}
    for name in ("SDL2.dll", "SDL2.dll.original"):
        path = GAME.parent / name
        runtime_sdl_before[name] = path.read_bytes() if path.exists() else None
        if runtime_sdl_before[name] is not None:
            (out / (name + ".before.bin")).write_bytes(runtime_sdl_before[name])
    baseline = None
    try:
        evidence["artifacts_before"] = artifact_snapshot()
        evidence["staged_sdl_before"] = sha(GAME.parent / "SDL2.dll") if (GAME.parent / "SDL2.dll").exists() else None
        baseline = endpoint_snapshot()
        write_json(out / "endpoint-before.json", baseline)
        write_json(out / "result.json", evidence)
        asyncio.run(run_session(args, out, evidence, baseline))
        evidence["run_session_completed"] = True
    except BaseException as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["traceback"] = traceback.format_exc()
    finally:
        try:
            settings_after = SETTINGS.read_bytes() if SETTINGS.exists() else None
            evidence["settings_restored_by_product"] = settings_after == settings_before
            remaining = vcmi_processes()
            evidence["remaining_vcmi_processes"] = remaining
            if settings_after != settings_before and not remaining:
                if settings_before is None:
                    SETTINGS.unlink(missing_ok=True)
                else:
                    SETTINGS.write_bytes(settings_before)
                evidence["settings_restored_by_harness"] = True
            evidence["settings_bytes_restored"] = (SETTINGS.read_bytes() if SETTINGS.exists() else None) == settings_before
            if not remaining:
                # session_start stages only this process-local DLL location.
                # Restore precisely the files this test observed before launch.
                for name, original in runtime_sdl_before.items():
                    path = GAME.parent / name
                    current = path.read_bytes() if path.exists() else None
                    if current != original:
                        if original is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_bytes(original)
                evidence["runtime_sdl_bytes_restored"] = all(
                    (GAME.parent / name).read_bytes() == data if data is not None else
                    not (GAME.parent / name).exists() for name, data in runtime_sdl_before.items())
            evidence["artifacts_after"] = artifact_snapshot()
            evidence["artifact_hashes_unchanged"] = evidence.get("artifacts_before") == evidence["artifacts_after"]
            evidence["staged_sdl_after"] = sha(GAME.parent / "SDL2.dll") if (GAME.parent / "SDL2.dll").exists() else None
            if baseline is not None:
                after = endpoint_snapshot()
                write_json(out / "endpoint-after.json", after)
                pids = {evidence[key]["pid"] for key in ("owned_game", "owned_recorder") if key in evidence}
                evidence["endpoint_after_comparison"] = endpoint_comparison(baseline, after, pids)
        except BaseException as exc:
            evidence["finalization_error"] = f"{type(exc).__name__}: {exc}"
        evidence.update(final_verdict(evidence))
        evidence["finished_at"] = time.time()
        write_json(out / "result.json", evidence)
    print(json.dumps({"ok": evidence["ok"], "media_checks_ok": evidence["media_checks_ok"],
                      "endpoint_checks_ok": evidence["endpoint_checks_ok"],
                      "exit_code": evidence["exit_code"], "evidence": str(out / "result.json"),
                      "error": evidence.get("error"), "finalization_error": evidence.get("finalization_error")},
                     ensure_ascii=False))
    return evidence["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
