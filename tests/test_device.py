from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import device as device_module
from memorysafe_chatgpt.device import ensure_device_identity


class ProcessScopedLock:
    """Stand-in for a platform lock scoped to the *process*, not the open file.

    ``fcntl.flock`` is scoped to the open file description, so on POSIX two threads
    that each ``open()`` the lock file genuinely block each other -- the file lock
    happens to also serialise threads, as a side effect of the API rather than by
    design. ``msvcrt.locking`` is scoped to the process: a second thread asking for a
    byte range the process already holds does not wait, it fails fast with
    ``OSError: [Errno 36] Resource deadlock avoided``. That is exactly the shape of
    the bug this test guards against.

    This fake reproduces the Windows failure mode (immediate failure on re-entry,
    never a wait) without needing ``msvcrt`` or Windows, so the test can prove the
    fix on any platform instead of only ever failing in Windows CI the way
    ``test_identity_is_stable_during_concurrent_status_refreshes`` in
    ``test_setup_app.py`` did.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held = False

    def acquire(self) -> None:
        with self._guard:
            if self._held:
                raise OSError(36, "Resource deadlock avoided")
            self._held = True
        # Give a second thread a real chance to race into acquire() while this
        # one is "inside" the platform call, the way a real syscall would take
        # measurable time.
        time.sleep(0.02)

    def release(self) -> None:
        with self._guard:
            self._held = False


class DeviceIdentityLockingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_concurrent_callers_are_serialised_even_when_the_platform_lock_is_process_scoped(
        self,
    ) -> None:
        """Regression test for the Windows-only EDEADLOCK from ensure_device_identity.

        The real bug only reproduces under ``msvcrt`` on Windows. Here we swap in a
        fake platform lock (``ProcessScopedLock``) that mimics msvcrt's actual
        behaviour -- reject immediately on re-entry rather than queue -- so the
        test can show, on any platform, that ``ensure_device_identity`` itself
        serialises concurrent callers instead of depending on the OS primitive
        being forgiving about it. Before the fix (no ``_IDENTITY_LOCK`` around the
        file-lock critical section), several threads can call the fake lock
        concurrently and one of them raises, exactly like the Windows traceback.
        After the fix, only one thread at a time ever reaches it, so it never
        raises, and every caller converges on one installation id.
        """

        fake_lock = ProcessScopedLock()

        def fake_lock_exclusive(_handle: object) -> None:
            fake_lock.acquire()

        def fake_unlock(_handle: object) -> None:
            fake_lock.release()

        with patch.object(
            device_module, "_lock_exclusive", side_effect=fake_lock_exclusive
        ), patch.object(device_module, "_unlock", side_effect=fake_unlock):
            with ThreadPoolExecutor(max_workers=12) as pool:
                futures = [
                    pool.submit(ensure_device_identity, self.state_dir)
                    for _ in range(24)
                ]
                identities = [future.result() for future in futures]

        installation_ids = {identity["installation_id"] for identity in identities}
        self.assertEqual(len(installation_ids), 1)

    def test_thread_lock_is_released_when_the_critical_section_raises(self) -> None:
        """The intra-process lock must not get stuck held after a failure.

        If a write mid-critical-section raises, ``_IDENTITY_LOCK`` still has to be
        released (mirroring how ``_unlock`` already runs in a ``finally``), or every
        later caller in the process would hang forever, turning one bad write into
        a permanent deadlock.
        """

        with patch.object(
            device_module, "write_private_json", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                ensure_device_identity(self.state_dir)

        self.assertFalse(device_module._IDENTITY_LOCK.locked())

        # A previous failure must not leave later callers unable to proceed.
        identity = ensure_device_identity(self.state_dir)
        self.assertIn("installation_id", identity)
