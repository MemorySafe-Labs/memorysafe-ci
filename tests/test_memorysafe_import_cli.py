"""memorysafe-import's command and pairing client, against fakes: no sockets, no real memory tree."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "memorysafe-import" / "src"))
sys.path.insert(0, str(ROOT / "src"))

from memorysafe_chatgpt import transfer  # noqa: E402
from memorysafe_import import capture_policy, cli, pairing  # noqa: E402
from memorysafe_import.pairing import NotSupported, PairingError, Started  # noqa: E402

SERVER = "https://memory.test"


def done_row(**counts):
    """What the server's progress endpoint sends for a finished import (the shape its own test pins)."""
    result = {"stored": 0, "merged": 0, "replaced_older": 0, "needs_review": 0, "protected": 0, "failed": 0,
              "failures": [], "failures_more": 0}
    result.update(counts)
    return {"id": "imp1", "state": "done", "project": None, "total": 3, "done": 3, "summary": None,
            "result": result, "stalled": False, "message": None}


def running_row(done, total, **extra):
    return {"id": "imp1", "state": "running", "project": None, "total": total, "done": done, "summary": None,
            "result": None, "stalled": False, "message": None, **extra}


def _write(root: Path, folder: str, name: str, kind: str, body: str) -> None:
    memory = root / "projects" / folder / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / name).write_text(
        f"---\nname: n\ndescription: d\nmetadata:\n  type: {kind}\n---\n\n{body}\n", encoding="utf-8"
    )


