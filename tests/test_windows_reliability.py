from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _dead_pid() -> int:
    """A PID that belonged to a process which has exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class AtomicRuntimeInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script(
            "memorysafe_runtime_installer_test",
            ROOT / "plugin" / "scripts" / "install_runtime.py",
        )

    def _plugin_dir(self, root: Path) -> Path:
        plugin = root / "plugin"
        scripts = plugin / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "runtime.env").write_text(
            "RUNTIME_KEY=abc123\nPYTHON_VERSION=3.12\nUV_VERSION=0.12.15\n", encoding="utf-8"
        )
        (plugin / "requirements.lock").write_text("", encoding="utf-8")
        return plugin

    def test_concurrent_builders_publish_one_complete_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self._plugin_dir(root)
            data_root = root / "data"
            entered = threading.Event()
            release = threading.Event()
            built = []
            results = []
            errors = []

            def fake_build(_uv, _plugin_dir, staging, _python_version, _data_root, progress=None):
                built.append(staging)
                executable = self.module.runtime_python(staging)
                executable.parent.mkdir(parents=True)
                executable.write_text("python", encoding="utf-8")
                (staging / self.module.READY_MARKER).write_text("ready", encoding="utf-8")
                entered.set()
                release.wait(timeout=2)

            def run():
                try:
                    results.append(self.module.install_uv_runtime(plugin, data_root))
                except Exception as error:
                    errors.append(error)

            with patch.object(self.module, "_ensure_uv", return_value="uv"), patch.object(
                self.module, "_build_staged_uv_runtime", side_effect=fake_build
            ):
                first = threading.Thread(target=run)
                second = threading.Thread(target=run)
                first.start()
                self.assertTrue(entered.wait(timeout=1))
                second.start()
                release.set()
                first.join(timeout=3)
                second.join(timeout=3)

            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(len(built), 1)
            runtime = data_root.resolve() / "runtime" / "abc123"
            self.assertTrue(self.module.runtime_python(runtime).is_file())
            self.assertTrue((runtime / self.module.READY_MARKER).is_file())
            self.assertFalse(runtime.with_name(".abc123.install.lock").exists())

    def test_failed_build_keeps_the_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = self._plugin_dir(root)
            data_root = root / "data"
            runtime = data_root.resolve() / "runtime" / "abc123"
            runtime.mkdir(parents=True)
            sentinel = runtime / "previous-version"
            sentinel.write_text("keep", encoding="utf-8")
            with patch.object(self.module, "_ensure_uv", return_value="uv"), patch.object(
                self.module, "_build_staged_uv_runtime", side_effect=RuntimeError("failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    self.module.install_uv_runtime(plugin, data_root)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


class StaleLockTests(unittest.TestCase):
    """A build that dies part-way leaves its lock behind.

    The lock was judged stale only after 30 minutes, while a waiting builder gave up after
    20. So the next start after a reboot or a killed build waited 20 minutes -- answering
    "try again in a minute" throughout -- then fell into limited mode. owner.json has always
    held the builder's PID; nothing read it.
    """

    def setUp(self) -> None:
        self.module = _load_script(
            "memorysafe_stale_lock_test", ROOT / "plugin" / "scripts" / "install_runtime.py"
        )
        self.temporary = tempfile.TemporaryDirectory()
        self.lock = Path(self.temporary.name) / ".abc123.install.lock"
        self.lock.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _owned_by(self, pid: int) -> None:
        (self.lock / "owner.json").write_text(
            json.dumps({"pid": pid, "created_at": time.time()}), encoding="utf-8"
        )

    def test_a_lock_whose_owner_died_is_taken_over_at_once(self) -> None:
        self._owned_by(_dead_pid())
        started = time.monotonic()
        with patch.object(self.module, "LOCK_TIMEOUT_SECONDS", 5):
            self.assertTrue(self.module._acquire_lock(self.lock, lambda: False))
        self.assertLess(time.monotonic() - started, 2)
        owner = json.loads((self.lock / "owner.json").read_text(encoding="utf-8"))
        self.assertEqual(owner["pid"], os.getpid())

    def test_a_lock_whose_owner_is_alive_is_waited_on(self) -> None:
        self._owned_by(os.getpid())
        with patch.object(self.module, "LOCK_TIMEOUT_SECONDS", 0.5):
            with self.assertRaises(TimeoutError):
                self.module._acquire_lock(self.lock, lambda: False)

    def test_a_lock_without_an_owner_is_judged_by_age_alone(self) -> None:
        """mkdir and the owner.json write are two steps; in between, a live lock has no owner."""
        with patch.object(self.module, "LOCK_TIMEOUT_SECONDS", 0.5):
            with self.assertRaises(TimeoutError):
                self.module._acquire_lock(self.lock, lambda: False)
        old = time.time() - self.module.STALE_LOCK_SECONDS - 60
        os.utime(self.lock, (old, old))
        with patch.object(self.module, "LOCK_TIMEOUT_SECONDS", 5):
            self.assertTrue(self.module._acquire_lock(self.lock, lambda: False))


if __name__ == "__main__":
    unittest.main()
