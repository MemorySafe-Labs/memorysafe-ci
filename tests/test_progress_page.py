"""The page on the dashboard's address during a first start: progress_page.py.

The install guide names http://127.0.0.1:8765/dashboard as the proof an install worked, and
it refused connections for the whole first build.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import Mock, patch

from memorysafe_chatgpt import claude_launcher, doctor, progress_page


ROOT = Path(__file__).resolve().parents[1]
BUILDING = {
    "status": "building",
    "step": "packages",
    "step_number": 3,
    "total_steps": 5,
    "failure": None,
    "elapsed_seconds": 72.0,
}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(port: int, path: str, timeout: float = 5.0) -> tuple:
    """(status code, body) once something answers on the port, retrying until timeout."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _refused(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
    except OSError:
        return True
    return False


def _until(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class ServingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.port = _free_port()
        self.status = dict(BUILDING)
        self.page = progress_page.ProgressPage(
            self.port, lambda: self.status, progress_page.render_page, poll_seconds=0.1
        )
        self.page.start()

    def tearDown(self) -> None:
        self.page.stop()

    def test_every_dashboard_path_shows_progress(self) -> None:
        for path in ("/", "/dashboard", "/panel"):
            code, body = _get(self.port, path)
            self.assertEqual(code, 200, path)
            self.assertIn("step 3 of 5, installing packages", body, path)

    def test_the_status_endpoint_is_not_mistaken_for_a_dashboard(self) -> None:
        """claude_launcher reads /api/status to decide whether to stop what holds the port,
        and doctor to name an older install's dashboard. Neither may act on this page."""
        code, body = _get(self.port, "/api/status")
        self.assertEqual(code, 503)
        payload = json.loads(body)
        self.assertNotIn("product", payload)
        self.assertNotIn("version", payload)
        self.assertIsNone(claude_launcher._dashboard_status("127.0.0.1", self.port))
        self.assertIsNone(doctor._dashboard_report(f"http://127.0.0.1:{self.port}"))

    def test_the_page_closes_once_the_runtime_is_ready_with_nobody_looking(self) -> None:
        """The real dashboard starts only on a free address, so this page must never
        outlive an unready runtime, whichever host's proxy happens to hold it."""
        _get(self.port, "/")
        self.status = {"status": "ready"}
        self.assertTrue(_until(lambda: _refused(self.port), timeout=2))

    def test_stop_frees_the_address_before_it_returns(self) -> None:
        _get(self.port, "/")
        self.page.stop()
        self.assertTrue(_refused(self.port))


class BindTests(unittest.TestCase):
    def test_a_busy_address_is_retried_until_it_frees(self) -> None:
        """Another host's proxy, or an older dashboard, may hold the address first."""
        port = _free_port()
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", port))
        blocker.listen()
        page = progress_page.ProgressPage(port, lambda: dict(BUILDING), progress_page.render_page, poll_seconds=0.1)
        page.start()
        try:
            time.sleep(0.3)
            blocker.close()
            code, _body = _get(port, "/dashboard", timeout=3)
            self.assertEqual(code, 200)
        finally:
            page.stop()

    def test_windows_binds_the_address_exclusively(self) -> None:
        """HTTPServer's SO_REUSEADDR lets a second socket bind a Windows port another one is
        listening on, so the page would never find the address busy and never wait."""
        server = progress_page._PageServer(("127.0.0.1", 0), BaseHTTPRequestHandler, bind_and_activate=False)
        server.socket.close()
        fake = Mock()
        server.socket = fake
        with patch.object(progress_page.os, "name", "nt"), patch.object(
            progress_page.socket, "SO_EXCLUSIVEADDRUSE", -5, create=True
        ):
            server.server_bind()
        options = [call.args[1] for call in fake.setsockopt.call_args_list]
        self.assertIn(-5, options)
        self.assertNotIn(socket.SO_REUSEADDR, options)
        fake.bind.assert_called_once_with(("127.0.0.1", 0))


class RenderTests(unittest.TestCase):
    def test_building_ticks_finished_steps_and_marks_the_current_one(self) -> None:
        page = progress_page.render_page(BUILDING)
        self.assertIn('<meta http-equiv="refresh" content="2">', page)
        self.assertIn('<li class="done">&#10003; Downloading the setup tool</li>', page)
        self.assertIn('<li class="now">Installing packages&hellip;</li>', page)
        self.assertIn("<li>Checking the install</li>", page)
        self.assertIn("You do not need to download anything again.", page)
        self.assertNotIn("<script", page)

    def test_a_failure_says_why_and_what_to_do(self) -> None:
        status = dict(BUILDING, status="failed", failure="download_failed")
        page = progress_page.render_page(
            status,
            explanation=("Could not download <uv>.", ["Check the network."]),
            install_log="/data/runtime-state/logs/claude-install.log",
        )
        self.assertIn("Could not download &lt;uv&gt;.", page)
        self.assertIn("<li>Check the network.</li>", page)
        self.assertIn("run the MemorySafe doctor", page)
        self.assertIn("/data/runtime-state/logs/claude-install.log", page)

    def test_interrupted_says_how_to_resume(self) -> None:
        self.assertIn("reopen it to resume", progress_page.render_page({"status": "interrupted"}))

    def test_the_address_follows_the_setup_port(self) -> None:
        with patch.dict(os.environ, {"MEMORYSAFE_SETUP_PORT": "9123"}):
            self.assertEqual(progress_page.setup_port(), 9123)
        with patch.dict(os.environ, {"MEMORYSAFE_SETUP_PORT": ""}):
            self.assertEqual(progress_page.setup_port(), 8765)
        with patch.dict(os.environ, {"MEMORYSAFE_SETUP_PORT": "not-a-port"}):
            self.assertEqual(progress_page.setup_port(), 8765)
        self.assertEqual(progress_page.dashboard_url(8765), "http://127.0.0.1:8765/dashboard")


class IsolationTests(unittest.TestCase):
    def test_it_imports_only_the_record_from_the_package_and_parses_as_python_3_8(self) -> None:
        """It runs inside the bootstrap proxy, on whatever python3 the machine already has."""
        source = (ROOT / "src" / "memorysafe_chatgpt" / "progress_page.py").read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 8))
        imported = set()
        relative = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative.append(node.module)
                else:
                    imported.add((node.module or "").split(".")[0])
        self.assertTrue(imported <= set(sys.stdlib_module_names) | {"__future__"}, sorted(imported))
        self.assertEqual(relative, ["provisioning"])


if __name__ == "__main__":
    unittest.main()