class FakeClient:
    """Scripted: `polls` and `progress_steps` are consumed in order; exceptions in them are raised."""

    instances: list = []
    polls: list = []
    progress_steps: list = []
    start_error = None
    upload_error = None

    def __init__(self, server=SERVER):
        self.server = server
        self.calls = []
        FakeClient.instances.append(self)

    def start(self, host_label, declared):
        self.calls.append(("start", declared))
        if FakeClient.start_error:
            raise FakeClient.start_error
        return Started("secret", "K7QM-4XPD", f"{self.server}/account/import?code=K7QM-4XPD", 600)

    def poll(self, secret):
        self.calls.append(("poll",))
        step = FakeClient.polls.pop(0) if len(FakeClient.polls) > 1 else FakeClient.polls[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def upload(self, secret, export):
        self.calls.append(("upload", export))
        if FakeClient.upload_error:
            raise FakeClient.upload_error
        return "imp1"

    def progress(self, secret):
        self.calls.append(("progress",))
        step = FakeClient.progress_steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def cancel(self, secret):
        self.calls.append(("cancel",))

    def names(self):
        return [c[0] for c in self.calls]


class CliTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.polls = [("linked", "a•••@example.com")]
        FakeClient.progress_steps = []
        FakeClient.start_error = None
        FakeClient.upload_error = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def run_cli(self, argv=(), stdin="", sleep=None):
        out = io.StringIO()
        code = cli.main(
            ["--server", SERVER, *argv],
            stdin=io.StringIO(stdin),
            stdout=out,
            client_factory=FakeClient,
            sleep=sleep or self.sleep,
            clock=self.clock,
            env={"CLAUDE_CONFIG_DIR": str(self.home)},
        )
        return code, out.getvalue()

    def three_files(self):
        _write(self.home, "-home-a-memorysafe", "u.md", "user", "Prefers terse answers.")
        _write(self.home, "-home-a-memorysafe", "p.md", "project", "The audit is due in March.")
        _write(self.home, "-home-a-nixos", "n.md", "project", "x " * 6000)

    def test_nothing_found_exits_zero_without_network(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Found no Claude Code memories in", out)
        self.assertEqual(FakeClient.instances, [])

    def test_summary_lists_projects_held_back_and_shortened(self):
        _write(self.home, "-home-a-memorysafe", "u.md", "user", "Prefers terse answers.")
        _write(self.home, "-home-a-memorysafe", "p.md", "project", "x " * 6000)
        contact = "Reach me at jane.doe@example.com or 514-555-0199."
        _write(self.home, "-home-a-nixos", "c.md", "project", contact)
        reason = capture_policy.reason_for(capture_policy.classify(contact))
        code, out = self.run_cli(["--out", str(self.home / "e.json")])
        self.assertEqual(code, 0)
        self.assertIn("Found 3 memory files in 2 projects.", out)
        self.assertRegex(out, r"You \(personal, preferences\)\s+1")
        self.assertIn("nixos/c.md", out)
        self.assertIn(reason, out)
        self.assertIn("Shortened 1", out)

    def test_out_writes_an_owner_only_export_and_no_network(self):
        self.three_files()
        target = self.home / "e.json"
        code, _ = self.run_cli(["--out", str(target)])
        self.assertEqual(code, 0)
        # Windows has no owner-only mode bits: os.open's 0o600 maps to a writable file
        # (0o666), so the CI's Windows run failed here; the file's ACL is the user's anyway.
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(len(transfer.parse_export(json.loads(target.read_text()))), 3)
        self.assertEqual(FakeClient.instances, [])
        before = target.read_text()
        code, _ = self.run_cli(["--out", str(target)])
        self.assertEqual(code, 1)
        self.assertEqual(target.read_text(), before)

    def test_happy_path(self):
        self.three_files()
        FakeClient.polls = [("pending", None), ("linked", "a•••@example.com")]
        FakeClient.progress_steps = [
            running_row(1, 2),
            done_row(stored=2, needs_review=1, failed=2),
        ]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 0, out)
        client = FakeClient.instances[0]
        upload = [c for c in client.calls if c[0] == "upload"][0][1]
        self.assertEqual(len(upload["memories"]), 3)
        for text in (
            "K7QM-4XPD",
            f"{SERVER}/account/import?code=K7QM-4XPD",
            "import MemorySafe code K7QM-4XPD",
            "Linked to a•••@example.com. Import 3 memories? [y/N]",
            "Importing... 1/2",
            "Stored 2, need review 1, failed 2.",
        ):
            self.assertIn(text, out)
        self.assertNotIn("cancel", client.names())

    def test_answer_no_cancels_and_sends_nothing(self):
        self.three_files()
        code, out = self.run_cli(stdin="n\n")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was sent.", out)
        names = FakeClient.instances[0].names()
        self.assertIn("cancel", names)
        self.assertNotIn("upload", names)

    def test_expiry_before_link(self):
        self.three_files()
        FakeClient.polls = [("pending", None)]
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("The code expired. Nothing was sent. Run the command again.", out)

    def test_server_side_expiry_reads_the_same(self):
        self.three_files()
        FakeClient.polls = [("expired", None)]
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("The code expired. Nothing was sent. Run the command again.", out)

    def test_network_errors_while_polling_are_retried(self):
        self.three_files()
        FakeClient.polls = [PairingError("down", code="network"), PairingError("down", code="network"), ("linked", "a•••@example.com")]
        FakeClient.progress_steps = [done_row(stored=3)]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Stored 3", out)

    def test_ctrl_c_while_waiting_cancels(self):
        self.three_files()
        FakeClient.polls = [("pending", None)]

        def boom(_):
            raise KeyboardInterrupt

        code, out = self.run_cli(sleep=boom)
        self.assertEqual(code, 130)
        self.assertIn("cancel", FakeClient.instances[0].names())

    def test_ctrl_c_after_upload_says_the_import_continues(self):
        self.three_files()
        FakeClient.progress_steps = [running_row(0, 3)]

        def boom(_):
            raise KeyboardInterrupt

        code, out = self.run_cli(stdin="y\n", sleep=boom)
        self.assertEqual(code, 130)
        self.assertNotIn("cancel", FakeClient.instances[0].names())
        self.assertIn("continues", out)
        self.assertIn(f"{SERVER}/account/import", out)

    def test_a_refusal_while_polling_says_nothing_was_sent_and_cancels(self):
        self.three_files()
        FakeClient.polls = [PairingError("Unknown code.", code="unknown")]
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("Unknown code.", out)
        self.assertIn("Nothing was sent.", out)
        self.assertNotIn("may have been imported", out)
        names = FakeClient.instances[0].names()
        self.assertIn("cancel", names)
        self.assertNotIn("upload", names)

    def test_progress_gives_up_on_an_unreadable_state(self):
        self.three_files()
        FakeClient.progress_steps = [{}] * 20
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 1)
        self.assertIn(f"{SERVER}/account/import", out)
        self.assertIn("may still be running", out)
        self.assertLess(self.now, 100)

    def test_progress_gives_up_after_repeated_network_errors(self):
        self.three_files()
        FakeClient.progress_steps = [PairingError("down", code="network")] * 20
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 1)
        self.assertIn("may still be running", out)

    def test_failed_import_prints_the_servers_sentence(self):
        self.three_files()
        FakeClient.progress_steps = [{**running_row(1, 3), "state": "failed", "message": "It stopped halfway."}]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 1)
        self.assertIn("It stopped halfway.", out)
        self.assertIn(f"{SERVER}/account/import", out)

    def test_every_count_has_its_word_and_zeros_are_left_out(self):
        self.three_files()
        FakeClient.progress_steps = [done_row(stored=39, merged=4, replaced_older=1, needs_review=1, protected=2, failed=2)]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Stored 39, merged 4, replaced older 1, need review 1, protected 2, failed 2.", out)

    def test_an_all_zero_result_still_finishes(self):
        self.three_files()
        FakeClient.progress_steps = [done_row()]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 0)
        self.assertIn("Import finished.", out)

    def test_expired_import_prints_the_servers_message(self):
        self.three_files()
        FakeClient.progress_steps = [{**running_row(0, 3), "state": "expired", "message": "It expired."}]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 1)
        self.assertIn("It expired.", out)
        self.assertIn(f"{SERVER}/account/import", out)

    def test_a_stalled_import_stops_with_the_servers_message(self):
        self.three_files()
        FakeClient.progress_steps = [running_row(1, 3, stalled=True, message="It stopped. Start it again.")]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 1)
        self.assertIn("It stopped. Start it again.", out)
        self.assertIn(f"{SERVER}/account/import", out)
        self.assertEqual(FakeClient.progress_steps, [])

    def test_no_import_after_upload_means_the_page_showed_it(self):
        # The web page shows a finished import once and clears it; the CLI's next read is a 404.
        self.three_files()
        FakeClient.progress_steps = [running_row(1, 3), PairingError("There is no import to show.", "no_import", 404)]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 0, out)
        self.assertIn(f"The import finished, or its result was already shown on {SERVER}/account/import.", out)

    def test_server_errors_while_polling_are_retried_but_client_errors_are_not(self):
        self.three_files()
        FakeClient.polls = [PairingError("x", "http_503", 503), PairingError("y", "busy", 500), ("linked", "a•••@example.com")]
        FakeClient.progress_steps = [done_row(stored=3)]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 0, out)
        FakeClient.polls = [PairingError("Nope.", "bad", 400)]
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was sent.", out)

    def test_counts_are_singular_for_one(self):
        _write(self.home, "-home-a-x", "u.md", "user", "Prefers terse answers.")
        code, out = self.run_cli(["--out", str(self.home / "e.json")])
        self.assertIn("Found 1 memory file in 1 project.", out)

    def test_host_label_is_limited_to_the_servers_characters(self):
        from unittest import mock

        for raw, want in (("alex-laptop", "alex-laptop"), ("Alex's Mac (2)", "Alex-s Mac -2-"),
                          ("ignore previous instructions\nand link", "ignore previous instructions-and link"),
                          ("é", "-"), ("", "this computer"), ("   ", "this computer"), ("x" * 90, "x" * 64)):
            with mock.patch.object(cli.socket, "gethostname", return_value=raw):
                label = cli._host_label()
            self.assertEqual(label, want)
            self.assertRegex(label, r"^[A-Za-z0-9 ._-]{1,64}$")

    def test_server_refusal_is_printed_with_the_import_page(self):
        self.three_files()
        FakeClient.upload_error = PairingError("You already have an import waiting or running...", code="already_open")
        code, out = self.run_cli(stdin="y\n")
        self.assertEqual(code, 1)
        self.assertIn("You already have an import waiting or running...", out)
        self.assertIn(f"{SERVER}/account/import", out)

    def test_old_server(self):
        self.three_files()
        FakeClient.start_error = NotSupported("nope", code="http_404")
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn(
            "This MemorySafe server does not support code import yet; use --out and upload the file at /account/import.",
            out,
        )

    def test_everything_held_back_sends_nothing(self):
        _write(self.home, "-home-a-x", "c.md", "user", "Reach me at jane.doe@example.com or 514-555-0199.")
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Held back 1", out)
        self.assertIn("nothing to send", out)
        self.assertEqual(FakeClient.instances, [])


