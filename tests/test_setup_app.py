from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from memorysafe_chatgpt import setup_app as setup_app_module
from memorysafe_chatgpt.dashboard import dashboard_html
from memorysafe_chatgpt.device import ensure_device_identity, masked_device_id
from memorysafe_chatgpt.setup_app import (
    SetupPaths,
    dashboard_payload,
    markdown_to_html,
    save_connection,
    save_consent,
    set_automatic_mode,
    status_payload,
    support_bundle_payload,
)


class SetupAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = SetupPaths(
            install_root=root,
            state_dir=root / "runtime-state",
            database_path=root / "data" / "memorysafe.sqlite3",
            secret_file=root / ".secrets" / "tunnel-runtime-key",
            tunnel_id_file=root / "runtime-state" / "tunnel-id",
            health_url_file=root / "runtime-state" / "health" / "tunnel.url",
            legal_dir=root / "legal",
            connect_url="https://chatgpt.com/plugins",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_consent_from_older_documents_reopens_the_agreement(self) -> None:
        """Section 15 promises notice of material changes, and the documents moved to 0.2.

        The page reads terms_accepted to mark the step done, so the version check has to
        reach it through status_payload or the promise is kept nowhere.
        """
        save_consent(self.paths, terms_accepted=True, automatic_mode=False)
        record = json.loads((self.paths.state_dir / "consent.json").read_text(encoding="utf-8"))
        self.assertTrue(status_payload(self.paths)["terms_accepted"])

        record.update(terms_version="0.1", privacy_version="0.1")
        (self.paths.state_dir / "consent.json").write_text(json.dumps(record), encoding="utf-8")
        status = status_payload(self.paths)
        self.assertFalse(status["terms_accepted"])
        self.assertTrue(status["terms_update_pending"])
        self.assertEqual(status["accepted_terms_version"], "0.1")
        self.assertEqual(status["current_terms_version"], setup_app_module.TERMS_VERSION)

    def test_a_first_run_is_not_reported_as_an_update(self) -> None:
        """Nothing accepted yet is a first run, not a change to notify anyone about."""
        status = status_payload(self.paths)
        self.assertFalse(status["terms_accepted"])
        self.assertFalse(status["terms_update_pending"])

    def test_serving_records_this_process_as_the_dashboard(self) -> None:
        """The launcher records the PID it spawned; on Windows that is a venv shim
        that re-execs the real interpreter, so it never matched the PID the port
        reports and a stale dashboard was never replaced. This is the record that
        does match, written where os.getpid() is by definition the server.
        """
        from memorysafe_chatgpt import dashboard_record

        server = MagicMock()
        server.__enter__.return_value = server
        server.server_address = ("127.0.0.1", 8765)
        with patch.object(setup_app_module, "ThreadingHTTPServer", return_value=server), patch.object(
            setup_app_module.SetupPaths, "from_environment", return_value=self.paths
        ), patch.object(setup_app_module, "_auto_connect_on_start"), patch.object(
            setup_app_module, "_warm_token_metrics"
        ), patch.object(setup_app_module, "_snapshot_on_start"):
            setup_app_module.main()
        record = dashboard_record.read(self.paths.state_dir)
        self.assertEqual(record["pid"], os.getpid())

    def test_a_state_dir_it_cannot_write_still_serves(self) -> None:
        """Failing to record who is serving is no reason to refuse to serve."""
        server = MagicMock()
        server.__enter__.return_value = server
        server.server_address = ("127.0.0.1", 8765)
        with patch.object(setup_app_module, "ThreadingHTTPServer", return_value=server), patch.object(
            setup_app_module.SetupPaths, "from_environment", return_value=self.paths
        ), patch.object(setup_app_module, "_auto_connect_on_start"), patch.object(
            setup_app_module, "_warm_token_metrics"
        ), patch.object(setup_app_module, "_snapshot_on_start"), patch.object(
            setup_app_module.dashboard_record, "write", side_effect=OSError("read-only")
        ):
            setup_app_module.main()
        server.serve_forever.assert_called_once()

    def test_identity_is_stable_and_masked(self) -> None:
        first = ensure_device_identity(self.paths.state_dir)
        second = ensure_device_identity(self.paths.state_dir)
        self.assertEqual(first["installation_id"], second["installation_id"])
        self.assertTrue(masked_device_id(first).startswith("MS-"))

    @unittest.skipIf(os.name == "nt", "asserts a live 0o600 stat on device.json; Windows returns 0o666 "
                                       "for every file regardless of what ensure_device_identity sets, "
                                       "since NTFS has no POSIX mode bits to narrow -- "
                                       "test_identity_is_stable_and_masked above still runs on Windows "
                                       "and covers identity stability and masking there")
    def test_identity_file_is_created_with_owner_only_permissions(self) -> None:
        ensure_device_identity(self.paths.state_dir)
        mode = stat.S_IMODE(os.stat(self.paths.state_dir / "device.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_identity_is_stable_during_concurrent_status_refreshes(self) -> None:
        with ThreadPoolExecutor(max_workers=12) as pool:
            identities = list(
                pool.map(
                    lambda _: ensure_device_identity(self.paths.state_dir),
                    range(48),
                )
            )
        installation_ids = {identity["installation_id"] for identity in identities}
        self.assertEqual(len(installation_ids), 1)

    def test_guided_setup_saves_consent_and_private_connection(self) -> None:
        save_consent(self.paths, terms_accepted=True, automatic_mode=True)
        save_connection(
            self.paths,
            "sk-" + ("a" * 40),
            "tunnel_1234567890abcdef",
        )
        status = status_payload(self.paths)
        self.assertTrue(status["terms_accepted"])
        self.assertTrue(status["automatic_mode"])
        self.assertTrue(status["runtime_key_saved"])
        self.assertTrue(status["tunnel_configured"])
        self.assertEqual(status["tunnel_id"], "tunnel_1234567890abcdef")
        self.assertFalse(status["connector_online"])
        self.assertNotIn("runtime_key", status)

    @unittest.skipIf(os.name == "nt", "asserts a live 0o600 stat on the secret file; Windows returns "
                                       "0o666 for every file regardless of what save_connection sets, "
                                       "since NTFS has no POSIX mode bits to narrow -- "
                                       "test_guided_setup_saves_consent_and_private_connection above "
                                       "still runs on Windows and covers the saved status shape")
    def test_guided_setup_writes_the_secret_file_owner_only(self) -> None:
        save_consent(self.paths, terms_accepted=True, automatic_mode=True)
        save_connection(
            self.paths,
            "sk-" + ("a" * 40),
            "tunnel_1234567890abcdef",
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(self.paths.secret_file).st_mode),
            0o600,
        )

    def test_save_connection_skips_the_macos_only_launchctl_call_elsewhere(self) -> None:
        """save_connection() runs from guided setup on every platform, but the
        launchctl kickstart call is launchd-only and needs os.getuid(), which
        does not exist on Windows at all. Calling it unconditionally raised an
        uncaught AttributeError there -- the (OSError, TimeoutExpired) guard
        around the subprocess.run call never got a chance to run, since the
        crash happened while building its arguments. Skip the whole call off
        of macOS instead of widening that except.
        """
        save_consent(self.paths, terms_accepted=True, automatic_mode=True)
        with patch.object(setup_app_module.sys, "platform", "win32"), patch.object(
            setup_app_module.os,
            "getuid",
            create=True,
            side_effect=AttributeError("module 'os' has no attribute 'getuid'"),
        ), patch.object(setup_app_module.subprocess, "run") as run:
            save_connection(
                self.paths,
                "sk-" + ("a" * 40),
                "tunnel_1234567890abcdef",
            )
        run.assert_not_called()
        status = status_payload(self.paths)
        self.assertTrue(status["tunnel_configured"])
        self.assertEqual(status["tunnel_id"], "tunnel_1234567890abcdef")

    def test_legal_markdown_is_escaped(self) -> None:
        rendered = markdown_to_html("# Terms\n\n- **Private**\n- <script>bad</script>")
        self.assertIn("<h1>Terms</h1>", rendered)
        self.assertIn("<strong>Private</strong>", rendered)
        self.assertNotIn("<script>", rendered)

    def test_local_dashboard_uses_real_store_and_private_local_token(self) -> None:
        save_consent(self.paths, terms_accepted=True, automatic_mode=False)
        payload = dashboard_payload(self.paths)
        self.assertEqual(payload["active_memories"], 0)
        self.assertEqual(payload["recent_memories"], [])
        self.assertFalse(payload["automatic_mode"])
        # 42.1 was pinned here while test_storage asserted the model's invariants instead,
        # because a pinned figure goes stale whenever a tool description changes.
        benchmark = payload["token_benchmark"]
        governed = benchmark["fixed_overhead_tokens"] + (
            benchmark["recalled_per_turn"] * benchmark["average_fact_tokens"]
        )
        baseline = 80 * benchmark["average_fact_tokens"]
        self.assertEqual(
            benchmark["savings_at_80_facts"],
            round(max(0.0, 100.0 * (1.0 - governed / baseline)), 1),
        )
        self.assertEqual(payload["live_token_metrics"]["active_memory_tokens"], 0)
        self.assertEqual(payload["live_token_metrics"]["counted_memories"], 0)
        self.assertIn("Automatic capture is off", payload["automatic_status"])

        local_html = dashboard_html(local_api_token="local-test-token")
        embedded_html = dashboard_html()
        self.assertIn('const localApiToken = "local-test-token";', local_html)
        self.assertIn("const localApiToken = null;", embedded_html)
        self.assertIn('fetch("/api/dashboard"', local_html)
        # The savings figure must stay labelled as a model, not live billing. The
        # dashboard redesign renamed the labels; the claim they carry is unchanged.
        self.assertIn("Modeled context per turn", local_html)
        self.assertIn("Not ChatGPT billed usage", local_html)
        self.assertIn('id="auto-status"', local_html)
        self.assertIn('id="support-bundle"', local_html)

    def test_no_dashboard_text_is_smaller_than_twelve_pixels(self) -> None:
        """Carla found the dashboard too small to read: 36 of its 45 text sizes were under 12px and 19 were
        8px. Nothing the person has to read may go below 12px again (font-size or the font shorthand)."""
        import re

        html = dashboard_html(local_api_token="local-test-token")
        css = html[html.index("<style>"):html.index("</style>")]
        sizes = [float(v) for v in re.findall(r"font-size:\s*([0-9.]+)px", css)]
        sizes += [float(v) for v in re.findall(r"font:\s*(?:[0-9]{3}\s+)?([0-9.]+)px", css)]
        self.assertTrue(sizes)
        too_small = sorted({v for v in sizes if v < 12})
        self.assertEqual(too_small, [], f"dashboard text under 12px: {too_small}")

    def test_the_dashboard_folds_what_a_tester_does_not_need_first(self) -> None:
        """The dashboard showed every panel at once. The savings comparison, most-used and the
        governance trail now fold; the governance summary still says how many need a decision, and
        hidden must beat the panel layout (the same display-vs-hidden trap the setup page had)."""
        html = dashboard_html(local_api_token="local-test-token")
        for marker in ('<details class="panel token-panel fold-panel"', 'id="governance-panel"', "<summary>"):
            self.assertIn(marker, html)
        self.assertIn('<details class="panel fold-panel" aria-label="Governance trail" id="governance-panel" hidden>', html)
        self.assertIn("[hidden] { display: none !important; }", html)
        self.assertIn("NEED${waiting === 1", html)
        self.assertIn("memories.slice(0, 3)", html)
        # Honest wording stays: the savings are a model, not billing.
        self.assertIn("Modeled context per turn", html)

    def test_dashboard_can_change_automatic_mode(self) -> None:
        save_consent(self.paths, terms_accepted=True, automatic_mode=False)
        set_automatic_mode(self.paths, True)
        self.assertTrue(status_payload(self.paths)["automatic_mode"])
        payload = dashboard_payload(self.paths)
        self.assertTrue(payload["automatic_mode"])
        self.assertIn("no candidate has reached", payload["automatic_status"])

    def test_support_bundle_is_created_locally_without_upload(self) -> None:
        payload = support_bundle_payload(self.paths)
        self.assertTrue(payload["created"])
        self.assertFalse(payload["privacy"]["memory_contents_included"])
        self.assertFalse(payload["privacy"]["runtime_keys_included"])
        self.assertFalse(payload["privacy"]["uploaded_automatically"])
        bundle = Path(payload["path"].replace("~/", f"{Path.home()}/", 1))
        self.assertTrue(bundle.is_file())

    def test_status_reports_the_catalog_version_rather_than_its_own(self) -> None:
        """The launcher compares this with the dashboard it recorded starting. A
        hardcoded string here would never change, so nothing would ever be replaced."""
        import inspect

        from memorysafe_chatgpt import setup_app
        from memorysafe_chatgpt.bootstrap_catalog import VERSION

        self.assertEqual(status_payload(self.paths)["version"], VERSION)
        self.assertNotIn('"version": "0.', inspect.getsource(setup_app.status_payload))

    def test_status_names_the_process_serving_it(self) -> None:
        """The launcher signals a recorded dashboard only when the port reports that
        same PID. A live PID beside a matching version is not proof: after a reboot the
        PID can be reused while a launchd dashboard holds the port."""
        self.assertEqual(status_payload(self.paths)["pid"], os.getpid())


class ReportAProblemTests(unittest.TestCase):
    """A tester on Windows (19 Sep) clicked Report a problem and "nothing comes out".

    The bundle was made, but the confirmation sat at the foot of the page, below Technical
    detail, and named a hidden AppData folder. The page now shows it under the button and
    hands the ZIP to the browser, which saves it to Downloads, through an endpoint that
    serves support bundles only, by exact name, to this dashboard's page only.
    """

    def setUp(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = SetupPaths(
            install_root=root, state_dir=root / "runtime-state", database_path=root / "data" / "memorysafe.sqlite3",
            secret_file=root / ".secrets" / "key", tunnel_id_file=root / "tunnel-id",
            health_url_file=root / "tunnel.url", legal_dir=root / "legal", connect_url="https://example.invalid",
        )
        self.bundles = root / "runtime-state" / "support-bundles"
        # Bundles are written under MEMORYSAFE_STATE_DIR when it is set, which on a
        # developer's machine is their real support-bundles folder.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("MEMORYSAFE_STATE_DIR", None)
        # Cleanups rather than tearDown, so a failed bind can't leave SetupHandler.paths
        # pointing at a deleted folder for the rest of the suite.
        paths_patch = patch.object(setup_app_module.SetupHandler, "paths", self.paths)
        paths_patch.start()
        self.addCleanup(paths_patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup_app_module.SetupHandler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.token = setup_app_module.SetupHandler.setup_token

    def _post(self, path: str, body: dict, token: str | None, host: str | None = None) -> tuple[int, dict[str, str], bytes]:
        import http.client
        import json

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-MemorySafe-Setup-Token"] = token
        if host is not None:
            headers["Host"] = host
        connection.request("POST", path, body=json.dumps(body), headers=headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def test_the_bundle_downloads_as_a_zip(self) -> None:
        import json

        status, _headers, data = self._post("/api/support-bundle", {}, self.token)
        self.assertEqual(status, 200)
        name = json.loads(data)["file_name"]
        self.assertRegex(name, r"^MemorySafe-Support-\d{8}-\d{6}\.zip$")
        status, headers, zipped = self._post("/api/support-bundle/download", {"name": name}, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn(name, headers["Content-Disposition"])
        self.assertTrue(zipped.startswith(b"PK"))

    def test_the_download_needs_this_pages_token(self) -> None:
        status, _headers, _data = self._post("/api/support-bundle/download", {"name": "MemorySafe-Support-20260919-170608.zip"}, None)
        self.assertEqual(status, 403)

    def test_the_download_is_answered_only_on_this_computer(self) -> None:
        """The same Host check as /api/forget: a site that rebinds its own name to 127.0.0.1
        still sends that name, so it is refused even if it somehow had the token."""
        self.bundles.mkdir(parents=True)
        (self.bundles / "MemorySafe-Support-20260919-170608.zip").write_bytes(b"PK")
        status, _headers, _data = self._post(
            "/api/support-bundle/download", {"name": "MemorySafe-Support-20260919-170608.zip"}, self.token,
            host="attacker.example:8765",
        )
        self.assertEqual(status, 403)

    def test_nothing_but_a_support_bundle_can_be_fetched(self) -> None:
        state = self.paths.install_root / "runtime-state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "device.json").write_text("{}", encoding="utf-8")
        # Named like a bundle, but not in the support-bundles folder.
        (state / "MemorySafe-Support-20260919-170608.zip").write_bytes(b"PK")
        for name in (
            "../device.json", "device.json", "..\\device.json", "MemorySafe-Support-x.zip", "",
            "MemorySafe-Support-20260919-170608.zip", "../MemorySafe-Support-20260919-170608.zip",
        ):
            status, _headers, _data = self._post("/api/support-bundle/download", {"name": name}, self.token)
            self.assertEqual(status, 404, name)

    def test_only_the_exact_names_create_support_bundle_writes_are_served(self) -> None:
        """The first version matched with `$` and `\\d`, which let a trailing newline and other
        scripts' digits through, and a served name goes into Content-Disposition. These decoys
        sit in the bundle folder itself, so only the name check stands in their way."""
        self.bundles.mkdir(parents=True)
        names = ["MemorySafe-Support-\u0662\u0660\u0662\u0666\u0660\u0669\u0661\u0669-170608.zip",
                 "MemorySafe-Support-\uff12\uff10\uff12\uff16\uff10\uff19\uff11\uff19-170608.zip"]
        if os.name != "nt":  # Windows refuses control characters in file names.
            names.append("MemorySafe-Support-20260919-170608.zip\n")
        for name in names:
            (self.bundles / name).write_bytes(b"PK")
            status, _headers, _data = self._post("/api/support-bundle/download", {"name": name}, self.token)
            self.assertEqual(status, 404, repr(name))

    def _zip_names_and_text(self, name: str) -> dict[str, str]:
        import io
        import zipfile

        _status, _headers, zipped = self._post("/api/support-bundle/download", {"name": name}, self.token)
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            return {entry: archive.read(entry).decode("utf-8") for entry in archive.namelist()}

    def test_a_report_is_an_email_to_support_with_the_description(self) -> None:
        """A ZIP with no destination and no place to say what happened reported nothing.

        The tester asked "what is the outcome if the person can't actually report
        anything?" -- and chose email to contact@memorysafe.ca. The person's own words go
        into the bundle and the draft; the draft names the file to attach; nothing is sent.
        """
        import json
        from urllib.parse import parse_qs, urlsplit

        said = "Claude said MemorySafe wasn't available when I asked it to remember my deadline."
        status, _headers, data = self._post("/api/support-bundle", {"description": said}, self.token)
        self.assertEqual(status, 200)
        payload = json.loads(data)
        email = payload["email"]
        self.assertEqual(email["to"], "contact@memorysafe.ca")
        self.assertTrue(email["mailto"].startswith("mailto:contact@memorysafe.ca?"))
        # quote, not quote_plus: a "+" would show up in the mail client instead of a space.
        self.assertNotIn("+", email["mailto"])
        fields = parse_qs(urlsplit(email["mailto"]).query)
        self.assertIn("MemorySafe problem report", fields["subject"][0])
        self.assertIn(said, fields["body"][0])
        self.assertIn(payload["file_name"], fields["body"][0])
        files = self._zip_names_and_text(payload["file_name"])
        self.assertEqual(files["what-went-wrong.txt"].strip(), said)
        self.assertIn("what-went-wrong.txt", files["README.txt"])

    def test_what_the_draft_promises_matches_what_the_report_holds(self) -> None:
        """The description goes into the bundle as what-went-wrong.txt, so "status, counts
        and sanitized error signatures only" stopped being true of it the moment the form
        was added. The page and the draft say what it holds, and a claim changes with the
        code that makes it true."""
        import json
        from urllib.parse import parse_qs, urlsplit

        _status, _headers, data = self._post(
            "/api/support-bundle", {"description": "the dashboard never opened"}, self.token
        )
        payload = json.loads(data)
        body = parse_qs(urlsplit(payload["email"]["mailto"]).query)["body"][0]
        self.assertIn("what you typed", body)
        self.assertNotIn("sanitized error signatures only", body)
        self.assertNotIn("sanitized error signatures only", dashboard_html(local_api_token="t"))

    def test_no_description_means_no_description_file(self) -> None:
        import json

        _status, _headers, data = self._post("/api/support-bundle", {}, self.token)
        payload = json.loads(data)
        self.assertNotIn("what-went-wrong.txt", self._zip_names_and_text(payload["file_name"]))
        self.assertIn("not%20described", payload["email"]["mailto"])

    def test_a_pasted_wall_of_text_is_capped(self) -> None:
        import json

        # _read_json already refuses a body over 16 KB, so test the cap under that.
        _status, _headers, data = self._post("/api/support-bundle", {"description": "x" * 10_000}, self.token)
        payload = json.loads(data)
        self.assertEqual(len(self._zip_names_and_text(payload["file_name"])["what-went-wrong.txt"].strip()), 4000)
        status, _headers, _data = self._post("/api/support-bundle", {"description": "x" * 50_000}, self.token)
        self.assertEqual(status, 400)

    def test_the_report_form_ships_hidden_and_writes_text_never_html(self) -> None:
        page = dashboard_html(local_api_token="t")
        self.assertIn('id="report-form" hidden', page)
        start = page.index('byId("support-bundle").addEventListener')
        end = page.index("if (standalone) {", start)
        self.assertNotIn("innerHTML", page[start:end])
        self.assertIn('email.mailto.startsWith("mailto:")', page[start:end])

    def test_the_result_sits_under_the_button_not_at_the_foot_of_the_page(self) -> None:
        page = dashboard_html(local_api_token="t")
        self.assertLess(page.index('id="support-result"'), page.index('class="hero"'))


if __name__ == "__main__":
    unittest.main()
