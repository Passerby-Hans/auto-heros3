# -*- coding: utf-8 -*-
"""AV.1b display lifecycle: pure synthetic-fixture suite (no Win32/device mutation).

Covers the P1 root causes:
* hardware ID vs instance ID identity;
* exactly-one-primary / complete snapshot validation;
* ownership manifest and refusal to touch unowned devices;
* install classification with post-state (exit 0 is not proof);
* duplicate/ambiguous devices and modes/primary changes;
* DevCon receipt parsing and rollback verification/partial/failed states.

Run: python -X utf8 tools/test_display_lifecycle.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from auto_heros3.runtime import display_lifecycle as L  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def D(instance='ROOT\\DISPLAY\\0002', hardware=('Root\\MttVDD',),
      present=True, status='OK', problem='CM_PROB_NONE', inf='oem84.inf',
      orig='mttvdd.inf', provider='MikeTheTech'):
    return L.Device(instance_id=instance, hardware_ids=tuple(hardware),
                    friendly_name='Virtual Display Driver', inf_name=inf,
                    original_inf=orig, provider=provider, driver_version='11.30.4.434',
                    present=present, status=status, problem=problem)


def P(device='\\\\.\\DISPLAY32', primary=False, width=1920, height=1080,
      refresh=60, adapter='ROOT\\DISPLAY\\0002', active=True, x=7680):
    return L.Display(device=device, primary=primary, x=x, y=0,
                     width=width, height=height, refresh_num=refresh, refresh_den=1,
                     monitor_device_path='MONITOR\\MTT1337\\...\\0006',
                     adapter_instance_id=adapter, active_path=active)


def S(devices, displays, complete=True):
    return L.Snapshot(captured_at='2026-11-02T00:00:00Z', complete=complete,
                      devices=tuple(devices), displays=tuple(displays))


def command(argv, exit_code=0, stdout='', stderr=''):
    return L.CommandRecord(argv=tuple(argv), exit_code=exit_code,
                           stdout=stdout, stderr=stderr)


def physical():
    return L.Device(instance_id='PCI\\VEN_10DE\\0000', hardware_ids=('PCI\\VEN_10DE',),
                    friendly_name='NVIDIA GeForce RTX 4090', inf_name='nv.inf',
                    provider='NVIDIA', status='OK', problem='CM_PROB_NONE')


def primary_display():
    return L.Display(device='\\\\.\\DISPLAY1', primary=True, x=0, y=0,
                     width=3840, height=2160, refresh_num=144,
                     monitor_device_path='MONITOR\\AOC2702\\...\\0001',
                     adapter_instance_id='PCI\\VEN_10DE\\0000', active_path=True)


def base_before():
    return S([physical()], [primary_display()])


def installed_after():
    return S([physical(), D()], [primary_display(), P()])


def test_identity_and_snapshot_validation() -> None:
    check('L1 hardware ID identifies the family even though instance ID differs',
          L.DEFAULT_CANDIDATE.matches(D()) and D().instance_id != 'Root\\MttVDD')
    check('L2 a wrong provider/INF is not accepted as our candidate',
          not L.DEFAULT_CANDIDATE.matches(D(provider='Other')) and
          not L.DEFAULT_CANDIDATE.matches(D(orig='other.inf', inf='other.inf')))
    valid = base_before()
    check('L3 a complete snapshot needs exactly one primary and positive modes',
          not valid.validation_errors())
    missing_primary = S([physical()], [primary_display().__class__(
        device='\\\\.\\DISPLAY1', primary=False, width=1920, height=1080,
        refresh_num=60, active_path=True)])
    check('L4 a snapshot without one primary is rejected',
          any('primary' in item for item in missing_primary.validation_errors()))
    dup = S([physical(), D()], [primary_display(), P()],
            complete=True)
    check('L5 duplicate instance IDs are rejected',
          any('duplicate' in item for item in S([physical(), physical()],
                                                [primary_display()]).validation_errors()))
    check('L6 incomplete collector output is rejected',
          any('incomplete' in item for item in S([physical()], [primary_display()],
                                                 complete=False).validation_errors()))
    check('L7 zero-sized active display is rejected',
          any('non-positive' in item for item in S(
              [physical()], [primary_display().__class__(
                  device='\\\\.\\DISPLAY32', primary=False, width=0, height=0,
                  refresh_num=60, active_path=True)]).validation_errors()))
    del dup


def test_manifest_ownership() -> None:
    snap = installed_after()
    manifest = L.OwnershipManifest.from_snapshot(snap, run_id='run-1', package_sha256='abc')
    check('L8 manifest records exact instance + display, not hardware ID',
          manifest.device_instance_id == 'ROOT\\DISPLAY\\0002'
          and manifest.display_device == '\\\\.\\DISPLAY32'
          and manifest.hardware_ids == ('Root\\MttVDD',))
    check('L9 manifest owns exactly that device/display and nothing else',
          manifest.owns_device(snap.device('ROOT\\DISPLAY\\0002'))
          and manifest.owns_display(snap.display('\\\\.\\DISPLAY32'))
          and not manifest.owns_device(snap.device('ROOT\\DISPLAY\\0000')))
    with tempfile.TemporaryDirectory(prefix='av1b-manifest-') as tmp:
        path = Path(tmp) / 'owner.json'
        L.write_manifest(path, manifest)
        loaded = L.load_manifest(path)
        check('L10 manifest round-trips atomically with schema/project identity',
              loaded == manifest and json.loads(path.read_text(encoding='utf-8'))['project'] == 'auto-heros3')
    broken = manifest.to_dict()
    broken['project'] = 'someone-else'
    try:
        L.OwnershipManifest.from_mapping(broken).validate()
        bad_project_rejected = False
    except L.ManifestError:
        bad_project_rejected = True
    broken2 = manifest.to_dict()
    broken2['device_instance_id'] = 'ROOT\\DISPLAY\\0009'
    bad = L.OwnershipManifest.from_mapping(broken2)
    check('L11 foreign project and mismatched instance are refused',
          bad_project_rejected and not bad.owns_device(snap.device('ROOT\\DISPLAY\\0002')))
    ambiguous = S([physical(), D(), D(instance='ROOT\\DISPLAY\\0003')],
                  [primary_display(), P(), P(device='\\\\.\\DISPLAY33',
                                             adapter='ROOT\\DISPLAY\\0003')])
    try:
        L.OwnershipManifest.from_snapshot(ambiguous, run_id='run-2')
        ambiguous_rejected = False
    except L.ManifestError:
        ambiguous_rejected = True
    check('L12 manifest creation refuses two matching devices', ambiguous_rejected)


def test_install_classification() -> None:
    receipt = command(['devcon', 'install', 'MttVDD.inf', 'Root\\MttVDD'],
                      stdout='Device node created.\nDrivers installed successfully.')
    ok = L.classify_install(base_before(), installed_after(),
                            expected_mode={'width': 1920, 'height': 1080,
                                           'refresh_num': 60, 'refresh_den': 1},
                            command=receipt)
    check('L13 a real new device/display is installed; command exit is not the proof',
          ok.status == 'installed' and ok.ok and ok.device_instance_id == 'ROOT\\DISPLAY\\0002'
          and ok.display_device == '\\\\.\\DISPLAY32' and ok.receipt.installed)
    already = L.classify_install(installed_after(), installed_after(), command=receipt)
    check('L14 an existing candidate is already_present, not installed twice',
          already.status == 'already_present' and already.ok)
    failed = L.classify_install(base_before(), base_before(),
                                command=command(['devcon', 'install'], exit_code=0,
                                                stdout='Drivers installed successfully.'))
    check('L15 exit 0 with no observed device is install_failed',
          failed.status == 'install_failed' and not failed.ok)
    primary = S([physical(), D()],
                [primary_display().__class__(device='\\\\.\\DISPLAY32', primary=True,
                                             width=1920, height=1080, refresh_num=60,
                                             adapter_instance_id='ROOT\\DISPLAY\\0002'),
                 primary_display().__class__(device='\\\\.\\DISPLAY1', primary=False,
                                             width=3840, height=2160, refresh_num=144,
                                             adapter_instance_id='PCI\\VEN_10DE\\0000')])
    changed = L.classify_install(base_before(), primary)
    check('L16 a new primary cannot be reported as success', changed.status == 'primary_changed'
          and not changed.ok)
    no_display = L.classify_install(base_before(), S([physical(), D()], [primary_display()]))
    check('L17 a device without a mapped display is installed_no_display (incomplete)',
          no_display.status == 'installed_no_display' and not no_display.ok)
    wrong_mode = L.classify_install(base_before(), installed_after(),
                                    expected_mode={'width': 800, 'height': 600,
                                                   'refresh_num': 30, 'refresh_den': 1})
    check('L18 a mode mismatch is explicit, not success',
          wrong_mode.status == 'display_mode_mismatch' and not wrong_mode.ok)
    two_new = S([physical(), D(), D(instance='ROOT\\DISPLAY\\0003')],
                [primary_display(), P(device='\\\\.\\DISPLAY33', adapter='ROOT\\DISPLAY\\0003')])
    ambiguous = L.classify_install(base_before(), two_new)
    check('L19 two new matching devices are ambiguous', ambiguous.status == 'ambiguous_new_devices'
          and not ambiguous.ok)
    not_ready = S([physical(), D(status='Error', problem='CM_PROB_FAILED_START')],
                  [primary_display(), P()])
    not_ready_res = L.classify_install(base_before(), not_ready)
    check('L20 a present but failed device is device_not_ready', not_ready_res.status == 'device_not_ready'
          and not not_ready_res.ok)
    incomplete = L.classify_install(base_before(), S([physical(), D()], [primary_display()],
                                                     complete=False))
    check('L21 incomplete snapshots are rejected before classification',
          incomplete.status == 'snapshot_invalid' and not incomplete.ok)


def test_receipts_and_rollback() -> None:
    no_remove = L.classify_remove_receipt(command(
        ['devcon', 'remove', 'ROOT\\DISPLAY\\0002'], exit_code=0,
        stdout='No devices were removed.'))
    removed = L.classify_remove_receipt(command(
        ['devcon', 'remove', '@ROOT\\DISPLAY\\0002'], exit_code=0,
        stdout='ROOT\\DISPLAY\\0002 : Removed\n1 device(s) were removed.'))
    reboot = L.classify_remove_receipt(command(
        ['devcon', 'remove', '@ROOT\\DISPLAY\\0002'], exit_code=0,
        stdout='A reboot is required to complete the operation.'))
    check('L22 DevCon zero-match and removed receipts are distinguished',
          no_remove.no_devices and no_remove.removed_count == 0
          and removed.removed_count == 1 and not removed.no_devices)
    check('L23 reboot requirement is parsed, not silently treated as success',
          reboot.requires_reboot)
    reboot_install = L.classify_install(
        base_before(), installed_after(),
        command=command(['devcon', 'install'], exit_code=0,
                        stdout='Device node created. A reboot is required.'))
    check('L24 a pending-reboot install is reboot_required, not installed',
          reboot_install.status == 'reboot_required' and not reboot_install.ok)
    snap_before = installed_after()
    manifest = L.OwnershipManifest.from_snapshot(snap_before, run_id='run-rb')
    still = L.classify_rollback(snap_before, snap_before, manifest,
                                command=command(['devcon', 'remove', 'ROOT\\DISPLAY\\0002'],
                                                stdout='No devices were removed.'))
    check('L24b exit 0/zero-target receipt while device remains is rollback_failed',
          still.status == 'rollback_failed' and not still.ok)
    gone = S([physical()], [primary_display()])
    verified = L.classify_rollback(snap_before, gone, manifest,
                                   command=command(['devcon', 'remove', '@ROOT\\DISPLAY\\0002'],
                                                   stdout='1 device(s) were removed.'))
    check('L25 post-state absence + primary unchanged is rollback_verified',
          verified.status == 'rollback_verified' and verified.ok)
    reboot_rollback = L.classify_rollback(
        snap_before, gone, manifest,
        command=command(['devcon', 'remove', '@ROOT\\DISPLAY\\0002'],
                        stdout='Device removed. A reboot is required.'))
    check('L26 a pending-reboot rollback is not verified yet',
          reboot_rollback.status == 'reboot_required' and not reboot_rollback.ok)
    partial = L.classify_rollback(snap_before,
                                  S([physical()], [primary_display(), P()]), manifest)
    check('L27 a device gone but display still active is rollback_partial',
          partial.status == 'rollback_partial' and not partial.ok)
    other = L.OwnershipManifest.from_snapshot(snap_before, run_id='run-other')
    object.__setattr__(other, 'device_instance_id', 'ROOT\\DISPLAY\\0009')
    refused = L.classify_rollback(snap_before, gone, other)
    check('L28 rollback refuses a manifest that does not own the device',
          refused.status == 'refused_unowned' and not refused.ok)
    changed = L.classify_rollback(snap_before,
                                  S([physical()], [primary_display().__class__(
                                      device='\\\\.\\DISPLAY32', primary=True, width=1920,
                                      height=1080, refresh_num=60, active_path=True)]),
                                  manifest)
    check('L29 rollback with a changed primary is rollback_failed',
          changed.status == 'rollback_failed' and not changed.ok)


def test_detach_attach_classification() -> None:
    snap = installed_after()
    manifest = L.OwnershipManifest.from_snapshot(snap, run_id='run-da')
    detached = S([physical(), D()], [primary_display()])
    ok = L.classify_detach(snap, detached, manifest)
    check('L30 desktop detach keeps the PnP device and removes only the display path',
          ok.status == 'detached' and ok.ok
          and ok.device_instance_id == 'ROOT\\DISPLAY\\0002'
          and ok.display_device == '\\\\.\\DISPLAY32')
    still_active = L.classify_detach(snap, snap, manifest)
    check('L31 a still-active display is detach_failed', still_active.status == 'detach_failed'
          and not still_active.ok)
    device_lost = L.classify_detach(snap, S([physical()], [primary_display()]), manifest)
    check('L32 a disappeared PnP device during detach is device_lost, not detached',
          device_lost.status == 'device_lost' and not device_lost.ok)
    attach_before = S([physical(), D()], [primary_display()])
    attach_ok = L.classify_attach(attach_before, snap, manifest,
                                  expected_mode={'width': 1920, 'height': 1080,
                                                 'refresh_num': 60, 'refresh_den': 1})
    check('L33 attach restores the same display target/mode',
          attach_ok.status == 'attached' and attach_ok.ok
          and attach_ok.display_device == '\\\\.\\DISPLAY32')
    already = L.classify_attach(snap, snap, manifest)
    check('L34 an already-active display is already_attached, not a new operation',
          already.status == 'already_attached' and already.ok)
    wrong = S([physical(), D()],
              [primary_display(), P(adapter='ROOT\\DISPLAY\\9999')])
    mismatch = L.classify_attach(attach_before, wrong, manifest)
    check('L35 display mapped to another instance is display_mismatch',
          mismatch.status == 'display_mismatch' and not mismatch.ok)
    bad_primary = S([physical(), D()],
                    [primary_display().__class__(device='\\\\.\\DISPLAY32', primary=True,
                                                 width=1920, height=1080, refresh_num=60,
                                                 adapter_instance_id='ROOT\\DISPLAY\\0002')])
    changed = L.classify_attach(attach_before, bad_primary, manifest)
    check('L36 attach with a changed primary is primary_changed',
          changed.status == 'primary_changed' and not changed.ok)
    other = L.OwnershipManifest.from_snapshot(snap, run_id='run-other-da')
    object.__setattr__(other, 'device_instance_id', 'ROOT\\DISPLAY\\0009')
    refused = L.classify_detach(snap, detached, other)
    check('L37 detach refuses a manifest that does not own the device',
          refused.status == 'refused_unowned' and not refused.ok)


def main() -> int:
    test_identity_and_snapshot_validation()
    test_manifest_ownership()
    test_install_classification()
    test_receipts_and_rollback()
    test_detach_attach_classification()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f'\n{passed}/{len(RESULTS)} checks passed')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