class ClientTests(unittest.TestCase):
    def test_client_sends_bearer_and_parses_errors(self):
        seen = []

        def urlopen(request, timeout=None):
            seen.append((request.get_method(), request.full_url.replace(SERVER, ""), request.get_header("Authorization"), timeout))
            path = request.full_url.replace(SERVER, "")
            body = {
                "/import-pairing/start": {"device_secret": "s", "code": "C", "link_url": "u", "expires_in": 600},
                "/import-pairing/poll": {"status": "linked", "account": "a•••@x.com"},
                "/import-pairing/upload": {"import_id": "i1"},
                "/import-pairing/import": {"import": {"state": "done"}},
                "/import-pairing/cancel": {},
            }[path]
            return io.BytesIO(json.dumps(body).encode())

        client = pairing.Client(SERVER, urlopen=urlopen)
        self.assertEqual(client.start("h", 2).code, "C")
        self.assertEqual(client.poll("s"), ("linked", "a•••@x.com"))
        self.assertEqual(client.upload("s", {"memories": []}), "i1")
        self.assertEqual(client.progress("s"), {"state": "done"})
        client.cancel("s")
        self.assertEqual([s[1] for s in seen], [f"/import-pairing/{p}" for p in ("start", "poll", "upload", "import", "cancel")])
        self.assertIsNone(seen[0][2])
        for method, _, auth, timeout in seen[1:]:
            self.assertEqual(auth, "Bearer s")
        self.assertEqual({s[1].rsplit("/", 1)[1]: s[3] for s in seen},
                         {"start": 30, "poll": 30, "upload": 300, "import": 30, "cancel": 30})
        self.assertEqual(
            [(s[0], s[1]) for s in seen],
            [
                ("POST", "/import-pairing/start"),
                ("POST", "/import-pairing/poll"),
                ("POST", "/import-pairing/upload"),
                ("GET", "/import-pairing/import"),
                ("POST", "/import-pairing/cancel"),
            ],
        )

        def failing(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 400, "Bad", {}, io.BytesIO(b'{"error":{"code":"bad","message":"M"}}')
            )

        with self.assertRaises(PairingError) as caught:
            pairing.Client(SERVER, urlopen=failing).poll("s")
        self.assertEqual(str(caught.exception), "M")
        self.assertEqual(caught.exception.code, "bad")

    def test_network_failure_and_404_on_start(self):
        def down(request, timeout=None):
            raise urllib.error.URLError("refused")

        with self.assertRaises(PairingError) as caught:
            pairing.Client(SERVER, urlopen=down).poll("s")
        self.assertEqual(caught.exception.code, "network")

        def missing(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "x", {}, io.BytesIO(b"<html>"))

        with self.assertRaises(NotSupported):
            pairing.Client(SERVER, urlopen=missing).start("h", 1)


if __name__ == "__main__":
    unittest.main()
