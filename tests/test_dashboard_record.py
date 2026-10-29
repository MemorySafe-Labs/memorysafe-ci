"""Who is recorded as serving the dashboard, and why it has to be the server itself.

The launcher replaces a dashboard it started from an older plugin, and only that one:
it compares the PID in the record with the PID the port reports. On Windows
`runtime/<key>/Scripts/python.exe` is a venv shim that re-execs the real interpreter,
so the PID Popen returns is the shim's and the port is bound by its child. Measured on
the machine where this was found:

    dashboard.json  -> {"pid": 25612, "version": "0.4.6"}
    /api/status     -> pid 25916        (25916's parent is 25612)

The comparison could never pass, so the stale dashboard was never replaced and an
upgraded install kept serving the old page -- through 0.4.7, 0.4.8 and 0.4.9, with
nothing to say so but one line in the doctor.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import claude_launcher, dashboard_record
from memorysafe_chatgpt.bootstrap_catalog import VERSION


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_record_names_a_pid_and_this_version(self) -> None:
        dashboard_record.write(self.state, 4242)
        record = dashboard_record.read(self.state)
        self.assertEqual((record["pid"], record["version"]), (4242, VERSION))

    def test_the_serving_process_overwrites_the_one_the_launcher_spawned(self) -> None:
        """The whole point: the last writer is the process that answers /api/status."""
        dashboard_record.write(self.state, 25612)   # the shim, from the launcher
        dashboard_record.write(self.state, 25916)   # the server, from setup_app
        self.assertEqual(dashboard_record.read(self.state)["pid"], 25916)

    def test_a_half_written_record_is_not_read(self) -> None:
        (self.state / dashboard_record.NAME).write_text('{"pid":', encoding="utf-8")
        self.assertIsNone(dashboard_record.read(self.state))

    def test_a_record_without_a_pid_is_not_read(self) -> None:
        (self.state / dashboard_record.NAME).write_text(json.dumps({"version": VERSION}), encoding="utf-8")
        self.assertIsNone(dashboard_record.read(self.state))

    def test_no_record_at_all_is_not_an_error(self) -> None:
        self.assertIsNone(dashboard_record.read(self.state))

    def test_the_launcher_reads_what_the_server_wrote(self) -> None:
        """Both sides go through this module, so the shapes cannot drift apart."""
        dashboard_record.write(self.state, 31337)
        self.assertEqual(claude_launcher._read_dashboard_record(self.state)["pid"], 31337)


class StaleReplacementNowMatchesTests(unittest.TestCase):
    """End to end: a record written by the serving process lets the check pass.

    Before the fix the record held the spawned PID and /api/status held the child's, so
    _replace_stale_dashboard returned False and the old dashboard kept the port.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _replace(self, recorded_pid: int, serving_pid: int) -> bool:
        dashboard_record.write(self.state, recorded_pid)
        # The record has to name an older plugin, or there is nothing stale to replace.
        stale = json.loads((self.state / dashboard_record.NAME).read_text(encoding="utf-8"))
        stale["version"] = "0.4.6"
        (self.state / dashboard_record.NAME).write_text(json.dumps(stale), encoding="utf-8")
        with patch.object(claude_launcher, "_process_alive", return_value=True), patch.object(
            claude_launcher, "_dashboard_status", return_value={"version": "0.4.6", "pid": serving_pid}
        ), patch.object(claude_launcher, "_dashboard_is_running", return_value=False), patch.object(
            claude_launcher.os, "kill"
        ), patch.object(claude_launcher.subprocess, "run"), patch.object(claude_launcher.time, "sleep"):
            return claude_launcher._replace_stale_dashboard("127.0.0.1", 8765, self.state)

    def test_the_serving_pid_in_the_record_is_replaced(self) -> None:
        self.assertTrue(self._replace(recorded_pid=25916, serving_pid=25916))

    def test_a_shim_pid_in_the_record_is_still_refused(self) -> None:
        """The guard is unchanged: a record that does not name the serving process is
        not evidence that the process is ours, and nothing gets signalled on a guess."""
        self.assertFalse(self._replace(recorded_pid=25612, serving_pid=25916))


if __name__ == "__main__":
    unittest.main()
