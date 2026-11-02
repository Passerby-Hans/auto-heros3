# -*- coding: utf-8 -*-
"""AV.1b display lifecycle: pure ownership, snapshot and classification contracts.

Why this module exists
----------------------
The first P1 install scripts conflated a hardware ID (``Root\\MttVDD``) with a PnP instance
ID (``ROOT\\DISPLAY\\0002``), treated an exit code as proof of removal, and fed a NULL DEVMODE
to ``ChangeDisplaySettingsEx`` as if it were a detach request.  This module makes those
mistakes hard to repeat:

* observations are validated snapshots (one primary, unique IDs, complete flag);
* an ownership manifest names the exact instance/display this project owns, so an existing or
  concurrent device can never be silently adopted or rolled back;
* action receipts are recorded separately from post-state; a zero exit code never proves a
  target was installed/removed;
* classification is pure data: no Win32 calls, no subprocesses, no display/device mutation.

The collector (``tools/av1b_display_probe.py``) is read-only.  Lifecycle changes remain a
separate, explicitly authorized maintenance action.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "av1b-lifecycle/1"
PROJECT = "auto-heros3"
DEFAULT_HARDWARE_ID = r"Root\MttVDD"
DEFAULT_PROVIDER = "MikeTheTech"
DEFAULT_INF_NAMES = ("mttvdd.inf",)
READY_STATUS = "ok"
NONE_PROBLEMS = {"", "cm_prob_none", "0", "none"}


class LifecycleError(ValueError):
    """Base error for malformed lifecycle data or an impossible transition."""


class SnapshotError(LifecycleError):
    """The observation is incomplete or mutually inconsistent."""


class ManifestError(LifecycleError):
    """The ownership manifest is malformed, stale, or does not own the target."""


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


@dataclass(frozen=True)
class CandidateSpec:
    """What we are allowed to call *our* VDD family (never a display name only)."""

    hardware_id: str = DEFAULT_HARDWARE_ID
    provider: str = DEFAULT_PROVIDER
    inf_names: tuple[str, ...] = DEFAULT_INF_NAMES

    def matches(self, device: "Device") -> bool:
        wanted = _norm(self.hardware_id)
        if wanted not in {_norm(item) for item in device.hardware_ids}:
            return False
        if self.provider and _norm(self.provider) != _norm(device.provider):
            return False
        if self.inf_names:
            names = {_norm(item) for item in self.inf_names}
            if _norm(device.inf_name) not in names and _norm(device.original_inf) not in names:
                return False
        return True


DEFAULT_CANDIDATE = CandidateSpec()


@dataclass(frozen=True)
class Device:
    instance_id: str
    hardware_ids: tuple[str, ...] = ()
    friendly_name: str = ""
    inf_name: str = ""
    original_inf: str = ""
    provider: str = ""
    driver_version: str = ""
    present: bool = True
    status: str = ""
    problem: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Device":
        instance_id = _as_str(data.get("instance_id")).strip()
        if not instance_id:
            raise SnapshotError("device.instance_id is required")
        hardware_ids = tuple(_as_str(item).strip() for item in _as_list(data.get("hardware_ids"))
                             if _as_str(item).strip())
        return cls(
            instance_id=instance_id,
            hardware_ids=hardware_ids,
            friendly_name=_as_str(data.get("friendly_name")),
            inf_name=_as_str(data.get("inf_name")),
            original_inf=_as_str(data.get("original_inf")),
            provider=_as_str(data.get("provider")),
            driver_version=_as_str(data.get("driver_version")),
            present=_as_bool(data.get("present"), True),
            status=_as_str(data.get("status")),
            problem=_as_str(data.get("problem")),
        )

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "hardware_ids": list(self.hardware_ids),
            "friendly_name": self.friendly_name,
            "inf_name": self.inf_name,
            "original_inf": self.original_inf,
            "provider": self.provider,
            "driver_version": self.driver_version,
            "present": self.present,
            "status": self.status,
            "problem": self.problem,
        }

    def is_ready(self) -> bool:
        return (self.present and _norm(self.status) in {"ok", "started", "running"}
                and _norm(self.problem) in NONE_PROBLEMS)


@dataclass(frozen=True)
class Display:
    device: str
    primary: bool = False
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    refresh_num: int = 0
    refresh_den: int = 1
    monitor_device_path: str = ""
    adapter_instance_id: str = ""
    active_path: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Display":
        device = _as_str(data.get("device")).strip()
        if not device:
            raise SnapshotError("display.device is required")
        den = _as_int(data.get("refresh_den"), 1)
        if den <= 0:
            raise SnapshotError("display.refresh_den must be positive")
        return cls(
            device=device,
            primary=_as_bool(data.get("primary"), False),
            x=_as_int(data.get("x")),
            y=_as_int(data.get("y")),
            width=_as_int(data.get("width")),
            height=_as_int(data.get("height")),
            refresh_num=_as_int(data.get("refresh_num")),
            refresh_den=den,
            monitor_device_path=_as_str(data.get("monitor_device_path")),
            adapter_instance_id=_as_str(data.get("adapter_instance_id")),
            active_path=_as_bool(data.get("active_path"), True),
        )

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "primary": self.primary,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "refresh_num": self.refresh_num,
            "refresh_den": self.refresh_den,
            "monitor_device_path": self.monitor_device_path,
            "adapter_instance_id": self.adapter_instance_id,
            "active_path": self.active_path,
        }

    def mode(self) -> dict:
        return {"width": self.width, "height": self.height,
                "refresh_num": self.refresh_num, "refresh_den": self.refresh_den}

    def belongs_to(self, instance_id: str) -> bool:
        return bool(self.adapter_instance_id) and _norm(self.adapter_instance_id) == _norm(instance_id)


@dataclass(frozen=True)
class Snapshot:
    captured_at: str = ""
    complete: bool = True
    devices: tuple[Device, ...] = ()
    displays: tuple[Display, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Snapshot":
        if not isinstance(data, Mapping):
            raise SnapshotError("snapshot must be an object")
        devices = []
        for index, item in enumerate(_as_list(data.get("devices"))):
            if not isinstance(item, Mapping):
                raise SnapshotError(f"device[{index}] must be an object")
            devices.append(Device.from_mapping(item))
        displays = []
        for index, item in enumerate(_as_list(data.get("displays"))):
            if not isinstance(item, Mapping):
                raise SnapshotError(f"display[{index}] must be an object")
            displays.append(Display.from_mapping(item))
        return cls(
            captured_at=_as_str(data.get("captured_at")),
            complete=_as_bool(data.get("complete"), True),
            devices=tuple(devices),
            displays=tuple(displays),
            warnings=tuple(_as_str(item) for item in _as_list(data.get("warnings"))),
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "captured_at": self.captured_at,
            "complete": self.complete,
            "devices": [item.to_dict() for item in self.devices],
            "displays": [item.to_dict() for item in self.displays],
            "warnings": list(self.warnings),
        }

    def device(self, instance_id: str | None) -> Device | None:
        if not instance_id:
            return None
        wanted = _norm(instance_id)
        for item in self.devices:
            if _norm(item.instance_id) == wanted:
                return item
        return None

    def display(self, device_name: str | None) -> Display | None:
        if not device_name:
            return None
        wanted = _norm(device_name)
        for item in self.displays:
            if _norm(item.device) == wanted:
                return item
        return None

    def primary(self) -> Display | None:
        primary = [item for item in self.displays if item.primary]
        return primary[0] if len(primary) == 1 else None

    def matching_devices(self, candidate: CandidateSpec = DEFAULT_CANDIDATE) -> list[Device]:
        return [item for item in self.devices if item.present and candidate.matches(item)]

    def matching_displays(self, instance_id: str) -> list[Display]:
        return [item for item in self.displays
                if item.active_path and item.belongs_to(instance_id)]

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.complete:
            errors.append("snapshot is marked incomplete")
        ids = [_norm(item.instance_id) for item in self.devices]
        if len(ids) != len(set(ids)):
            errors.append("duplicate device instance_id")
        names = [_norm(item.device) for item in self.displays]
        if len(names) != len(set(names)):
            errors.append("duplicate display device name")
        active = [item for item in self.displays if item.active_path]
        if not active:
            errors.append("no active display path")
        primary = [item for item in active if item.primary]
        if len(primary) != 1:
            errors.append(f"expected exactly one active primary display, got {len(primary)}")
        for item in active:
            if item.width <= 0 or item.height <= 0:
                errors.append(f"display {item.device} has a non-positive mode")
                break
        return errors

    def validate(self) -> None:
        errors = self.validation_errors()
        if errors:
            raise SnapshotError("; ".join(errors))


def load_snapshot(path: str | Path) -> Snapshot:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"cannot read snapshot {path}: {exc}") from exc
    return Snapshot.from_mapping(data)


def _primary_signature(snapshot: Snapshot) -> tuple:
    primary = snapshot.primary()
    if primary is None:
        return ()
    # x/y are logical desktop coordinates (DPI virtualization); the stable identity is the
    # device/monitor + native mode, not a logical position.
    return (primary.device, primary.monitor_device_path, primary.width, primary.height,
            primary.refresh_num, primary.refresh_den)


@dataclass(frozen=True)
class OwnershipManifest:
    run_id: str
    device_instance_id: str
    hardware_ids: tuple[str, ...]
    inf_name: str = ""
    original_inf: str = ""
    provider: str = ""
    driver_version: str = ""
    display_device: str = ""
    monitor_device_path: str = ""
    package_sha256: str = ""
    adopted_existing: bool = False
    created_at: str = ""
    project: str = PROJECT
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "OwnershipManifest":
        if not isinstance(data, Mapping):
            raise ManifestError("manifest must be an object")
        run_id = _as_str(data.get("run_id")).strip()
        instance_id = _as_str(data.get("device_instance_id")).strip()
        hardware_ids = tuple(_as_str(item).strip() for item in _as_list(data.get("hardware_ids"))
                             if _as_str(item).strip())
        if not run_id:
            raise ManifestError("manifest.run_id is required")
        if not instance_id:
            raise ManifestError("manifest.device_instance_id is required")
        if not hardware_ids:
            raise ManifestError("manifest.hardware_ids is required")
        return cls(
            run_id=run_id,
            device_instance_id=instance_id,
            hardware_ids=hardware_ids,
            inf_name=_as_str(data.get("inf_name")),
            original_inf=_as_str(data.get("original_inf")),
            provider=_as_str(data.get("provider")),
            driver_version=_as_str(data.get("driver_version")),
            display_device=_as_str(data.get("display_device")),
            monitor_device_path=_as_str(data.get("monitor_device_path")),
            package_sha256=_as_str(data.get("package_sha256")),
            adopted_existing=_as_bool(data.get("adopted_existing"), False),
            created_at=_as_str(data.get("created_at")),
            project=_as_str(data.get("project"), PROJECT),
            schema_version=_as_str(data.get("schema_version"), SCHEMA_VERSION),
        )

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot, *, run_id: str,
                      candidate: CandidateSpec = DEFAULT_CANDIDATE,
                      display_device: str | None = None,
                      package_sha256: str = "", adopted_existing: bool = False,
                      created_at: str | None = None) -> "OwnershipManifest":
        snapshot.validate()
        matches = snapshot.matching_devices(candidate)
        if len(matches) != 1:
            raise ManifestError(
                f"expected exactly one matching candidate device, got {len(matches)}")
        device = matches[0]
        if display_device:
            display = snapshot.display(display_device)
            if display is None:
                raise ManifestError(f"display {display_device!r} not present in snapshot")
        else:
            owned = snapshot.matching_displays(device.instance_id)
            if len(owned) != 1:
                raise ManifestError(
                    f"need an explicit display target or exactly one display mapped to "
                    f"{device.instance_id}, got {len(owned)}")
            display = owned[0]
        if not display.active_path:
            raise ManifestError(f"display {display.device!r} is not an active path")
        return cls(
            run_id=run_id,
            device_instance_id=device.instance_id,
            hardware_ids=device.hardware_ids,
            inf_name=device.inf_name,
            original_inf=device.original_inf,
            provider=device.provider,
            driver_version=device.driver_version,
            display_device=display.device,
            monitor_device_path=display.monitor_device_path,
            package_sha256=package_sha256,
            adopted_existing=adopted_existing,
            created_at=created_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "run_id": self.run_id,
            "device_instance_id": self.device_instance_id,
            "hardware_ids": list(self.hardware_ids),
            "inf_name": self.inf_name,
            "original_inf": self.original_inf,
            "provider": self.provider,
            "driver_version": self.driver_version,
            "display_device": self.display_device,
            "monitor_device_path": self.monitor_device_path,
            "package_sha256": self.package_sha256,
            "adopted_existing": self.adopted_existing,
            "created_at": self.created_at,
        }

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ManifestError(f"unsupported manifest schema {self.schema_version!r}")
        if self.project != PROJECT:
            raise ManifestError(f"manifest belongs to project {self.project!r}")
        if not self.run_id or not self.device_instance_id or not self.hardware_ids:
            raise ManifestError("manifest is missing run/instance/hardware identity")
        if not self.provider:
            raise ManifestError("manifest.provider is required")
        if not (self.inf_name or self.original_inf):
            raise ManifestError("manifest needs inf_name/original_inf")

    def owns_device(self, device: Device | None) -> bool:
        if device is None:
            return False
        if _norm(device.instance_id) != _norm(self.device_instance_id):
            return False
        actual = {_norm(item) for item in device.hardware_ids}
        required = {_norm(item) for item in self.hardware_ids}
        return required.issubset(actual)

    def owns_display(self, display: Display | None) -> bool:
        if display is None or not self.display_device:
            return False
        return (_norm(display.device) == _norm(self.display_device)
                and display.active_path
                and (not display.adapter_instance_id
                     or _norm(display.adapter_instance_id) == _norm(self.device_instance_id)))


def write_manifest(path: str | Path, manifest: OwnershipManifest) -> Path:
    """Atomic manifest write; this is the only file write in this module."""
    target = Path(path)
    manifest.validate()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
    return target


def load_manifest(path: str | Path) -> OwnershipManifest:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    manifest = OwnershipManifest.from_mapping(data)
    manifest.validate()
    return manifest


@dataclass(frozen=True)
class CommandRecord:
    argv: tuple[str, ...]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    started_at: str = ""
    finished_at: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CommandRecord":
        return cls(
            argv=tuple(_as_str(item) for item in _as_list(data.get("argv"))),
            exit_code=(None if data.get("exit_code") is None
                       else _as_int(data.get("exit_code"))),
            stdout=_as_str(data.get("stdout")),
            stderr=_as_str(data.get("stderr")),
            started_at=_as_str(data.get("started_at")),
            finished_at=_as_str(data.get("finished_at")),
        )

    def to_dict(self) -> dict:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class DevconReceipt:
    exit_code: int | None
    removed_count: int = 0
    no_devices: bool = False
    requires_reboot: bool = False
    installed: bool = False
    raw: str = ""

    @classmethod
    def parse(cls, command: CommandRecord | None) -> "DevconReceipt | None":
        if command is None:
            return None
        raw = "\n".join(part for part in (command.stdout, command.stderr) if part)
        removed_counts = [int(item) for item in re.findall(
            r"(\d+)\s+device\(s\)\s+were\s+removed", raw, flags=re.IGNORECASE)]
        no_devices = bool(re.search(r"no\s+devices?\s+were\s+removed", raw, flags=re.IGNORECASE))
        reboot = bool(re.search(
            r"requires?\s+(?:a\s+)?(?:reboot|restart)|"
            r"(?:reboot|restart)\s+(?:is\s+)?required", raw, flags=re.IGNORECASE))
        installed = bool(re.search(
            r"drivers?\s+installed\s+successfully|install(?:ed|ation)?\s+is\s+complete|"
            r"device\s+node\s+created", raw, flags=re.IGNORECASE))
        return cls(exit_code=command.exit_code,
                   removed_count=max(removed_counts, default=0),
                   no_devices=no_devices,
                   requires_reboot=reboot,
                   installed=installed,
                   raw=raw)

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "removed_count": self.removed_count,
            "no_devices": self.no_devices,
            "requires_reboot": self.requires_reboot,
            "installed": self.installed,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class Decision:
    action: str
    status: str
    ok: bool
    reasons: tuple[str, ...] = ()
    device_instance_id: str = ""
    display_device: str = ""
    receipt: DevconReceipt | None = None
    before: Snapshot | None = None
    after: Snapshot | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "status": self.status,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "device_instance_id": self.device_instance_id,
            "display_device": self.display_device,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
        }


def _invalid(action: str, reasons: Sequence[str]) -> Decision:
    return Decision(action=action, status="snapshot_invalid", ok=False,
                    reasons=tuple(reasons))


def _validate_pair(before: Snapshot, after: Snapshot) -> list[str]:
    errors = []
    for label, snapshot in (("before", before), ("after", after)):
        for error in snapshot.validation_errors():
            errors.append(f"{label}: {error}")
    return errors


def _candidate_reason(device: Device, candidate: CandidateSpec) -> str:
    return (f"candidate {device.instance_id} hardware={list(device.hardware_ids)} "
            f"provider={device.provider!r} inf={device.inf_name!r}/{device.original_inf!r}")


def classify_install(before: Snapshot, after: Snapshot, *,
                     candidate: CandidateSpec = DEFAULT_CANDIDATE,
                     command: CommandRecord | None = None,
                     expected_mode: Mapping[str, int] | None = None,
                     display_device: str | None = None) -> Decision:
    """Classify an install using observations, never the command exit code alone."""
    errors = _validate_pair(before, after)
    if errors:
        return _invalid("install", errors)
    receipt = DevconReceipt.parse(command)
    before_matches = before.matching_devices(candidate)
    if len(before_matches) > 1:
        return Decision("install", "ambiguous_existing", False,
                        (f"{len(before_matches)} candidate devices already present",),
                        receipt=receipt, before=before, after=after)
    if len(before_matches) == 1:
        existing = before_matches[0]
        same = after.device(existing.instance_id)
        if same is not None and same.present:
            return Decision("install", "already_present", True,
                            ("candidate device was already present; no install should be repeated",
                             _candidate_reason(same, candidate)),
                            device_instance_id=same.instance_id,
                            receipt=receipt, before=before, after=after)
        return Decision("install", "unexpected_device_change", False,
                        ("candidate existed before but is missing/not present after",),
                        device_instance_id=existing.instance_id,
                        receipt=receipt, before=before, after=after)
    before_ids = {_norm(item.instance_id) for item in before.devices}
    new_devices = [item for item in after.devices if _norm(item.instance_id) not in before_ids]
    matches = [item for item in new_devices if candidate.matches(item)]
    if len(matches) > 1:
        return Decision("install", "ambiguous_new_devices", False,
                        tuple(_candidate_reason(item, candidate) for item in matches),
                        receipt=receipt, before=before, after=after)
    if not matches:
        status = "reboot_required" if receipt and receipt.requires_reboot else "install_failed"
        reasons = ["no new candidate device in the after snapshot"]
        if receipt:
            reasons.append(f"command exit={receipt.exit_code!r} installed={receipt.installed} "
                           f"no_devices={receipt.no_devices} reboot={receipt.requires_reboot}")
        return Decision("install", status, False, tuple(reasons),
                        receipt=receipt, before=before, after=after)
    device = matches[0]
    if receipt and receipt.requires_reboot:
        return Decision("install", "reboot_required", False,
                        ("driver receipt reports a reboot/restart requirement",
                         _candidate_reason(device, candidate)),
                        device_instance_id=device.instance_id,
                        receipt=receipt, before=before, after=after)
    if not device.is_ready():
        return Decision("install", "device_not_ready", False,
                        (f"new device {device.instance_id} present={device.present} "
                         f"status={device.status!r} problem={device.problem!r}",),
                        device_instance_id=device.instance_id,
                        receipt=receipt, before=before, after=after)
    if _primary_signature(before) != _primary_signature(after):
        return Decision("install", "primary_changed", False,
                        (f"primary changed {_primary_signature(before)} -> "
                         f"{_primary_signature(after)}",),
                        device_instance_id=device.instance_id,
                        receipt=receipt, before=before, after=after)
    owned_displays = [item for item in after.displays
                      if item.active_path and item.belongs_to(device.instance_id)]
    if display_device:
        named = after.display(display_device)
        if named is None or not named.active_path:
            return Decision("install", "display_missing", False,
                            (f"expected display {display_device!r} is absent/inactive",),
                            device_instance_id=device.instance_id,
                            receipt=receipt, before=before, after=after)
        if named.adapter_instance_id and not named.belongs_to(device.instance_id):
            return Decision("install", "display_mismatch", False,
                            (f"display {named.device} is mapped to "
                             f"{named.adapter_instance_id}, not {device.instance_id}",),
                            device_instance_id=device.instance_id,
                            receipt=receipt, before=before, after=after)
        display = named
    else:
        if len(owned_displays) != 1:
            return Decision("install", "installed_no_display" if not owned_displays
                            else "ambiguous_display", False,
                            (f"expected exactly one active display mapped to "
                             f"{device.instance_id}, got {len(owned_displays)}",),
                            device_instance_id=device.instance_id,
                            receipt=receipt, before=before, after=after)
        display = owned_displays[0]
    if display.primary:
        return Decision("install", "primary_changed", False,
                        (f"new display {display.device} became primary",),
                        device_instance_id=device.instance_id,
                        display_device=display.device, receipt=receipt,
                        before=before, after=after)
    if expected_mode:
        expected = {key: _as_int(value) for key, value in expected_mode.items()}
        actual = display.mode()
        mismatch = {key: (expected[key], actual.get(key)) for key in expected
                    if actual.get(key) != expected[key]}
        if mismatch:
            return Decision("install", "display_mode_mismatch", False,
                            (f"mode mismatch {mismatch}",),
                            device_instance_id=device.instance_id,
                            display_device=display.device, receipt=receipt,
                            before=before, after=after)
    reasons = [_candidate_reason(device, candidate),
               f"new display {display.device} is active, non-primary, mapped to {device.instance_id}"]
    if receipt:
        reasons.append(f"command receipt exit={receipt.exit_code!r} (not used as proof)")
    return Decision("install", "installed", True, tuple(reasons),
                    device_instance_id=device.instance_id,
                    display_device=display.device, receipt=receipt,
                    before=before, after=after)


def _manifest_ownership_errors(snapshot: Snapshot, manifest: OwnershipManifest,
                               candidate: CandidateSpec,
                               require_display: bool = True) -> list[str]:
    try:
        manifest.validate()
    except ManifestError as exc:
        return [f"manifest invalid: {exc}"]
    errors = []
    device = snapshot.device(manifest.device_instance_id)
    if device is None or not device.present:
        errors.append(f"owned device {manifest.device_instance_id} is absent from snapshot")
        return errors
    if not manifest.owns_device(device):
        errors.append(f"manifest does not own device {device.instance_id}")
    if not candidate.matches(device):
        errors.append(f"device {device.instance_id} is not a candidate VDD")
    if require_display and manifest.display_device:
        display = snapshot.display(manifest.display_device)
        if display is None:
            errors.append(f"owned display {manifest.display_device!r} is absent from snapshot")
        elif not manifest.owns_display(display):
            errors.append(f"manifest display {display.device} is inactive or mapped elsewhere")
    return errors


def classify_rollback(before: Snapshot, after: Snapshot, manifest: OwnershipManifest, *,
                      candidate: CandidateSpec = DEFAULT_CANDIDATE,
                      command: CommandRecord | None = None) -> Decision:
    """Classify rollback using the owned instance + post-state; exit 0 is not proof."""
    errors = _validate_pair(before, after)
    if errors:
        return _invalid("rollback", errors)
    receipt = DevconReceipt.parse(command)
    ownership_errors = _manifest_ownership_errors(before, manifest, candidate)
    if ownership_errors:
        return Decision("rollback", "refused_unowned", False, tuple(ownership_errors),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, receipt=receipt,
                        before=before, after=after)
    after_device = after.device(manifest.device_instance_id)
    if after_device is not None and after_device.present:
        reasons = [f"owned device {manifest.device_instance_id} is still present after rollback"]
        if receipt and receipt.no_devices:
            reasons.append("DevCon receipt says no devices were removed")
        if receipt and receipt.exit_code == 0:
            reasons.append("exit 0 is not proof; post-state still has the device")
        return Decision("rollback", "rollback_failed", False, tuple(reasons),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, receipt=receipt,
                        before=before, after=after)
    if _primary_signature(before) != _primary_signature(after):
        return Decision("rollback", "rollback_failed", False,
                        (f"primary changed during rollback {_primary_signature(before)} -> "
                         f"{_primary_signature(after)}",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, receipt=receipt,
                        before=before, after=after)
    if receipt and receipt.requires_reboot:
        return Decision("rollback", "reboot_required", False,
                        ("target is gone but the receipt reports a pending reboot/restart; "
                         "rollback is not verified until the post-reboot bus state is checked",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, receipt=receipt,
                        before=before, after=after)
    if manifest.display_device:
        display = after.display(manifest.display_device)
        if display is not None and display.active_path:
            return Decision("rollback", "rollback_partial", False,
                            (f"device is gone but owned display {display.device} is still active",),
                            device_instance_id=manifest.device_instance_id,
                            display_device=manifest.display_device, receipt=receipt,
                            before=before, after=after)
    else:
        leftovers = after.matching_displays(manifest.device_instance_id)
        if leftovers:
            return Decision("rollback", "rollback_partial", False,
                            (f"device is gone but display(s) still map to it: "
                             f"{[item.device for item in leftovers]}",),
                            device_instance_id=manifest.device_instance_id,
                            receipt=receipt, before=before, after=after)
    reasons = [f"owned device {manifest.device_instance_id} is no longer present",
               "primary display signature unchanged"]
    if receipt and receipt.no_devices:
        reasons.append("post-state proves removal despite receipt 'no devices' (state wins)")
    if receipt and receipt.exit_code == 0:
        reasons.append("verified by post-state, not by exit 0")
    return Decision("rollback", "rollback_verified", True, tuple(reasons),
                    device_instance_id=manifest.device_instance_id,
                    display_device=manifest.display_device, receipt=receipt,
                    before=before, after=after)


def classify_detach(before: Snapshot, after: Snapshot, manifest: OwnershipManifest, *,
                    candidate: CandidateSpec = DEFAULT_CANDIDATE,
                    command: CommandRecord | None = None) -> Decision:
    """Classify a DESKTOP detach (display path), not a PnP/package removal.

    Detach must leave the owned PnP device present/running; it only removes the active display
    path.  A device that disappears is a failure of this operation, not a successful detach.
    """
    errors = _validate_pair(before, after)
    if errors:
        return _invalid("detach", errors)
    ownership_errors = _manifest_ownership_errors(before, manifest, candidate)
    if ownership_errors:
        return Decision("detach", "refused_unowned", False, tuple(ownership_errors),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    if not manifest.display_device:
        return Decision("detach", "refused_unowned", False,
                        ("manifest has no owned display target",),
                        device_instance_id=manifest.device_instance_id,
                        before=before, after=after)
    before_display = before.display(manifest.display_device)
    if before_display is None or not before_display.active_path:
        return Decision("detach", "not_attached", False,
                        (f"owned display {manifest.display_device} was not active before detach",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    after_device = after.device(manifest.device_instance_id)
    if after_device is None or not after_device.present:
        return Decision("detach", "device_lost", False,
                        (f"owned device {manifest.device_instance_id} disappeared during a "
                         "desktop detach (PnP removal is a separate operation)",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    if _primary_signature(before) != _primary_signature(after):
        return Decision("detach", "primary_changed", False,
                        (f"primary changed during detach {_primary_signature(before)} -> "
                         f"{_primary_signature(after)}",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    after_display = after.display(manifest.display_device)
    if after_display is not None and after_display.active_path:
        return Decision("detach", "detach_failed", False,
                        (f"owned display {after_display.device} is still active after detach",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device,
                        receipt=DevconReceipt.parse(command), before=before, after=after)
    reasons = [f"owned display {manifest.display_device} is no longer active",
               f"owned device {manifest.device_instance_id} remains present",
               "primary display signature unchanged"]
    return Decision("detach", "detached", True, tuple(reasons),
                    device_instance_id=manifest.device_instance_id,
                    display_device=manifest.display_device,
                    receipt=DevconReceipt.parse(command), before=before, after=after)


def classify_attach(before: Snapshot, after: Snapshot, manifest: OwnershipManifest, *,
                    candidate: CandidateSpec = DEFAULT_CANDIDATE,
                    expected_mode: Mapping[str, int] | None = None,
                    command: CommandRecord | None = None) -> Decision:
    """Classify re-attaching the owned display path; the device must already be present."""
    errors = _validate_pair(before, after)
    if errors:
        return _invalid("attach", errors)
    ownership_errors = _manifest_ownership_errors(before, manifest, candidate,
                                                  require_display=False)
    if ownership_errors:
        return Decision("attach", "refused_unowned", False, tuple(ownership_errors),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    if not manifest.display_device:
        return Decision("attach", "refused_unowned", False,
                        ("manifest has no owned display target",),
                        device_instance_id=manifest.device_instance_id,
                        before=before, after=after)
    before_display = before.display(manifest.display_device)
    after_display = after.display(manifest.display_device)
    if after_device := after.device(manifest.device_instance_id):
        if not after_device.present:
            return Decision("attach", "device_lost", False,
                            (f"owned device {manifest.device_instance_id} is no longer present",),
                            device_instance_id=manifest.device_instance_id,
                            display_device=manifest.display_device, before=before, after=after)
    else:
        return Decision("attach", "device_lost", False,
                        (f"owned device {manifest.device_instance_id} is absent",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    if _primary_signature(before) != _primary_signature(after):
        return Decision("attach", "primary_changed", False,
                        (f"primary changed during attach {_primary_signature(before)} -> "
                         f"{_primary_signature(after)}",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    if after_display is None or not after_display.active_path:
        return Decision("attach", "attach_failed", False,
                        (f"owned display {manifest.display_device} is not active after attach",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    if not manifest.owns_display(after_display):
        return Decision("attach", "display_mismatch", False,
                        (f"display {after_display.device} is mapped to "
                         f"{after_display.adapter_instance_id!r}, not "
                         f"{manifest.device_instance_id}",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    if before_display is not None and before_display.active_path:
        return Decision("attach", "already_attached", True,
                        (f"owned display {manifest.display_device} was already active",),
                        device_instance_id=manifest.device_instance_id,
                        display_device=manifest.display_device, before=before, after=after)
    if expected_mode:
        expected = {key: _as_int(value) for key, value in expected_mode.items()}
        mismatch = {key: (expected[key], after_display.mode().get(key))
                    for key in expected if after_display.mode().get(key) != expected[key]}
        if mismatch:
            return Decision("attach", "display_mode_mismatch", False,
                            (f"mode mismatch {mismatch}",),
                            device_instance_id=manifest.device_instance_id,
                            display_device=manifest.display_device, before=before, after=after)
    reasons = [f"owned display {manifest.display_device} is active and mapped to "
               f"{manifest.device_instance_id}",
               "primary display signature unchanged"]
    return Decision("attach", "attached", True, tuple(reasons),
                    device_instance_id=manifest.device_instance_id,
                    display_device=manifest.display_device, before=before, after=after)


def classify_remove_receipt(command: CommandRecord) -> DevconReceipt:
    """Convenience: parse a DevCon remove/install receipt without any state proof."""
    receipt = DevconReceipt.parse(command)
    if receipt is None:  # pragma: no cover - command is required
        raise LifecycleError("a command record is required")
    return receipt


__all__ = [
    "SCHEMA_VERSION", "PROJECT", "DEFAULT_HARDWARE_ID", "DEFAULT_PROVIDER",
    "DEFAULT_CANDIDATE", "DEFAULT_INF_NAMES", "CandidateSpec", "Device", "Display",
    "Snapshot", "OwnershipManifest", "CommandRecord", "DevconReceipt", "Decision",
    "LifecycleError", "SnapshotError", "ManifestError", "load_snapshot",
    "load_manifest", "write_manifest", "classify_install", "classify_rollback",
    "classify_detach", "classify_attach", "classify_remove_receipt",
]
