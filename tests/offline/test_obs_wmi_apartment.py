"""WMI COM ownership on reused/different worker threads; no COM/process launch."""
from __future__ import annotations

import concurrent.futures
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from auto_heros3.recording.obs_backend import ObsBackendError, ObsRecorder


class ComError(Exception):
    def __init__(self, hresult):
        self.hresult = hresult
        super().__init__(str(hresult))


class Node:
    def __init__(self, env, kind):
        self.env, self.kind = env, kind
        env.local.live += 1

    def __del__(self):
        self.env.local.live -= 1
        self.env.event("release", self.kind)

    def ConnectServer(self, host, namespace):
        self.env.event("connect", (host, namespace))
        return Node(self.env, "service")

    def Get(self, name):
        self.env.event("get", name)
        return Node(self.env, "process_class")

    def Methods_(self, name):
        return Node(self.env, "method")

    @property
    def InParameters(self):
        return Node(self.env, "input_parameters")

    def SpawnInstance_(self):
        return Node(self.env, "parameters")

    def ExecMethod(self, object_name, method, params):
        self.env.event("create", (object_name, method, params.CommandLine))
        if self.env.exec_error:
            raise ComError(-2147024891)
        result = Node(self.env, "result")
        result.ReturnValue = self.env.return_code
        result.ProcessId = 4242
        return result


class FakeCom:
    def __init__(self, *, return_code=0, init_error=None, dispatch_error=False, exec_error=False):
        self.local = threading.local()
        self.events = []
        self.lock = threading.Lock()
        self.return_code, self.init_error = return_code, init_error
        self.dispatch_error, self.exec_error = dispatch_error, exec_error
        self.pythoncom = ModuleType("pythoncom")
        self.pythoncom.com_error = ComError
        self.pythoncom.COINIT_APARTMENTTHREADED = 2
        self.pythoncom.CoInitializeEx = self.initialize
        self.pythoncom.CoUninitialize = self.uninitialize
        self.win32com = ModuleType("win32com")
        self.win32com.__path__ = []
        self.client = ModuleType("win32com.client")
        self.client.Dispatch = self.dispatch
        self.win32com.client = self.client

    def modules(self):
        return {"pythoncom": self.pythoncom, "win32com": self.win32com, "win32com.client": self.client}

    def event(self, name, detail=None):
        with self.lock:
            self.events.append((threading.get_ident(), name, detail))

    def initialize(self, mode):
        self.event("initialize", mode)
        self.local.live = 0
        if self.init_error is not None:
            if self.init_error & 0xffffffff == 0x80010106:
                self.local.refs = 1  # caller-owned MTA, incompatible init adds no reference
            raise ComError(self.init_error)
        self.local.refs = getattr(self.local, "refs", 0) + 1

    def uninitialize(self):
        self.event("uninitialize", self.local.live)
        self.local.refs -= 1
        if self.local.refs < 0:
            raise AssertionError("unbalanced COM release")

    def dispatch(self, name):
        if not getattr(self.local, "refs", 0):
            raise ComError(-2147221008)  # CO_E_NOTINITIALIZED: observed second-worker failure
        self.event("dispatch", name)
        if self.dispatch_error:
            raise ComError(-2147024891)
        return Node(self, "locator")


