"""The first-start progress record: src/memorysafe_chatgpt/provisioning.py.

For the length of a first build, nothing a person could look at worked: the dashboard address
refused connections and every call, the doctor's included, answered "try again in a minute".
The builder writes this record and the bootstrap proxy reads it back for every surface.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import provisioning


ROOT = Path(__file__).resolve().parents[1]
KEY = "abc123"


def _dead_pid() -> int:
    """A PID that belonged to a process which has exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class _DataRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data = self.temporary.name
        os.makedirs(os.path.join(self.data, "runtime"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record(self) -> dict:
        with open(provisioning.record_path(self.data, KEY), encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, **record) -> None:
        with open(provisioning.record_path(self.data, KEY), "w", encoding="utf-8") as handle:
            json.dump(record, handle)


class ReporterTests(_DataRoot):
    def test_each_step_is_recorded_with_its_number(self) -> None:
        reporter = provisioning.Reporter(self.data, KEY)
        self.assertTrue(reporter.step("packages"))
        record = self._record()
        self.assertEqual(
            (record["state"], record["step"], record["step_number"], record["total_steps"]),
            ("building", "packages", 3, 5),
        )
        self.assertEqual((record["pid"], record["runtime_key"]), (os.getpid(), KEY))
        self.assertIsNone(record["failure"])

    def test_a_failure_is_recorded_by_its_reason_at_the_step_it_stopped(self) -> None:
        reporter = provisioning.Reporter(self.data, KEY)
        reporter.step("uv")
        reporter.failed(3)
        record = self._record()
        self.assertEqual(
            (record["state"], record["step"], record["failure"]),
            ("failed", "uv", "uv_checksum_mismatch"),
        )

    def test_exit_codes_map_to_the_reasons_limited_mode_explains(self) -> None:
        self.assertEqual(provisioning.failure_reason(2), "download_failed")
        self.assertEqual(provisioning.failure_reason(3), "uv_checksum_mismatch")
        self.assertEqual(provisioning.failure_reason(4), "install_root_unwritable")
        self.assertEqual(provisioning.failure_reason(1), "runtime_build_failed")
        self.assertEqual(provisioning.failure_reason(99), provisioning.GENERIC_FAILURE)

    def test_the_record_lives_beside_the_build_lock(self) -> None:
        self.assertEqual(
            provisioning.record_path("/data", KEY),
            os.path.join("/data", "runtime", ".abc123.progress.json"),
        )

    def test_a_write_that_fails_is_reported_not_raised(self) -> None:
        """The record is for people watching. The build's exit code alone decides success."""
        os.makedirs(provisioning.record_path(self.data, KEY))
        with patch.object(provisioning.sys, "stderr") as stderr:
            self.assertFalse(provisioning.Reporter(self.data, KEY).step("uv"))
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("could not record setup progress", written)
        self.assertEqual(
            [name for name in os.listdir(os.path.join(self.data, "runtime")) if name.endswith(".tmp")],
            [],
        )


class ReadTests(_DataRoot):
    def test_a_live_builder_is_building(self) -> None:
        self._write(state="building", pid=os.getpid(), step="python", started_at=100.0)
        status = provisioning.read(self.data, KEY, now=172.0)
        self.assertEqual(
            (status["status"], status["step"], status["step_number"], status["elapsed_seconds"]),
            ("building", "python", 2, 72.0),
        )

    def test_a_dead_builder_is_interrupted(self) -> None:
        """A reboot or Task Manager ends a build with its record still saying building."""
        self._write(state="building", pid=_dead_pid(), step="packages", started_at=100.0)
        self.assertEqual(provisioning.read(self.data, KEY)["status"], "interrupted")

    def test_the_ready_marker_wins_over_the_record(self) -> None:
        self._write(state="building", pid=_dead_pid(), step="packages")
        runtime = os.path.join(self.data, "runtime", KEY)
        os.makedirs(runtime)
        open(os.path.join(runtime, provisioning.READY_MARKER), "w").close()
        self.assertEqual(provisioning.read(self.data, KEY)["status"], "ready")

    def test_a_failed_build_keeps_its_reason(self) -> None:
        self._write(state="failed", pid=os.getpid(), step="uv", failure="download_failed")
        status = provisioning.read(self.data, KEY)
        self.assertEqual((status["status"], status["failure"]), ("failed", "download_failed"))

    def test_a_missing_or_corrupt_record_is_unknown(self) -> None:
        status = provisioning.read(self.data, KEY, fallback_started_at=100.0, now=130.0)
        self.assertEqual((status["status"], status["elapsed_seconds"]), ("unknown", 30.0))
        with open(provisioning.record_path(self.data, KEY), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(provisioning.read(self.data, KEY)["status"], "unknown")
        self.assertEqual(provisioning.read("", KEY)["status"], "unknown")
        self.assertEqual(provisioning.read(self.data, "")["status"], "unknown")

    def test_elapsed_time_is_never_negative(self) -> None:
        """Several processes read the record, and they all read wall clocks."""
        self._write(state="building", pid=os.getpid(), step="uv", started_at=200.0)
        self.assertEqual(provisioning.read(self.data, KEY, now=150.0)["elapsed_seconds"], 0.0)

    def test_an_unknown_step_name_is_ignored(self) -> None:
        self._write(state="building", pid=os.getpid(), step="format_disk")
        status = provisioning.read(self.data, KEY)
        self.assertEqual((status["status"], status["step"], status["step_number"]), ("building", None, 0))


class DescribeTests(unittest.TestCase):
    def test_building_names_the_step_and_the_time(self) -> None:
        status = {"status": "building", "step": "packages", "step_number": 3, "elapsed_seconds": 72.4}
        self.assertEqual(
            provisioning.describe(status),
            "MemorySafe is installed and finishing its one-time setup: "
            "step 3 of 5, installing packages (1m 12s so far).",
        )

    def test_unknown_still_says_setup_is_running(self) -> None:
        self.assertEqual(
            provisioning.describe({"status": "unknown", "step": None, "step_number": 0, "elapsed_seconds": 9.0}),
            "MemorySafe is installed and finishing its one-time setup (9s so far).",
        )
        self.assertEqual(
            provisioning.describe({"status": "unknown", "step": None, "step_number": 0, "elapsed_seconds": None}),
            "MemorySafe is installed and finishing its one-time setup.",
        )

    def test_interrupted_says_how_to_resume(self) -> None:
        self.assertIn("reopen it to resume", provisioning.describe({"status": "interrupted"}))

    def test_failed_and_ready_are_one_line_each(self) -> None:
        self.assertEqual(
            provisioning.describe({"status": "failed"}),
            "MemorySafe could not finish its one-time setup.",
        )
        self.assertEqual(provisioning.describe({"status": "ready"}), "MemorySafe's one-time setup is finished.")

    def test_elapsed_time_reads_like_a_person_wrote_it(self) -> None:
        self.assertEqual(provisioning.format_elapsed(0.4), "0s")
        self.assertEqual(provisioning.format_elapsed(59.9), "59s")
        self.assertEqual(provisioning.format_elapsed(60.0), "1m 0s")
        self.assertEqual(provisioning.format_elapsed(754.0), "12m 34s")


class ProcessAliveTests(unittest.TestCase):
    def test_this_process_is_alive_and_a_reaped_one_is_not(self) -> None:
        self.assertTrue(provisioning.process_alive(os.getpid()))
        self.assertFalse(provisioning.process_alive(_dead_pid()))

    def test_windows_never_signals_the_process(self) -> None:
        """os.kill(pid, 0) terminates the target on Windows instead of probing it."""
        with patch.object(provisioning.os, "name", "nt"), patch.object(
            provisioning, "_windows_process_alive", return_value=True
        ) as probe, patch.object(provisioning.os, "kill") as kill:
            self.assertTrue(provisioning.process_alive(1234))
        probe.assert_called_once_with(1234)
        kill.assert_not_called()


class IsolationTests(unittest.TestCase):
    """The proxy and the builder import this on whatever python3 the machine already has."""

    def test_it_uses_only_the_standard_library_and_parses_as_python_3_8(self) -> None:
        source = (ROOT / "src" / "memorysafe_chatgpt" / "provisioning.py").read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 8))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("<relative>" if node.level else (node.module or "").split(".")[0])
        self.assertTrue(imported <= set(sys.stdlib_module_names) | {"__future__"}, sorted(imported))


if __name__ == "__main__":
    unittest.main()
