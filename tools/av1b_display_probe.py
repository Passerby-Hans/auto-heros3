# -*- coding: utf-8 -*-
"""AV.1b read-only display/VDD snapshot collector.

Collects the data contract used by ``auto_heros3.runtime.display_lifecycle``:

* PnP Display devices via read-only ``Get-PnpDevice`` / ``Get-PnpDeviceProperty``;
* active adapters/monitors and native current modes via ``EnumDisplayDevices`` /
  ``EnumDisplaySettings``;
* the adapter-hardware-ID -> exact PnP instance mapping when it is unambiguous.

It performs **no** install, remove, attach/detach, registry or display-setting writes.  The
snapshot is intentionally complete=False when a display/mode cannot be read; the classifier
must reject incomplete observations rather than guess.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from auto_heros3.runtime.display_lifecycle import (  # noqa: E402
    SCHEMA_VERSION,
    Device,
    Display,
    Snapshot,
)

user32 = ctypes.WinDLL('user32', use_last_error=True)
if hasattr(user32, 'SetProcessDpiAwarenessContext'):
    # Per-monitor v2 for physical desktop rectangles.  This does not change the display.
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


class POINTL(ctypes.Structure):
    _fields_ = [('x', wt.LONG), ('y', wt.LONG)]


class DM1(ctypes.Structure):
    _fields_ = [('dmOrientation', ctypes.c_short), ('dmPaperSize', ctypes.c_short),
                ('dmPaperLength', ctypes.c_short), ('dmPaperWidth', ctypes.c_short),
                ('dmScale', ctypes.c_short), ('dmCopies', ctypes.c_short),
                ('dmDefaultSource', ctypes.c_short), ('dmPrintQuality', ctypes.c_short)]


class DM2(ctypes.Structure):
    _fields_ = [('dmPosition', POINTL), ('dmDisplayOrientation', wt.DWORD),
                ('dmDisplayFixedOutput', wt.DWORD)]


class DMU(ctypes.Union):
    _fields_ = [('s1', DM1), ('s2', DM2)]


class DEVMODE(ctypes.Structure):
    _fields_ = [
        ('dmDeviceName', wt.WCHAR * 32), ('dmSpecVersion', wt.WORD),
        ('dmDriverVersion', wt.WORD), ('dmSize', wt.WORD), ('dmDriverExtra', wt.WORD),
        ('dmFields', wt.DWORD), ('u', DMU), ('dmColor', ctypes.c_short),
        ('dmDuplex', ctypes.c_short), ('dmYResolution', ctypes.c_short),
        ('dmTTOption', ctypes.c_short), ('dmCollate', ctypes.c_short),
        ('dmFormName', wt.WCHAR * 32), ('dmLogPixels', wt.WORD),
        ('dmBitsPerPel', wt.DWORD), ('dmPelsWidth', wt.DWORD),
        ('dmPelsHeight', wt.DWORD), ('dmDisplayFlags', wt.DWORD),
        ('dmDisplayFrequency', wt.DWORD), ('dmICMMethod', wt.DWORD),
        ('dmICMIntent', wt.DWORD), ('dmMediaType', wt.DWORD),
        ('dmDitherType', wt.DWORD), ('dmReserved1', wt.DWORD),
        ('dmReserved2', wt.DWORD), ('dmPanningWidth', wt.DWORD),
        ('dmPanningHeight', wt.DWORD),
    ]


class RECT(ctypes.Structure):
    _fields_ = [('left', wt.LONG), ('top', wt.LONG), ('right', wt.LONG), ('bottom', wt.LONG)]


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', RECT), ('rcWork', RECT),
                ('dwFlags', wt.DWORD), ('szDevice', wt.WCHAR * 32)]


class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('DeviceName', wt.WCHAR * 32),
                ('DeviceString', wt.WCHAR * 128), ('StateFlags', wt.DWORD),
                ('DeviceID', wt.WCHAR * 128), ('DeviceKey', wt.WCHAR * 128)]


# A wrong DEVMODEW layout silently reads the wrong width/refresh fields.  Refuse to collect
# rather than publish plausible-looking but shifted values.
if ctypes.sizeof(DEVMODE) != 220:
    raise RuntimeError(f'unexpected DEVMODEW sizeof={ctypes.sizeof(DEVMODE)}, expected 220')


PS_DEVICES = r'''
$out=@()
$devices=Get-PnpDevice -Class Display -ErrorAction Stop
foreach($d in $devices){
  $id=$d.InstanceId
  $props=@{}
  foreach($k in @('DEVPKEY_Device_HardwareIds','DEVPKEY_Device_DriverInfPath',
                   'DEVPKEY_Device_DriverProvider','DEVPKEY_Device_DriverVersion')){
    $p=Get-PnpDeviceProperty -InstanceId $id -KeyName $k -ErrorAction SilentlyContinue
    $props[$k]=$p.Data
  }
  $out += [pscustomobject]@{
    instance_id=$id; friendly_name=$d.FriendlyName; present=[bool]$d.Present;
    status=[string]$d.Status; problem=[string]$d.Problem;
    hardware_ids=@($props['DEVPKEY_Device_HardwareIds']);
    inf_name=$props['DEVPKEY_Device_DriverInfPath'];
    provider=$props['DEVPKEY_Device_DriverProvider'];
    driver_version=$props['DEVPKEY_Device_DriverVersion']
  }
}
$out | ConvertTo-Json -Depth 6
'''


def _pnp_devices() -> list[dict]:
    proc = subprocess.run(['powershell', '-NoProfile', '-NonInteractive',
                           '-ExecutionPolicy', 'Bypass', '-Command', PS_DEVICES],
                          capture_output=True, text=True, encoding='utf-8', timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f'Get-PnpDevice failed: {proc.stderr[-1000:]}')
    text = (proc.stdout or '').strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def _adapter_for_display(display_name: str) -> tuple[DISPLAY_DEVICE | None, int]:
    index = 0
    while True:
        adapter = DISPLAY_DEVICE()
        adapter.cb = ctypes.sizeof(DISPLAY_DEVICE)
        if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(adapter), 0):
            return None, index
        if adapter.DeviceName == display_name:
            return adapter, index
        index += 1


def _native_mode(display_name: str) -> dict | None:
    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    if not user32.EnumDisplaySettingsW(display_name, -1, ctypes.byref(dm)):
        return None
    return {'width': int(dm.dmPelsWidth), 'height': int(dm.dmPelsHeight),
            'refresh_num': int(dm.dmDisplayFrequency), 'refresh_den': 1,
            'bpp': int(dm.dmBitsPerPel)}


def _monitors() -> list[tuple[MONITORINFOEX, DISPLAY_DEVICE | None]]:
    result: list[tuple[MONITORINFOEX, DISPLAY_DEVICE | None]] = []

    def cb(hmon, _hdc, _rect, _data):
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            adapter, _ = _adapter_for_display(info.szDevice)
            result.append((info, adapter))
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HMONITOR, wt.HDC, ctypes.POINTER(RECT),
                              wt.LPARAM)(cb)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    return result


def collect(vdd_original_inf: str = 'mttvdd.inf') -> Snapshot:
    warnings: list[str] = []
    raw_devices = _pnp_devices()
    devices = []
    by_hardware: dict[str, list[str]] = {}
    for item in raw_devices:
        hardware = item.get('hardware_ids') or []
        if isinstance(hardware, str):
            hardware = [hardware]
        instance = str(item.get('instance_id') or '')
        for hid in hardware:
            by_hardware.setdefault(str(hid).lower(), []).append(instance)
        original_inf = ''
        if any(str(h).lower() == r'root\mttvdd'.lower() for h in hardware):
            original_inf = vdd_original_inf
        devices.append(Device(
            instance_id=instance,
            hardware_ids=tuple(str(h) for h in hardware if str(h)),
            friendly_name=str(item.get('friendly_name') or ''),
            inf_name=str(item.get('inf_name') or ''),
            original_inf=original_inf,
            provider=str(item.get('provider') or ''),
            driver_version=str(item.get('driver_version') or ''),
            present=bool(item.get('present', True)),
            status=str(item.get('status') or ''),
            problem=str(item.get('problem') or ''),
        ))
    displays = []
    for info, adapter in _monitors():
        device = info.szDevice
        mode = _native_mode(device)
        if mode is None:
            warnings.append(f'{device}: EnumDisplaySettings failed')
            continue
        hardware_id = str(adapter.DeviceID) if adapter is not None else ''
        candidates = by_hardware.get(hardware_id.lower(), []) if hardware_id else []
        instance_id = ''
        if len(candidates) == 1:
            instance_id = candidates[0]
        elif len(candidates) > 1:
            warnings.append(f'{device}: hardware ID {hardware_id!r} maps to '
                            f'{len(candidates)} PnP instances')
        monitor_path = ''
        if adapter is not None:
            monitor = DISPLAY_DEVICE()
            monitor.cb = ctypes.sizeof(DISPLAY_DEVICE)
            if user32.EnumDisplayDevicesW(adapter.DeviceName, 0, ctypes.byref(monitor), 0):
                monitor_path = monitor.DeviceID
        displays.append(Display(
            device=device,
            primary=bool(info.dwFlags & 1),
            x=int(info.rcMonitor.left), y=int(info.rcMonitor.top),
            width=mode['width'], height=mode['height'],
            refresh_num=mode['refresh_num'], refresh_den=mode['refresh_den'],
            monitor_device_path=monitor_path,
            adapter_instance_id=instance_id,
            active_path=True,
        ))
    warnings.append('refresh values are integer-Hz observations; exact rational CCD timing '
                    'must be checked separately')
    snapshot = Snapshot(captured_at=__import__('time').strftime('%Y-%m-%dT%H:%M:%S%z'),
                        complete=True, devices=tuple(devices), displays=tuple(displays),
                        warnings=tuple(warnings))
    errors = snapshot.validation_errors()
    if errors:
        snapshot = Snapshot(captured_at=snapshot.captured_at, complete=False,
                            devices=snapshot.devices, displays=snapshot.displays,
                            warnings=tuple(warnings) + tuple(errors))
    return snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description='Read-only AV.1b display/VDD snapshot')
    ap.add_argument('--out', default='var/runs/av1b-p1b-20261102-1845/snapshot.json')
    ap.add_argument('--vdd-original-inf', default='mttvdd.inf')
    args = ap.parse_args()
    snap = collect(args.vdd_original_inf)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap.to_dict(), ensure_ascii=False, indent=1) + '\n',
                   encoding='utf-8')
    print(json.dumps({'out': str(out), 'complete': snap.complete,
                      'devices': len(snap.devices), 'displays': len(snap.displays),
                      'primary': snap.primary().device if snap.primary() else None,
                      'warnings': list(snap.warnings)}, ensure_ascii=False, indent=1))
    return 0 if snap.complete else 2


if __name__ == '__main__':
    raise SystemExit(main())
