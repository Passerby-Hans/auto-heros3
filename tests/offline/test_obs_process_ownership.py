"""Recorder cleanup preserves uncertain ownership; entirely fake process tests."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
import psutil
from auto_heros3.recording.obs_backend import ObsBackendError, ObsRecorder


class FakeProcess:
    def __init__(self, *, pid=4242, terminate_exits=False, kill_exits=False, denied=False):
        self.pid = pid
        self.returncode = None
        self.terminate_exits = terminate_exits
        self.kill_exits = kill_exits
        self.denied = denied
        self.created = 12345.0
        self.path = str(Path('recorder.exe').resolve())
        self.calls = []
        self.wmi = False

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.calls.append(('wait', timeout))
        if self.returncode is None:
            if self.wmi:
                raise psutil.TimeoutExpired(timeout, self.pid)
            raise subprocess.TimeoutExpired('fake', timeout)
        return self.returncode

    def terminate(self):
        self.calls.append(('terminate',))
        if self.denied:
            raise psutil.AccessDenied(self.pid)
        if self.terminate_exits:
            self.returncode = 1

    def kill(self):
        self.calls.append(('kill',))
        if self.denied:
            raise psutil.AccessDenied(self.pid)
        if self.kill_exits:
            self.returncode = 9

    def is_running(self):
        return self.returncode is None

    def create_time(self):
        return self.created

    def exe(self):
        return self.path


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        controller = patch('auto_heros3.recording.obs_backend._controller_process_identity',
                           return_value=(7654, 134064000000000001))
        controller.start()
        self.addCleanup(controller.stop)

    def recorder(self):
        return ObsRecorder(Path('recorder.exe'))

    def popen_owner(self, process):
        recorder = self.recorder()
        recorder.proc = process
        recorder.native_pid = process.pid  # hello may already have supplied it
        return recorder

    def wmi_owner(self, process):
        process.wmi = True
        recorder = self.recorder()
        recorder.native_pid = process.pid
        recorder._wmi_wrapper_pid = process.pid
        recorder.wmi_launched = True
        recorder.native_identity = {'pid': process.pid, 'create_time': process.created, 'exe': process.path}
        return recorder

    def test_popen_failed_terminate_and_kill_preserves_owner(self):
        process = FakeProcess(denied=True)
        recorder = self.popen_owner(process)
        with self.assertRaises(ObsBackendError) as caught:
            recorder.close(force=True)
        self.assertEqual(caught.exception.code, 'native_cleanup_unconfirmed')
        self.assertIs(recorder.proc, process)
        self.assertEqual(recorder.pid, process.pid)
        self.assertTrue(recorder.alive())

    def test_popen_wait_timeout_does_not_clear_pid(self):
        process = FakeProcess()
        recorder = self.popen_owner(process)
        with self.assertRaises(ObsBackendError):
            recorder.close(force=True)
        self.assertEqual(recorder.pid, process.pid)
        self.assertEqual([row[0] for row in process.calls], ['terminate', 'wait', 'kill', 'wait'])

    def test_popen_failed_cleanup_can_retry_successfully(self):
        process = FakeProcess()
        recorder = self.popen_owner(process)
        with self.assertRaises(ObsBackendError):
            recorder.close(force=True)
        process.kill_exits = True
        recorder.close(force=True)
        self.assertIsNone(recorder.pid)
        self.assertIsNone(recorder.proc)

    def test_popen_already_exited_is_not_signaled(self):
        process = FakeProcess()
        process.returncode = 0
        recorder = self.popen_owner(process)
        recorder.close(force=True)
        self.assertEqual(process.calls, [])
        self.assertFalse(recorder.alive())

    def test_nongraceful_popen_cleanup_waits_after_escalation(self):
        process = FakeProcess(kill_exits=True)
        recorder = self.popen_owner(process)
        recorder.close(force=False)
        self.assertEqual([row[0] for row in process.calls], ['wait', 'terminate', 'wait', 'kill', 'wait'])
        self.assertIsNone(recorder.pid)

    def test_wmi_failed_cleanup_preserves_full_identity(self):
        process = FakeProcess(denied=True)
        recorder = self.wmi_owner(process)
        expected = dict(recorder.native_identity)
        with patch('psutil.Process', return_value=process), self.assertRaises(ObsBackendError):
            recorder.close(force=True)
        self.assertEqual(recorder.native_identity, expected)
        self.assertEqual(recorder.pid, process.pid)

    def test_wmi_timeout_then_successful_retry(self):
        process = FakeProcess()
        recorder = self.wmi_owner(process)
        with patch('psutil.Process', return_value=process):
            with self.assertRaises(ObsBackendError):
                recorder.close(force=True)
            process.kill_exits = True
            recorder.close(force=True)
        self.assertIsNone(recorder.pid)
        self.assertIsNone(recorder.native_identity)
        self.assertIsNone(recorder._wmi_wrapper_pid)

    def test_wmi_reused_pid_never_signals_new_process(self):
        process = FakeProcess()
        recorder = self.wmi_owner(process)
        process.created += 10
        with patch('psutil.Process', return_value=process):
            self.assertFalse(recorder.alive())
            recorder.close(force=True)
        self.assertEqual(process.calls, [])
        self.assertIsNone(recorder.pid)

    def test_wmi_access_denied_is_unknown_not_dead(self):
        recorder = self.wmi_owner(FakeProcess())
        with patch('psutil.Process', side_effect=psutil.AccessDenied(recorder.pid)):
            self.assertTrue(recorder.alive())
            with self.assertRaises(ObsBackendError):
                recorder.close(force=True)
        self.assertEqual(recorder.pid, 4242)

    def test_wmi_missing_identity_is_not_killed(self):
        process = FakeProcess()
        recorder = self.wmi_owner(process)
        recorder.native_identity = None
        with patch('psutil.Process', return_value=process), self.assertRaises(ObsBackendError):
            recorder.close(force=True)
        self.assertEqual(process.calls, [])
        self.assertEqual(recorder.pid, process.pid)

    def test_wmi_gone_process_clears_owner(self):
        recorder = self.wmi_owner(FakeProcess())
        with patch('psutil.Process', side_effect=psutil.NoSuchProcess(recorder.pid)):
            recorder.close(force=True)
        self.assertIsNone(recorder.pid)

    def test_wmi_identity_capture_verifies_executable(self):
        process = FakeProcess()
        recorder = self.recorder()
        with patch('psutil.Process', return_value=process):
            recorder._remember_native_identity(process.pid)
            self.assertEqual(recorder.native_identity['create_time'], process.created)
            process.path = str(Path('unrelated.exe').resolve())
            with self.assertRaises(ObsBackendError):
                recorder._remember_native_identity(process.pid)

    def test_connect_timeout_keeps_unstoppable_popen_owner(self):
        process = FakeProcess(denied=True)
        recorder = self.recorder()
        with patch('subprocess.Popen', return_value=process), \
                patch('auto_heros3.recording.obs_backend._process_in_job', return_value=False), \
                patch.dict('os.environ', {'AUTO_HEROS3_OBS_FORCE_WMI': '0'}), \
                self.assertRaises(ObsBackendError) as caught:
            recorder.connect(timeout=0)
        self.assertEqual(caught.exception.code, 'native_cleanup_unconfirmed')
        self.assertIs(recorder.proc, process)
        self.assertEqual(recorder.pid, process.pid)

    def test_popen_callback_precedes_any_connect_wait(self):
        process = FakeProcess(denied=True)
        observed = []
        recorder = ObsRecorder(Path('recorder.exe'), on_spawn=lambda owner: observed.append(owner.pid))
        with patch('subprocess.Popen', return_value=process) as launch, \
                patch('auto_heros3.recording.obs_backend._process_in_job', return_value=False), \
                patch.dict('os.environ', {'AUTO_HEROS3_OBS_FORCE_WMI': '0'}), self.assertRaises(ObsBackendError):
            recorder.connect(timeout=0)
        self.assertEqual(observed, [process.pid])
        cmd = launch.call_args.args[0]
        self.assertEqual(cmd[cmd.index('--owner-pid') + 1], '7654')
        self.assertEqual(cmd[cmd.index('--owner-created-filetime') + 1], '134064000000000001')

    def test_failed_spawn_notification_retains_child_before_wait(self):
        process = FakeProcess()
        def notification(owner):
            self.assertEqual(owner.pid, process.pid)
            raise OSError('owner publication failed')
        recorder = ObsRecorder(Path('recorder.exe'), on_spawn=notification)
        with patch('subprocess.Popen', return_value=process), \
                patch('auto_heros3.recording.obs_backend._process_in_job') as check_job, \
                self.assertRaisesRegex(OSError, 'publication failed'):
            recorder.connect(timeout=0)
        check_job.assert_not_called()
        self.assertIs(recorder.proc, process)

    def test_job_breakaway_failure_cannot_spawn_second_native(self):
        process = FakeProcess(denied=True)
        recorder = self.recorder()
        with patch('subprocess.Popen', return_value=process), \
                patch('auto_heros3.recording.obs_backend._process_in_job', return_value=True), \
                patch.object(recorder, '_spawn_wmi') as spawn, self.assertRaises(ObsBackendError):
            recorder.connect(timeout=0)
        spawn.assert_not_called()
        self.assertIs(recorder.proc, process)

    def test_connect_timeout_keeps_unstoppable_wmi_owner(self):
        child = FakeProcess(pid=111, terminate_exits=True)
        native = FakeProcess(denied=True)
        native.wmi = True
        recorder = self.recorder()
        def spawn(cmd):
            recorder.native_pid = native.pid
            recorder.native_identity = {'pid': native.pid, 'create_time': native.created, 'exe': native.path}
        with patch('subprocess.Popen', return_value=child), \
                patch('auto_heros3.recording.obs_backend._process_in_job', return_value=True), \
                patch.object(recorder, '_spawn_wmi', side_effect=spawn), \
                patch('psutil.Process', return_value=native), self.assertRaises(ObsBackendError) as caught:
            recorder.connect(timeout=0)
        self.assertEqual(caught.exception.code, 'native_cleanup_unconfirmed')
        self.assertEqual(recorder.pid, native.pid)
        self.assertIsNotNone(recorder.native_identity)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(OwnershipTests))
    failures = len(result.failures) + len(result.errors)
    print(f'{result.testsRun - failures}/{result.testsRun} checks passed')
    raise SystemExit(0 if result.wasSuccessful() else 1)
