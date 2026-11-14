"""Where memories live is one recorded choice, and nothing happens until it is made.

MemorySafe is one product with two places a memory can live: this computer, or the
person's hosted account. The dashboard records the person's choice; the record moves no
memories and connects nothing. What these tests pin down is the safe side of that:
the default is local, a broken record is local, and a hosted choice stops the start-up
auto-connect from installing the local plugin the person has decided against.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import agents, storage_mode
from memorysafe_chatgpt.migrate import PLUGIN_ID


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class StorageModeRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"

    def test_nothing_decided_reads_as_local(self) -> None:
        """Local is what MemorySafe does when the person has decided nothing."""
        self.assertEqual(storage_mode.read_storage_mode(self.state_dir), "local")

    def test_the_choice_survives(self) -> None:
        storage_mode.write_storage_mode(self.state_dir, "hosted")
        self.assertEqual(storage_mode.read_storage_mode(self.state_dir), "hosted")
        storage_mode.write_storage_mode(self.state_dir, "local")
        self.assertEqual(storage_mode.read_storage_mode(self.state_dir), "local")

    def test_a_broken_record_reads_as_local(self) -> None:
        """A half-written file must not strand the person in a mode they did not choose."""
        for text in ("", "{not json", json.dumps({"mode": "cloud"}), json.dumps({"mode": 1}), json.dumps([])):
            _write(self.state_dir / storage_mode.MODE_FILE, text)
            self.assertEqual(storage_mode.read_storage_mode(self.state_dir), "local", repr(text))

    def test_only_the_two_modes_are_accepted(self) -> None:
        for mode in ("cloud", "", "HOSTED", None, True, 1):
            with self.assertRaises(ValueError, msg=repr(mode)):
                storage_mode.write_storage_mode(self.state_dir, mode)  # type: ignore[arg-type]
        self.assertFalse((self.state_dir / storage_mode.MODE_FILE).exists())


class HostedModeStopsTheStartupConnectTests(unittest.TestCase):
    """The old yes to "connect your other assistants too?" was a yes to the local plugin.

    Acting on it after the person chose their hosted account would install what they
    decided against, at every start of the dashboard service.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.bin = Path(self.temporary.name) / "bin"
        self.bin.mkdir()
        self.state_dir = Path(self.temporary.name) / "state"
        # An empty PATH and no fallback folders: no test may find a real claude or codex.
        self.env = {"PATH": str(self.bin)}
        for name, value in (("_POSIX_BIN_DIRS", ()), ("_MAC_APP_DIRS", ())):
            patcher = patch.object(agents, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # A connectable Claude Code: the CLI on PATH and ~/.claude present.
        cli = self.bin / ("claude.exe" if agents.sys.platform == "win32" else "claude")
        cli.write_text("", encoding="utf-8")
        cli.chmod(0o755)
        (self.home / ".claude").mkdir()
        self.calls: list = []

    def _runner(self, command, **kwargs):
        self.calls.append(command)
        if "install" in command:
            _write(
                self.home / ".claude" / "plugins" / "installed_plugins.json",
                json.dumps({"plugins": {PLUGIN_ID: [{}]}}),
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def _on_start(self) -> list:
        return agents.auto_connect_on_start(self.state_dir, self.home, self.env, agents.sys.platform, runner=self._runner)

    def test_a_hosted_choice_stops_the_startup_connect(self) -> None:
        agents.write_auto_connect(self.state_dir, True)
        storage_mode.write_storage_mode(self.state_dir, "hosted")
        self.assertEqual(self._on_start(), [])
        self.assertEqual(self.calls, [])

    def test_choosing_local_again_connects_as_before(self) -> None:
        """Hosted settles nothing: the assistant was skipped, not tried, so a return to
        local connects it at the next start instead of remembering a try that never ran."""
        agents.write_auto_connect(self.state_dir, True)
        storage_mode.write_storage_mode(self.state_dir, "hosted")
        self.assertEqual(self._on_start(), [])
        storage_mode.write_storage_mode(self.state_dir, "local")
        results = self._on_start()
        self.assertTrue(results and results[0]["ok"])
        self.assertTrue(self.calls)


class StorageModeEndpointTests(unittest.TestCase):
    """The choice is saved from the dashboard, to this computer only, with this page's token."""

    def setUp(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        from memorysafe_chatgpt import setup_app as setup_app_module
        from memorysafe_chatgpt.setup_app import SetupPaths

        self.setup_app_module = setup_app_module
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = SetupPaths(
            install_root=root, state_dir=root / "runtime-state", database_path=root / "data" / "memorysafe.sqlite3",
            secret_file=root / ".secrets" / "key", tunnel_id_file=root / "tunnel-id",
            health_url_file=root / "tunnel.url", legal_dir=root / "legal", connect_url="https://example.invalid",
        )
        paths_patch = patch.object(setup_app_module.SetupHandler, "paths", self.paths)
        paths_patch.start()
        self.addCleanup(paths_patch.stop)
        # The endpoints answer with the assistants found on the machine running the test;
        # these tests are about the mode record, so the inventory is pinned empty.
        inventory_patch = patch.object(setup_app_module.agents_module, "inventory", return_value=[])
        inventory_patch.start()
        self.addCleanup(inventory_patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup_app_module.SetupHandler)
        # server_close() waits only for handler threads that are not daemons. A request this
        # test gave up on must have finished writing the store before the folder under it is
        # removed: on Windows a file still open cannot be deleted.
        self.server.daemon_threads = False
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.token = setup_app_module.SetupHandler.setup_token

    def _request(self, method: str, path: str, body: dict | None = None, token: str | None = None,
                 timeout: float = 30) -> tuple[int, dict]:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=timeout)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-MemorySafe-Setup-Token"] = token
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, payload

    def test_the_agents_payload_carries_the_mode(self) -> None:
        status, payload = self._request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(payload["storage_mode"], "local")

    def test_the_choice_is_saved_and_read_back(self) -> None:
        status, payload = self._request("POST", "/api/storage-mode", {"mode": "hosted"}, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["storage_mode"], "hosted")
        self.assertEqual(storage_mode.read_storage_mode(self.paths.state_dir), "hosted")
        status, payload = self._request("GET", "/api/agents")
        self.assertEqual(payload["storage_mode"], "hosted")

    def test_it_needs_this_pages_token(self) -> None:
        for token in (None, "", "not-the-token"):
            status, _payload = self._request("POST", "/api/storage-mode", {"mode": "hosted"}, token)
            self.assertEqual(status, 403, repr(token))
        self.assertEqual(storage_mode.read_storage_mode(self.paths.state_dir), "local")

    def test_only_the_two_modes_are_accepted(self) -> None:
        for mode in ("cloud", "", None, True, 1, ["hosted"]):
            status, _payload = self._request("POST", "/api/storage-mode", {"mode": mode}, self.token)
            self.assertEqual(status, 400, repr(mode))
        self.assertEqual(storage_mode.read_storage_mode(self.paths.state_dir), "local")

    def test_the_dashboard_offers_the_choice(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        html = dashboard_html(local_api_token="local-test-token")
        for text in (
            "MEMORIES LIVE",
            "/api/storage-mode",
            "mode-hosted",
            storage_mode.HOSTED_CONNECT_URL,
            # The account's connections cannot be checked from this computer, so the page
            # must say so rather than show a state it cannot know (false ambers, issue #44).
            "cannot be checked from here",
            # The choice moves nothing; the export path is the move.
            "This choice moves no memories",
        ):
            self.assertIn(text, html)


class ImportEndpointTests(StorageModeEndpointTests):
    """The way back from Hosted: the account page's move file, imported from the dashboard.

    The hosted account's data page hands out the same v1 move file that `memorysafe import`
    reads; /api/import is that command behind a button. The file travels as the request
    body, so no path from a browser touches this computer's disk, and every memory goes
    through remember(), so duplicates merge and nothing changes silently. In hosted mode
    the local store is not written: memories live in one place or the other.
    """

    def _move_file(self, *contents: str) -> dict:
        return {
            "format": "memorysafe-export",
            "format_version": 1,
            "memories": [
                {
                    "content": content,
                    "category": "project",
                    "protected": number == 1,
                    "source": "hosted",
                    "created_at": f"2026-03-0{number}T10:00:00+00:00",
                    "updated_at": f"2026-03-0{number}T10:00:00+00:00",
                }
                for number, content in enumerate(contents, start=1)
            ],
        }

    def _active(self) -> list[dict]:
        from memorysafe_chatgpt.storage import MemoryStore

        store = MemoryStore(self.paths.database_path)
        with store._session() as connection:
            rows = connection.execute(
                "SELECT content, protected, created_at FROM memories WHERE state = 'active' ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def test_a_move_file_is_imported_with_its_dates_and_protection(self) -> None:
        body = self._move_file("The Gama permit is still pending", "The team prefers plain answers")
        status, payload = self._request("POST", "/api/import", body, self.token)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual((payload["total"], payload["stored"], payload["merged"], payload["failed"]), (2, 2, 0, 0))
        self.assertEqual(payload["protected"], 1)
        active = self._active()
        self.assertEqual([row["content"] for row in active], [m["content"] for m in body["memories"]])
        self.assertTrue(active[0]["protected"])
        # The March date arrives as March, not as today: "still pending" must not look new.
        self.assertTrue(active[0]["created_at"].startswith("2026-03-01"))

    def test_importing_twice_merges_instead_of_doubling(self) -> None:
        body = self._move_file("The Gama permit is still pending")
        self._request("POST", "/api/import", body, self.token)
        status, payload = self._request("POST", "/api/import", body, self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload["stored"], payload["merged"]), (0, 1))
        self.assertEqual(len(self._active()), 1)

    def test_a_file_that_is_not_a_move_file_is_refused_and_writes_nothing(self) -> None:
        for body in (
            {"format": "something-else", "format_version": 1, "memories": []},
            {"format": "memorysafe-export", "format_version": 2, "memories": []},
            {"format": "memorysafe-export", "format_version": 1, "memories": [{"content": ""}]},
            {"format": "memorysafe-export", "format_version": 1},
        ):
            status, payload = self._request("POST", "/api/import", body, self.token)
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)
        self.assertFalse(self.paths.database_path.exists())

    def test_it_needs_this_pages_token_too(self) -> None:
        body = self._move_file("The Gama permit is still pending")
        for token in (None, "", "not-the-token"):
            status, _payload = self._request("POST", "/api/import", body, token)
            self.assertEqual(status, 403, repr(token))
        self.assertFalse(self.paths.database_path.exists())

    def test_hosted_mode_never_writes_the_local_store(self) -> None:
        storage_mode.write_storage_mode(self.paths.state_dir, "hosted")
        body = self._move_file("The Gama permit is still pending")
        status, payload = self._request("POST", "/api/import", body, self.token)
        self.assertEqual(status, 409, payload)
        self.assertIn("On this computer", payload["error"])
        self.assertFalse(self.paths.database_path.exists())
        # Back to local, the same file imports.
        storage_mode.write_storage_mode(self.paths.state_dir, "local")
        status, payload = self._request("POST", "/api/import", body, self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["stored"], 1)

    def test_a_move_file_is_larger_than_a_setup_request(self) -> None:
        """Ordinary setup requests are capped at 16 KB; a move file holding a few hundred
        memories is past that and must still arrive whole."""

        body = self._move_file(*(f"Memory number {n} about the project, long enough to add up to a real file" for n in range(1, 400)))
        self.assertGreater(len(json.dumps(body)), 16_384)
        # About the size, not the speed: a slow machine gets the time it needs.
        status, payload = self._request("POST", "/api/import", body, self.token, timeout=180)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["total"], 399)
        self.assertEqual(payload["failed"], 0)

    def test_a_plan_key_is_entered_on_the_dashboard(self) -> None:
        """Where local storage has a limit, the dashboard says how full the store is and takes
        the plan key from the account page. The key is checked on this computer; one that
        does not check out is refused and never stored."""
        import base64
        import time

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        def b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        private = Ed25519PrivateKey.generate()
        public = b64(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        claims = b64(json.dumps({"plan": "paid", "max": 50, "exp": int(time.time()) + 86400}).encode("utf-8"))
        good = f"msplan1.{claims}.{b64(private.sign(claims.encode('ascii')))}"

        status, payload = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["local_plan"])  # no limit in this build: nothing to show

        limits = {"MEMORYSAFE_PLAN_PUBLIC_KEY": public, "MEMORYSAFE_PLAN_FREE_LIMIT": "5",
                  "MEMORYSAFE_STATE_DIR": str(self.paths.state_dir)}
        with patch.dict(os.environ, limits):
            _status, payload = self._request("GET", "/api/dashboard")
            self.assertEqual(payload["local_plan"], {"plan": "free", "used": 0, "limit": 5, "expires_at": None})
            status, _payload = self._request("POST", "/api/plan", {"key": good}, None)
            self.assertEqual(status, 403)
            status, payload = self._request("POST", "/api/plan", {"key": "msplan1.nope.nope"}, self.token)
            self.assertEqual(status, 400)
            self.assertIn("not accepted", payload["error"])
            status, payload = self._request("POST", "/api/plan", {"key": good}, self.token)
            self.assertEqual(status, 200, payload)
            self.assertEqual((payload["local_plan"]["plan"], payload["local_plan"]["limit"]), ("paid", 50))
            _status, payload = self._request("GET", "/api/dashboard")
            self.assertEqual(payload["local_plan"]["plan"], "paid")
            self.assertNotIn(good, json.dumps(payload))
            status, payload = self._request("POST", "/api/plan", {"remove": True}, self.token)
            self.assertEqual((status, payload["local_plan"]["plan"]), (200, "free"))

    def test_the_plan_panel_is_out_of_sight_until_there_is_a_plan(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        for html in (dashboard_html(local_api_token="t"),
                     dashboard_html(local_api_token="t", hosted=True, account_url="https://memory.memorysafe.ca/account")):
            self.assertIn('id="plan-panel" aria-label="Your plan" hidden', html)

    def test_the_dashboard_offers_the_way_back(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        html = dashboard_html(local_api_token="local-test-token")
        for text in (
            "Import a move file",
            "/api/import",
            'id="hosted-import-file"',
            "https://memory.memorysafe.ca/account/export",
            # Honest about what import does to what is already here.
            "duplicates merge",
        ):
            self.assertIn(text, html)
        # The hosted page is not the way back's home: its panel stays hidden, and no text
        # calls the device a computer.
        hosted = dashboard_html(local_api_token="t", hosted=True, account_url="https://memory.memorysafe.ca/account")
        self.assertNotIn("this computer", hosted)


if __name__ == "__main__":
    unittest.main()
