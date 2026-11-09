"""Explicit LIVE MCP stdio recording acceptance; never part of offline runs.

Requires --execute and a new output directory. Refuses existing VCMI processes.
Uses existing display index 2 by default; does not configure displays or audio endpoints.
An optional JSONL mailbox accepts individual controller requests; it never chooses
game actions. No cheats, playback, automatic backend fallback, or default changes.
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
import zlib

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
    repeated_ok = ("recordings" not in evidence or
        len(evidence["recordings"]) == evidence.get("requested_recordings", 1) and
        all(row.get("checks_ok") is True for row in evidence["recordings"]))
    media_ok = bool(evidence.get("run_session_completed") is True and formal_stop_ok and repeated_ok and
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


def isolated_env(out: Path, default_path: bool = False) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AUTO_HEROS3_", "A3_", "A3CAPTURE_", "SDL_AUDIO", "SDL_DISKAUDIO"))}
    env.update({"PYTHONIOENCODING": "utf8",
                "AUTO_HEROS3_STATE_DIR": str(out / "state"),
                "AUTO_HEROS3_SAVES_DIR": str(out / "saves"),
                "AUTO_HEROS3_SAVE_SUBDIR": "native_e2e",
                "AUTO_HEROS3_RECORDINGS_DIR": str(out / "recordings"),
                "AUTO_HEROS3_MEMORY_ROOT": str(out / "player")})
    if not default_path:
        env["AUTO_HEROS3_AUDIO_DRIVER"] = "a3capture"
    return env


def recording_arguments(args) -> dict:
    """Default acceptance must omit defaults, rather than pass their expected values."""
    result = {} if args.default_path else {"backend": "libobs"}
    if args.rollover_seconds is not None:
        result["rollover_seconds"] = args.rollover_seconds
    elif not args.default_path:
        result["rollover_seconds"] = 10
    if args.max_segment_gb is not None:
        result["max_segment_gb"] = args.max_segment_gb
    return result


def session_arguments(target: str, display_index: str) -> dict:
    result = {"map_name": target}
    if display_index != "default":
        result["display_index"] = int(display_index)
    return result


def rollover_events(report: dict, required_reason: str | None = None) -> list[dict]:
    events = []
    for entry in report.get("output_events", []):
        if entry.get("name") != "output_rollover":
            continue
        fields = dict(part.strip().split("=", 1) for part in entry.get("detail", "").split(";")
                      if "=" in part)
        event = {"at_ns": entry.get("t_ns"), "reason": fields.get("reason")}
        for key in ("start_pts_usec", "boundary_pts_usec", "closed_bytes"):
            event[key] = int(fields[key])
        if event["boundary_pts_usec"] <= event["start_pts_usec"] or event["closed_bytes"] <= 0:
            raise ValueError("invalid native rollover event")
        events.append(event)
    if required_reason and not any(row["reason"] == required_reason for row in events):
        raise RuntimeError(f"required {required_reason} rollover was not observed")
    return events


def framed_identity(out: Path) -> dict:
    """Read the actual current source; never infer it from requested backend alone."""
    from dataclasses import asdict
    from a3_framed import parse_header
    state = json.loads((out / "state/recording.json").read_text(encoding="utf-8"))
    session = json.loads((out / "state/session.json").read_text(encoding="utf-8"))
    config = state.get("config", {})
    expected = (out / "state/a3capture.raw").resolve()
    actual = Path(config.get("audio_framed") or "").resolve()
    ready = Path(str(expected) + ".ready")
    if (actual != expected or config.get("audio_framed_live") is not True or
            not ready.is_file() or not any("audio_sink=a3capture " in note
                                          for note in session.get("notes", []))):
        raise RuntimeError("current session did not select the ready live a3capture source")
    with actual.open("rb") as handle:
        header = handle.read(128)
        packet = parse_header(header, 0)
        if packet.payload_bytes > 131072:
            raise RuntimeError("unexpected framed first packet size")
        payload = handle.read(packet.payload_bytes)
    if (len(payload) != packet.payload_bytes or
            zlib.crc32(payload) & 0xffffffff != packet.payload_crc32 or
            not packet.first or packet.sequence != 0 or packet.sample_start != 0):
        raise RuntimeError("invalid first packet identity/CRC")
    details = asdict(packet)
    details.pop("payload", None)
    return {"path": str(actual), "bytes_at_observation": actual.stat().st_size,
            "first_header_sha256": hashlib.sha256(header).hexdigest(),
            "first_header": details, "ready_path": str(ready),
            "ready_text": ready.read_text(encoding="utf-8", errors="replace"),
            "ready_sha256": sha(ready), "session_notes": session.get("notes", []),
            "recording_config": config}


CONTROL_READ_TOOLS = frozenset({"game_status", "game_observe", "game_events", "game_brief",
    "game_action_status", "game_reconcile", "battle_actions", "battle_assess", "town_info",
    "game_screenshot"})
CONTROL_ACTIONS = frozenset({"move", "move_hero", "build", "recruit", "recruit_hero",
    "open_hero", "transfer", "merge", "swap_stacks", "split", "upgrade", "market_trade",
    "answer", "end_turn", "cast_spell", "battle_cast_spell", "battle_attack",
    "battle_approach", "battle_move", "battle_defend", "battle_wait", "battle_result_ok",
    "confirm_deployment", "close_window", "center_hero", "click"})


class ControlMailbox:
    """One ordered controller, one transport, durable dispatch log, no automatic retry."""
    def __init__(self, out: Path):
        self.out = out
        self.path = out / "control-requests.jsonl"
        self.path.touch(exist_ok=False)
        self.results = out / "control-results.jsonl"
        self.offset = 0
        self.seen = set()
        self.action_requests = set()
        self.fence = None
        self.recording_index = None
        self.publish("waiting_for_recording")

    def publish(self, state: str) -> None:
        write_json(self.out / "control-state.json", {"state": state, "at": time.time(),
            "recording_index": self.recording_index, "fence": self.fence,
            "request_file": str(self.path), "result_file": str(self.results),
            "tools": sorted(CONTROL_READ_TOOLS | {"game_act", "game_note"})})

    def append(self, row: dict) -> None:
        with self.results.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def next_request(self):
        with self.path.open("rb") as handle:
            if handle.seek(0, 2) < self.offset:
                raise RuntimeError("control mailbox was truncated")
            handle.seek(self.offset)
            line = handle.readline(131073)
            if not line or not line.endswith(b"\n"):
                if len(line) > 131072:
                    raise RuntimeError("control request exceeds 128 KiB")
                return None
            self.offset = handle.tell()
        return json.loads(line.decode("utf-8-sig"))

    def validate(self, request: dict) -> tuple[str, str, dict]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        rid, tool, arguments = request.get("id"), request.get("tool"), request.get("arguments", {})
        if not isinstance(rid, str) or not rid.strip() or len(rid) > 160:
            raise ValueError("nonempty id required, at most 160 characters")
        if rid in self.seen:
            raise ValueError("duplicate control id; request will not be dispatched again")
        self.seen.add(rid)
        if tool not in CONTROL_READ_TOOLS | {"game_act", "game_note"}:
            raise ValueError("tool is outside the controller allowlist")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        arguments = dict(arguments)
        if tool == "game_act":
            action_id = arguments.get("request_id")
            if self.fence:
                raise ValueError("unresolved game action; read/reconcile before further mutations")
            if (arguments.get("action") not in CONTROL_ACTIONS or
                    not isinstance(action_id, str) or not action_id.strip()):
                raise ValueError("whitelisted game action and nonempty request_id required")
            if action_id in self.action_requests:
                raise ValueError("duplicate game request_id; use status/reconcile instead")
            self.action_requests.add(action_id)
        if tool == "game_screenshot":
            target = Path(arguments.get("path") or self.out / "shots" / f"{len(self.seen):04d}.png").resolve()
            if not target.is_relative_to((self.out / "shots").resolve()):
                raise ValueError("screenshots must stay below the run shots directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            arguments["path"] = str(target)
        return rid, tool, arguments

    def apply_result(self, tool: str, arguments: dict, result: dict) -> None:
        if tool == "game_act" and result.get("status") not in ("completed", "rejected"):
            self.fence = {"request_id": arguments["request_id"], "action_id": result.get("action_id"),
                          "status": result.get("status", "unknown")}
        if not self.fence:
            return
        resolved = None
        if tool == "game_reconcile" and arguments.get("request_id") == self.fence["request_id"]:
            if result.get("resolved") is True:
                resolved = result.get("result")
        elif tool == "game_action_status" and arguments.get("action_id") == self.fence.get("action_id"):
            if self.fence.get("action_id") and result.get("ok") is True:
                resolved = result.get("action")
        if isinstance(resolved, dict) and resolved.get("status") in ("completed", "rejected"):
            self.fence = None

    async def dispatch_one(self, call) -> None:
        request = None
        try:
            request = self.next_request()
            if request is None:
                return
            rid, tool, arguments = self.validate(request)
        except (ValueError, TypeError) as exc:
            self.append({"at": time.time(), "state": "rejected", "request": request, "error": str(exc)})
            return
        self.append({"at": time.time(), "id": rid, "state": "dispatching",
                     "tool": tool, "arguments": arguments, "recording_index": self.recording_index})
        try:
            result = await call(tool, arguments, timeout=90)
            self.apply_result(tool, arguments, result)
            self.append({"at": time.time(), "id": rid, "state": "returned", "result": result})
        except Exception as exc:
            if tool == "game_act":
                self.fence = {"request_id": arguments["request_id"], "action_id": None, "status": "unknown"}
            self.append({"at": time.time(), "id": rid, "state": "unknown", "error": str(exc)})
        self.publish("recording")


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

    env = isolated_env(out, args.default_path)
    mailbox = ControlMailbox(out) if args.control_requests else None
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
                        try:
                            return tool_payload(result)
                        except ValueError:
                            if name != "game_screenshot" or not Path(arguments["path"]).is_file():
                                raise
                            target = Path(arguments["path"])
                            sidecar = Path(str(target) + ".json")
                            return {"ok": True, "path": str(target), "sha256": sha(target),
                                    "sidecar": json.loads(sidecar.read_text(encoding="utf-8"))
                                    if sidecar.is_file() else None}
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
                    session_args = session_arguments(target, args.display_index)
                    evidence["session_arguments"] = session_args
                    start = await call("session_start", session_args, timeout=360)
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
                    for recording_index in range(2 if args.two_recordings else 1):
                        recording_attempted = True
                        stopped = False
                        arguments = recording_arguments(args)
                        evidence["recording_arguments"] = arguments
                        recording = await call("recording_start", arguments)
                        evidence["recording_start"] = recording
                        if recording.get("ok") is not True or recording.get("backend") != "libobs":
                            raise RuntimeError("native recording_start failed")
                        result_dir = Path(recording["dir"]).resolve()
                        if not result_dir.is_relative_to(out / "recordings"):
                            raise RuntimeError("recorder output escaped test directory")
                        if any(Path(row["dir"]) == result_dir for row in evidence.get("recordings", [])):
                            raise RuntimeError("second recording reused the first recording directory")
                        owned_recorder = process_identity(int(recording["pid"]), ARTIFACTS["recorder"])
                        owned_pids.add(owned_recorder["pid"])
                        evidence["owned_recorder"] = owned_recorder
                        evidence["framed_identity"] = framed_identity(out)
                        write_json(out / f"framed-identity-{recording_index:02d}.json", evidence["framed_identity"])
                        if mailbox:
                            mailbox.recording_index = recording_index
                            mailbox.publish("recording")
                        started = time.monotonic()
                        killed_once = False
                        next_observation = started
                        while time.monotonic() - started < args.seconds:
                            if mailbox:
                                await mailbox.dispatch_one(call)
                            if time.monotonic() < next_observation:
                                await asyncio.sleep(.2)
                                continue
                            next_observation = time.monotonic() + 5
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
                            await asyncio.sleep(min(.2 if mailbox else 5,
                                                    max(0, args.seconds - (time.monotonic() - started))))
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
                        events = rollover_events(report, args.require_rollover_reason)
                        evidence["rollover_events"] = events
                        audio = report.get("media", {}).get("audio", {})
                        none = audio.get("monitoring_type_none_value")
                        evidence["monitoring"] = audio
                        if (none != 0 or audio.get("source_monitoring_type") != none or
                                audio.get("raw_audio_source_monitoring_type") != none):
                            raise RuntimeError("monitoring NONE was not observed")
                        if not evidence["tail_verifier"].get("v2_ok"):
                            raise RuntimeError("v2 tail/interval contract failed")
                        observe("recording_stopped")
                        evidence.setdefault("recordings", []).append({"index": recording_index,
                            "dir": str(result_dir), "game_identity": dict(owned_game),
                            "recorder_identity": dict(owned_recorder), "arguments": arguments,
                            "start": recording, "stop": replies, "tail_verifier": evidence["tail_verifier"],
                            "framed_identity": evidence["framed_identity"], "monitoring": audio,
                            "wall_seconds": evidence["recording_wall_seconds"],
                            "rollover_events": events, "checks_ok": True})
                        write_json(out / "recordings.json", evidence["recordings"])
                        if mailbox:
                            mailbox.publish("between_recordings")
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
                    if mailbox:
                        mailbox.publish("finished")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--execute", action="store_true", help="explicitly start the live game and recording")
    parser.add_argument("--seconds", type=int, default=60, help="duration per recording, 1..3600 seconds")
    parser.add_argument("--default-path", action="store_true",
                        help="remove audio overrides and omit recording_start backend/rollover defaults")
    parser.add_argument("--rollover-seconds", type=float,
                        help="explicit test threshold; omitted in default-path acceptance")
    parser.add_argument("--max-segment-gb", type=float, help="explicit size test cap, e.g. 0.02")
    parser.add_argument("--two-recordings", action="store_true", help="two recordings in the same game session")
    parser.add_argument("--control-requests", action="store_true",
                        help="accept explicit controller requests via out/control-requests.jsonl")
    parser.add_argument("--display-index", default="2",
                        help="existing display index, or 'default' to omit the session argument; never changes topology")
    parser.add_argument("--require-rollover-reason", choices=("TIME", "SIZE"),
                        help="require an actual native rollover reason, not just multiple clips")
    parser.add_argument("--double-stop", action="store_true")
    parser.add_argument("--kill-native-once", action="store_true",
                        help="terminate only the verified owned native PID halfway through; require watchdog recovery")
    parser.add_argument("--observe-endpoint-changes", action="store_true",
                        help="record environment changes without aborting media checks; owned physical audio still aborts")
    parser.add_argument("--save", type=Path, help="existing .vsgm1 copied into isolated saves")
    args = parser.parse_args()
    if not args.execute:
        parser.error("LIVE test requires --execute; no game or recorder started")
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be between 1 and 3600")
    if args.rollover_seconds is not None and not 0 < args.rollover_seconds <= 3600:
        parser.error("--rollover-seconds must be positive and at most 3600")
    if args.max_segment_gb is not None and not 0 < args.max_segment_gb <= 1.5:
        parser.error("--max-segment-gb must be positive and at most 1.5")
    if args.display_index != "default" and not args.display_index.isdecimal():
        parser.error("--display-index must be a nonnegative integer or default")
    if vcmi_processes():
        parser.error("existing VCMI process detected; stop it separately before this test")
    out = args.out.resolve()
    allowed = (ROOT / "var").resolve()
    if not out.is_relative_to(allowed) or out == allowed:
        parser.error("out must be a fresh directory below the repository var directory")
    out.mkdir(parents=True, exist_ok=False)
    evidence = {"requested_seconds": args.seconds, "double_stop": args.double_stop,
                "requested_recordings": 2 if args.two_recordings else 1,
                "default_path": args.default_path, "control_requests": args.control_requests,
                "display_index_argument": args.display_index,
                "required_rollover_reason": args.require_rollover_reason,
                "rollover_seconds_argument": args.rollover_seconds,
                "max_segment_gb_argument": args.max_segment_gb,
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
    for name in ("SDL2.dll", "SDL2.dll.original", ".a3-sdl-deployment.json"):
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
                evidence["runtime_sdl_bytes_restored_by_product"] = all(
                    (GAME.parent / name).read_bytes() == data if data is not None else
                    not (GAME.parent / name).exists() for name, data in runtime_sdl_before.items())
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
