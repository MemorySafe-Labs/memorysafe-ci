"""The device link: one sign-in per computer to the hosted account, kept owner-only.

What these tests pin down is the safe side of the link. Nothing is written until a sign-in
finishes; a callback that this process did not start is refused; a refresh the server
refuses deletes the link instead of retrying (a replay would sign every computer out); being
offline keeps the link; sign-out deletes the file even when the server cannot be reached;
choosing "On this computer" again signs out; and no token ever appears in what the
dashboard or the doctor are given.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import client_identity, device_link, storage_mode

ORIGIN = "https://memory.test"


class FakeHosted:
    """The three endpoints the link talks to, as a callable with the HttpCall signature.

    Issues rotating refresh tokens and remembers which were spent, like the real server."""

    def __init__(self, *, metadata: bool = True, offline: bool = False) -> None:
        self.metadata = metadata
        self.offline = offline
        self.calls: list[tuple[str, dict | None]] = []
        self.issued = 0
        self.spent: set[str] = set()
        self.revoked: list[str] = []
        self.refuse_refresh = False

    def __call__(self, url: str, form: dict | None, headers: dict) -> tuple[int, dict]:
        self.calls.append((url, form))
        if self.offline:
            raise OSError("no route to host")
        path = urllib.parse.urlsplit(url).path
        if path == client_identity.METADATA_PATH:
            if not self.metadata:
                return 404, {}
            return 200, {
                "issuer": ORIGIN,
                "authorization_endpoint": f"{ORIGIN}/authorize",
                "token_endpoint": f"{ORIGIN}/token",
                "revocation_endpoint": f"{ORIGIN}/revoke",
                "client_id_metadata_document_supported": True,
            }
        if path == "/token":
            assert form is not None
            if form["grant_type"] == "authorization_code":
                if form.get("code") != "good-code" or not form.get("code_verifier"):
                    return 400, {"error": "invalid_grant"}
                return 200, self._tokens()
            if form["grant_type"] == "refresh_token":
                if self.refuse_refresh or form["refresh_token"] in self.spent:
                    return 400, {"error": "invalid_grant"}
                self.spent.add(form["refresh_token"])
                return 200, self._tokens()
            return 400, {"error": "unsupported_grant_type"}
        if path == "/revoke":
            assert form is not None
            self.revoked.append(form["token"])
            return 200, {}
        return 404, {}

    def _tokens(self) -> dict:
        self.issued += 1
        return {"access_token": f"access-{self.issued}", "token_type": "Bearer", "expires_in": 3600,
                "refresh_token": f"refresh-{self.issued}", "scope": "memories offline_access"}


class LinkFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"
        self.hosted = FakeHosted()
        import time

        # Real "now": the record's dates are written with the wall clock, and staleness
        # compares the two, so the fake clock starts where the wall clock is.
        self.clock = [time.time()]
        self.now = lambda: self.clock[0]
        device_link._pending.clear()

    def _sign_in(self) -> dict:
        begun = device_link.begin_sign_in(8765, origin=ORIGIN, http=self.hosted, now=self.now)
        return device_link.finish_sign_in(self.state_dir, code="good-code", state=begun["state"], iss=ORIGIN,
                                          http=self.hosted, now=self.now)

    def test_the_client_document_is_what_the_server_expects_of_a_public_client(self) -> None:
        document = client_identity.client_document()
        self.assertEqual(document["client_id"], client_identity.CLIENT_ID)
        self.assertTrue(client_identity.CLIENT_ID.startswith("https://"))
        self.assertEqual(document["token_endpoint_auth_method"], "none")
        self.assertIn("refresh_token", document["grant_types"])
        for uri in document["redirect_uris"]:
            parsed = urllib.parse.urlsplit(uri)
            self.assertEqual((parsed.scheme, parsed.port, parsed.path), ("http", None, client_identity.CALLBACK_PATH), uri)
            self.assertIn(parsed.hostname, {"127.0.0.1", "localhost"})
        # The runtime redirect is one of the registered ones with the port the dashboard got.
        runtime = urllib.parse.urlsplit(client_identity.redirect_uri(54321))
        self.assertEqual((runtime.hostname, runtime.port, runtime.path), ("127.0.0.1", 54321, client_identity.CALLBACK_PATH))

    def test_beginning_a_sign_in_writes_nothing_and_asks_with_pkce(self) -> None:
        begun = device_link.begin_sign_in(8765, origin=ORIGIN, http=self.hosted, now=self.now)
        self.assertFalse(device_link.link_path(self.state_dir).exists())
        url = urllib.parse.urlsplit(begun["url"])
        query = dict(urllib.parse.parse_qsl(url.query))
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "memory.test", "/authorize"))
        self.assertEqual(query["client_id"], client_identity.CLIENT_ID)
        self.assertEqual(query["redirect_uri"], "http://127.0.0.1:8765/callback")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertEqual(query["scope"], "memories offline_access")
        self.assertEqual(query["resource"], f"{ORIGIN}/mcp")
        self.assertEqual(query["state"], begun["state"])
        self.assertGreaterEqual(len(query["code_challenge"]), 43)

    def test_the_metadata_being_unreadable_falls_back_to_the_standard_paths(self) -> None:
        self.hosted.metadata = False
        begun = device_link.begin_sign_in(8765, origin=ORIGIN, http=self.hosted, now=self.now)
        self.assertTrue(begun["url"].startswith(f"{ORIGIN}/authorize?"))

    def test_finishing_a_sign_in_exchanges_the_code_and_keeps_the_link_owner_only(self) -> None:
        status = self._sign_in()
        self.assertTrue(status["linked"])
        self.assertFalse(status["stale"])
        self.assertNotIn("refresh_token", json.dumps(status))
        self.assertNotIn("access_token\"", json.dumps(status))
        self.assertNotIn("access-1", json.dumps(status))
        path = device_link.link_path(self.state_dir)
        self.assertTrue(path.is_file())
        import os

        if os.name != "nt":
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        record = json.loads(path.read_text())
        self.assertEqual(record["refresh_token"], "refresh-1")
        self.assertEqual(record["client_id"], client_identity.CLIENT_ID)
        token_call = next(form for url, form in self.hosted.calls if form and form.get("grant_type") == "authorization_code")
        self.assertEqual(token_call["redirect_uri"], "http://127.0.0.1:8765/callback")
        self.assertEqual(token_call["resource"], f"{ORIGIN}/mcp")

    def test_a_callback_this_process_did_not_start_is_refused_before_any_token_call(self) -> None:
        for state in ("", "never-issued"):
            with self.assertRaises(device_link.LinkError):
                device_link.finish_sign_in(self.state_dir, code="good-code", state=state, iss=ORIGIN, http=self.hosted, now=self.now)
        self.assertFalse(any(form for _url, form in self.hosted.calls if form))
        self.assertFalse(device_link.link_path(self.state_dir).exists())

    def test_a_callback_that_names_no_issuer_is_refused(self) -> None:
        """The server always sends `iss`; a callback without it did not come from the server."""
        begun = device_link.begin_sign_in(8765, origin=ORIGIN, http=self.hosted, now=self.now)
        with self.assertRaises(device_link.LinkError):
            device_link.finish_sign_in(self.state_dir, code="good-code", state=begun["state"], iss=None,
                                       http=self.hosted, now=self.now)
        self.assertFalse(device_link.link_path(self.state_dir).exists())

    def test_a_callback_from_another_issuer_is_refused(self) -> None:
        begun = device_link.begin_sign_in(8765, origin=ORIGIN, http=self.hosted, now=self.now)
        with self.assertRaises(device_link.LinkError):
            device_link.finish_sign_in(self.state_dir, code="good-code", state=begun["state"], iss="https://evil.test",
                                       http=self.hosted, now=self.now)
        self.assertFalse(device_link.link_path(self.state_dir).exists())

    def test_a_sign_in_left_too_long_is_forgotten(self) -> None:
        begun = device_link.begin_sign_in(8765, origin=ORIGIN, http=self.hosted, now=self.now)
        self.clock[0] += device_link.PENDING_SECONDS + 1
        with self.assertRaises(device_link.LinkError):
            device_link.finish_sign_in(self.state_dir, code="good-code", state=begun["state"], iss=ORIGIN, http=self.hosted, now=self.now)

    def test_a_bad_code_leaves_no_link(self) -> None:
        begun = device_link.begin_sign_in(8765, origin=ORIGIN, http=self.hosted, now=self.now)
        with self.assertRaises(device_link.LinkError):
            device_link.finish_sign_in(self.state_dir, code="stolen", state=begun["state"], iss=ORIGIN, http=self.hosted, now=self.now)
        self.assertFalse(device_link.link_path(self.state_dir).exists())

    def test_a_fresh_access_token_is_used_without_a_network_call(self) -> None:
        self._sign_in()
        calls = len(self.hosted.calls)
        self.assertEqual(device_link.access_token(self.state_dir, http=self.hosted, now=self.now), "access-1")
        self.assertEqual(len(self.hosted.calls), calls)

    def test_an_expiring_access_token_is_refreshed_and_the_rotated_token_kept(self) -> None:
        self._sign_in()
        self.clock[0] += 3600 - 30  # inside the margin
        self.assertEqual(device_link.access_token(self.state_dir, http=self.hosted, now=self.now), "access-2")
        record = json.loads(device_link.link_path(self.state_dir).read_text())
        self.assertEqual(record["refresh_token"], "refresh-2")
        self.assertIn("last_refreshed_at", record)
        self.assertEqual(record["linked_at"], device_link.status(self.state_dir, self.now)["linked_at"])

    def test_a_refused_refresh_deletes_the_link_and_says_sign_in_again(self) -> None:
        self._sign_in()
        self.clock[0] += 4000
        self.hosted.refuse_refresh = True
        with self.assertRaises(device_link.LinkError) as caught:
            device_link.access_token(self.state_dir, http=self.hosted, now=self.now)
        self.assertEqual(str(caught.exception), device_link.SIGN_IN_AGAIN)
        self.assertFalse(device_link.link_path(self.state_dir).exists())
        # And it is not retried: one refresh call, no second attempt that would trip the
        # server's replay rule and sign every computer out.
        refreshes = [form for _url, form in self.hosted.calls if form and form.get("grant_type") == "refresh_token"]
        self.assertEqual(len(refreshes), 1)

    def test_being_offline_keeps_the_link(self) -> None:
        self._sign_in()
        self.clock[0] += 4000
        self.hosted.offline = True
        with self.assertRaises(device_link.LinkError) as caught:
            device_link.access_token(self.state_dir, http=self.hosted, now=self.now)
        self.assertEqual(str(caught.exception), device_link.OFFLINE)
        self.assertTrue(caught.exception.linked)
        self.assertTrue(device_link.link_path(self.state_dir).exists())

    def test_no_link_says_so(self) -> None:
        with self.assertRaises(device_link.LinkError) as caught:
            device_link.access_token(self.state_dir, http=self.hosted, now=self.now)
        self.assertEqual(str(caught.exception), device_link.NOT_LINKED)

    def test_sign_out_revokes_then_deletes(self) -> None:
        self._sign_in()
        result = device_link.sign_out(self.state_dir, http=self.hosted)
        self.assertEqual(result, {"linked": False, "revoked": True, "reached": True})
        self.assertEqual(self.hosted.revoked, ["refresh-1"])
        self.assertFalse(device_link.link_path(self.state_dir).exists())

    def test_sign_out_deletes_even_when_the_server_cannot_be_reached(self) -> None:
        self._sign_in()
        self.hosted.offline = True
        result = device_link.sign_out(self.state_dir, http=self.hosted)
        self.assertEqual(result, {"linked": False, "revoked": False, "reached": False})
        self.assertFalse(device_link.link_path(self.state_dir).exists())

    def test_a_link_older_than_the_servers_refresh_lifetime_reads_as_stale(self) -> None:
        self._sign_in()
        self.clock[0] += device_link.REFRESH_LIFETIME_SECONDS + 60
        self.assertTrue(device_link.status(self.state_dir, self.now)["stale"])

    def test_a_broken_record_is_no_link(self) -> None:
        device_link.link_path(self.state_dir).parent.mkdir(parents=True)
        device_link.link_path(self.state_dir).write_text("{not json")
        self.assertEqual(device_link.status(self.state_dir, self.now), {"linked": False})
        device_link.link_path(self.state_dir).write_text(json.dumps({"issuer": ORIGIN}))
        self.assertEqual(device_link.status(self.state_dir, self.now), {"linked": False})

    def test_only_https_is_ever_called(self) -> None:
        with self.assertRaises(device_link.LinkError):
            device_link._http("http://memory.test/token", {"a": "b"}, {})


class LinkEndpointTests(unittest.TestCase):
    """The dashboard's side: start, callback, sign-out, and the mode switch that signs out."""

    def setUp(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        from memorysafe_chatgpt import setup_app as setup_app_module
        from memorysafe_chatgpt.setup_app import SetupPaths

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
        inventory_patch = patch.object(setup_app_module.agents_module, "inventory", return_value=[])
        inventory_patch.start()
        self.addCleanup(inventory_patch.stop)
        self.hosted = FakeHosted()
        http_patch = patch.object(device_link, "_http", self.hosted)
        http_patch.start()
        self.addCleanup(http_patch.stop)
        origin_patch = patch.object(client_identity, "HOSTED_ORIGIN", ORIGIN)
        origin_patch.start()
        self.addCleanup(origin_patch.stop)
        device_link._pending.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup_app_module.SetupHandler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.token = setup_app_module.SetupHandler.setup_token

    def _request(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-MemorySafe-Setup-Token"] = token
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            return response.status, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return response.status, raw.decode("utf-8")

    def _start(self) -> dict:
        storage_mode.write_storage_mode(self.paths.state_dir, "hosted")
        status, payload = self._request("POST", "/api/link/start", {}, self.token)
        self.assertEqual(status, 200, payload)
        return payload

    def test_sign_in_is_only_offered_in_hosted_mode(self) -> None:
        status, payload = self._request("POST", "/api/link/start", {}, self.token)
        self.assertEqual(status, 409, payload)
        self.assertFalse(any(form for _url, form in self.hosted.calls if form))

    def test_start_needs_this_pages_token(self) -> None:
        storage_mode.write_storage_mode(self.paths.state_dir, "hosted")
        for token in (None, "", "nope"):
            status, _payload = self._request("POST", "/api/link/start", {}, token)
            self.assertEqual(status, 403, repr(token))

    def test_start_returns_the_hosted_authorize_url_with_this_dashboards_port(self) -> None:
        payload = self._start()
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(payload["url"]).query))
        self.assertEqual(query["redirect_uri"], f"http://127.0.0.1:{self.port}/callback")
        self.assertTrue(payload["url"].startswith(f"{ORIGIN}/authorize?"))

    def test_the_callback_finishes_the_sign_in_and_the_agents_payload_shows_it(self) -> None:
        payload = self._start()
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(payload["url"]).query))["state"]
        status, page = self._request("GET", f"/callback?code=good-code&state={state}&iss={urllib.parse.quote(ORIGIN, safe='')}")
        self.assertEqual(status, 200, page)
        self.assertIn("Signed in", page)
        self.assertTrue(device_link.link_path(self.paths.state_dir).is_file())
        status, agents = self._request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertTrue(agents["device_link"]["linked"])
        for secret in ("refresh_token", "refresh-1", "access-1"):
            self.assertNotIn(secret, json.dumps(agents))
        self.assertEqual(agents["local_memories"], 0)

    def test_a_callback_nobody_started_is_refused(self) -> None:
        storage_mode.write_storage_mode(self.paths.state_dir, "hosted")
        status, page = self._request("GET", "/callback?code=good-code&state=made-up")
        self.assertEqual(status, 400)
        self.assertIn("not started from this dashboard", page)
        self.assertFalse(device_link.link_path(self.paths.state_dir).exists())

    def test_a_sign_in_left_open_does_not_finish_after_coming_back_to_this_computer(self) -> None:
        """The tab on the account stays open while the person changes their mind. Choosing
        this computer again must not be followed by a token arriving on it."""
        payload = self._start()
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(payload["url"]).query))["state"]
        status, _payload = self._request("POST", "/api/storage-mode", {"mode": "local"}, self.token)
        self.assertEqual(status, 200)
        callback = f"/callback?code=good-code&state={state}&iss={urllib.parse.quote(ORIGIN, safe='')}"
        status, page = self._request("GET", callback)
        self.assertEqual(status, 400)
        self.assertIn("live on this computer", page)
        # Back in hosted mode the old tab is still no use: the sign-in it belonged to was dropped.
        storage_mode.write_storage_mode(self.paths.state_dir, "hosted")
        status, page = self._request("GET", callback)
        self.assertEqual(status, 400)
        self.assertFalse(device_link.link_path(self.paths.state_dir).exists())
        self.assertFalse([url for url, form in self.hosted.calls if form is not None])  # no code was exchanged

    def test_the_hosted_side_saying_no_is_reported_without_a_token_call(self) -> None:
        self._start()
        status, page = self._request("GET", "/callback?error=access_denied&state=whatever")
        self.assertEqual(status, 400)
        self.assertIn("did not complete", page)
        self.assertFalse(any(form for _url, form in self.hosted.calls if form and form.get("grant_type")))

    def test_sign_out_from_the_dashboard(self) -> None:
        self._complete_sign_in()
        status, payload = self._request("POST", "/api/link/signout", {}, self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["signed_out"], {"linked": False, "revoked": True, "reached": True})
        self.assertFalse(payload["device_link"]["linked"])
        self.assertFalse(device_link.link_path(self.paths.state_dir).exists())

    def test_choosing_this_computer_again_signs_out(self) -> None:
        self._complete_sign_in()
        status, payload = self._request("POST", "/api/storage-mode", {"mode": "local"}, self.token)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["storage_mode"], "local")
        self.assertTrue(payload["signed_out"]["revoked"])
        self.assertFalse(device_link.link_path(self.paths.state_dir).exists())
        self.assertEqual(self.hosted.revoked, ["refresh-1"])

    def test_choosing_hosted_does_not_sign_anything(self) -> None:
        status, payload = self._request("POST", "/api/storage-mode", {"mode": "hosted"}, self.token)
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["signed_out"])
        self.assertFalse(payload["device_link"]["linked"])
        self.assertEqual(self.hosted.calls, [])

    def test_the_agents_payload_counts_the_memories_here(self) -> None:
        from memorysafe_chatgpt.storage import MemoryStore

        store = MemoryStore(self.paths.database_path)
        store.remember("The Gama permit is still pending", "project")
        store.remember("The team prefers plain answers", "preference")
        status, payload = self._request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(payload["local_memories"], 2)

    def test_counting_the_memories_closes_the_store(self) -> None:
        """A connection left open kept the store's file locked on Windows, where an open
        file can be neither moved nor deleted."""
        import sqlite3

        from memorysafe_chatgpt import setup_app as setup_app_module
        from memorysafe_chatgpt.storage import MemoryStore

        MemoryStore(self.paths.database_path).remember("The Gama permit is still pending", "project")
        closed = []

        class Tracked(sqlite3.Connection):
            def close(self) -> None:
                closed.append(True)
                super().close()

        real = sqlite3.connect
        with patch.object(sqlite3, "connect", lambda *args, **kwargs: real(*args, factory=Tracked, **kwargs)):
            self.assertEqual(setup_app_module._active_count(self.paths), 1)
        self.assertEqual(closed, [True])

    def _complete_sign_in(self) -> None:
        payload = self._start()
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(payload["url"]).query))["state"]
        status, _page = self._request("GET", f"/callback?code=good-code&state={state}&iss={urllib.parse.quote(ORIGIN, safe='')}")
        self.assertEqual(status, 200)


class DoctorAndDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"

    def _paths(self):
        from memorysafe_chatgpt.doctor import DoctorPaths

        root = Path(self.temporary.name)
        return DoctorPaths(install_root=root, database_path=root / "db", state_dir=self.state_dir,
                           secret_file=root / "s", tunnel_id_file=root / "t", health_url_file=root / "h")

    def test_the_doctor_says_nothing_about_the_link_in_local_mode(self) -> None:
        from memorysafe_chatgpt.doctor import _device_link_check

        self.assertIsNone(_device_link_check(self._paths()))

    def test_the_doctor_warns_when_hosted_is_chosen_but_not_signed_in(self) -> None:
        from memorysafe_chatgpt.doctor import _device_link_check

        storage_mode.write_storage_mode(self.state_dir, "hosted")
        check = _device_link_check(self._paths())
        self.assertEqual((check["id"], check["status"], check["summary"]), ("device_link", "warning", device_link.NOT_LINKED))

    def test_the_doctor_passes_a_live_link_and_warns_on_a_stale_one_without_tokens(self) -> None:
        from memorysafe_chatgpt.device import write_private_json
        from memorysafe_chatgpt.doctor import _device_link_check

        storage_mode.write_storage_mode(self.state_dir, "hosted")
        write_private_json(device_link.link_path(self.state_dir), {
            "issuer": ORIGIN, "token_endpoint": f"{ORIGIN}/token", "refresh_token": "r", "access_token": "a",
            "access_expires_at": 0, "linked_at": "2026-11-01T15:00:00+00:00",
        })
        import time
        from datetime import datetime, timezone

        today = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc).timestamp()  # eight days after linked_at
        with patch.object(device_link.time, "time", return_value=today):
            check = _device_link_check(self._paths())
        self.assertEqual(check["status"], "pass")
        for secret in ("refresh_token", "access_token", '"r"', '"a"'):
            self.assertNotIn(secret, json.dumps(check["details"]))
        with patch.object(device_link.time, "time", return_value=today + device_link.REFRESH_LIFETIME_SECONDS):
            stale = _device_link_check(self._paths())
        self.assertEqual((stale["status"], stale["summary"]), ("warning", device_link.SIGN_IN_AGAIN))

    def test_the_dashboard_offers_sign_in_and_says_what_sign_out_does(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        html = dashboard_html(local_api_token="t")
        for text in (
            'id="link-signin"', 'id="link-signout"', "/api/link/start", "/api/link/signout",
            # Sign-out reaches every computer; the page says so before the click.
            "on every computer where you use it",
            # Switching back to local tells the person what happened to the link.
            "sign-in to your hosted account was ended",
            # Memories left behind are named at the switch.
            "until you come back or delete",
        ):
            self.assertIn(text, html)
        hosted = dashboard_html(local_api_token="t", hosted=True, account_url="https://memory.memorysafe.ca/account")
        self.assertNotIn("this computer", hosted)




class PublishedClientDocumentTests(unittest.TestCase):
    """The file the website serves is the one the code describes, byte for byte."""

    def test_the_website_copy_is_client_document(self) -> None:
        root = Path(__file__).resolve().parents[1]
        published = root / "plugin" / "website" / "oauth" / "memorysafe-plugin.json"
        expected = json.dumps(client_identity.client_document(), indent=2, sort_keys=True) + "\n"
        self.assertEqual(published.read_text(encoding="utf-8"), expected)
        self.assertEqual(urllib.parse.urlsplit(client_identity.CLIENT_ID).path, "/oauth/memorysafe-plugin.json")
        headers = (root / "plugin" / "website" / "_headers").read_text(encoding="utf-8")
        self.assertIn("/oauth/memorysafe-plugin.json\n  Content-Type: application/json", headers)


if __name__ == "__main__":
    unittest.main()