class ApartmentTests(unittest.TestCase):
    def recorder(self):
        return ObsRecorder(Path("private directory/recorder.exe"),
                           log_path=Path("evidence directory/server.log"))

    def run_fake(self, fake, recorder=None):
        recorder = recorder or self.recorder()
        with patch.dict(sys.modules, fake.modules()), patch.object(recorder, '_remember_native_identity'):
            recorder._spawn_wmi([str(recorder.exe), "--outputs-module", "private directory/outputs.dll"])
        return recorder

    def test_call_initializes_and_balances_current_thread(self):
        fake = FakeCom()
        recorder = self.run_fake(fake)
        names = [row[1] for row in fake.events]
        self.assertLess(names.index("initialize"), names.index("dispatch"))
        self.assertEqual(names[-1], "uninitialize")
        self.assertEqual(fake.events[-1][2], 0)  # dispatch objects released first
        self.assertEqual(fake.local.refs, 0)
        self.assertEqual(recorder.pid, 4242)
        self.assertTrue(recorder.wmi_launched)

    def test_two_separate_workers_each_initialize(self):
        fake = FakeCom()
        first_done, second_done = threading.Event(), threading.Event()
        def first():
            recorder = self.recorder()
            try:
                recorder._spawn_wmi([str(recorder.exe)])
                first_done.set()
                if not second_done.wait(5):
                    raise AssertionError("second worker did not finish")
                return threading.get_ident(), recorder.pid
            finally:
                first_done.set()
        def second():
            try:
                if not first_done.wait(5):
                    raise AssertionError("first worker did not finish")
                recorder = self.recorder()
                recorder._spawn_wmi([str(recorder.exe)])
                return threading.get_ident(), recorder.pid
            finally:
                second_done.set()
        with patch.dict(sys.modules, fake.modules()), patch.object(ObsRecorder, '_remember_native_identity'), concurrent.futures.ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(first), pool.submit(second)
            first_result, second_result = a.result(10), b.result(10)
        self.assertNotEqual(first_result[0], second_result[0])
        self.assertEqual(first_result[1], second_result[1])
        for tid in (first_result[0], second_result[0]):
            names = [row[1] for row in fake.events if row[0] == tid]
            self.assertEqual(names.count("initialize"), 1)
            self.assertEqual(names.count("uninitialize"), 1)
            self.assertLess(names.index("initialize"), names.index("dispatch"))

    def test_same_worker_can_launch_again(self):
        fake = FakeCom()
        self.run_fake(fake)
        self.run_fake(fake)
        self.assertEqual(sum(row[1] == "initialize" for row in fake.events), 2)
        self.assertEqual(sum(row[1] == "uninitialize" for row in fake.events), 2)
        self.assertEqual(fake.local.refs, 0)

    def test_existing_sta_reference_remains_owned_by_caller(self):
        fake = FakeCom()
        fake.local.refs = 1
        self.run_fake(fake)
        self.assertEqual(fake.local.refs, 1)

    def test_existing_mta_is_reused_without_uninitialize(self):
        fake = FakeCom(init_error=-2147417850)
        self.run_fake(fake)
        self.assertEqual(fake.local.refs, 1)
        self.assertFalse(any(row[1] == "uninitialize" for row in fake.events))

    def test_other_initialize_error_cannot_dispatch(self):
        fake = FakeCom(init_error=-2147024891)
        with self.assertRaises(ComError):
            self.run_fake(fake)
        self.assertEqual([row[1] for row in fake.events], ["initialize"])

    def test_dispatch_error_still_balances_com(self):
        fake = FakeCom(dispatch_error=True)
        recorder = self.recorder()
        with self.assertRaises(ComError):
            self.run_fake(fake, recorder)
        self.assertEqual(fake.local.refs, 0)
        self.assertFalse(recorder.wmi_launched)
        self.assertIsNone(recorder.pid)

    def test_create_exception_still_balances_com(self):
        fake = FakeCom(exec_error=True)
        recorder = self.recorder()
        with self.assertRaises(ComError):
            self.run_fake(fake, recorder)
        self.assertEqual(fake.local.refs, 0)
        self.assertFalse(recorder.wmi_launched)

    def test_nonzero_create_result_cannot_publish_pid(self):
        fake = FakeCom(return_code=2)
        recorder = self.recorder()
        with self.assertRaisesRegex(ObsBackendError, "rc=2"):
            self.run_fake(fake, recorder)
        self.assertEqual(fake.local.refs, 0)
        self.assertIsNone(recorder.pid)

    def test_command_line_quoting_preserved(self):
        fake = FakeCom()
        recorder = self.run_fake(fake)
        line = next(row[2][2] for row in fake.events if row[1] == "create")
        self.assertEqual(line, subprocess.list2cmdline([str(recorder.exe), "--outputs-module",
            "private directory/outputs.dll", "--log", str(recorder.log_path)]))

    def test_successful_create_then_identity_failure_retains_pid(self):
        fake = FakeCom()
        recorder = self.recorder()
        with patch.dict(sys.modules, fake.modules()), \
                patch.object(recorder, '_remember_native_identity', side_effect=RuntimeError('identity unavailable')), \
                self.assertRaisesRegex(RuntimeError, 'identity unavailable'):
            recorder._spawn_wmi([str(recorder.exe)])
        self.assertEqual(recorder.pid, 4242)
        self.assertTrue(recorder.wmi_launched)
        self.assertIsNone(recorder.native_identity)
        self.assertEqual(fake.local.refs, 0)

    def test_com_cleanup_failure_after_create_retains_pid(self):
        fake = FakeCom()
        recorder = self.recorder()
        def uninitialize():
            fake.uninitialize()
            raise RuntimeError('injected COM cleanup failure')
        fake.pythoncom.CoUninitialize = uninitialize
        with patch.dict(sys.modules, fake.modules()), \
                patch.object(recorder, '_remember_native_identity'), \
                self.assertRaisesRegex(RuntimeError, 'COM cleanup failure'):
            recorder._spawn_wmi([str(recorder.exe)])
        self.assertEqual(recorder.pid, 4242)
        self.assertTrue(recorder.wmi_launched)

    def test_spawn_notification_precedes_identity_capture(self):
        fake = FakeCom()
        events = []
        recorder = ObsRecorder(Path('recorder.exe'), on_spawn=lambda owner: events.append(('notify', owner.pid)))
        with patch.dict(sys.modules, fake.modules()), \
                patch.object(recorder, '_remember_native_identity', side_effect=lambda pid: events.append(('identity', pid))):
            recorder._spawn_wmi([str(recorder.exe)])
        self.assertEqual(events, [('notify', 4242), ('identity', 4242), ('notify', 4242)])

    def test_failed_spawn_notification_keeps_wmi_pid_and_balances_com(self):
        fake = FakeCom()
        def notification(owner):
            raise OSError('owner persistence failed')
        recorder = ObsRecorder(Path('recorder.exe'), on_spawn=notification)
        with patch.dict(sys.modules, fake.modules()), \
                patch.object(recorder, '_remember_native_identity') as remember, \
                self.assertRaisesRegex(OSError, 'persistence failed'):
            recorder._spawn_wmi([str(recorder.exe)])
        remember.assert_not_called()
        self.assertEqual(recorder.pid, 4242)
        self.assertEqual(fake.local.refs, 0)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ApartmentTests))
    failures = len(result.failures) + len(result.errors)
    print(f"{result.testsRun - failures}/{result.testsRun} checks passed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
