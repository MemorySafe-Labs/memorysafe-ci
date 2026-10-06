"""The report has to survive its own contents.

Found by running `memorysafe uninstall` on Windows on 20 September: it died with
UnicodeEncodeError on the arrow it prints for every item it would remove, and
`memorysafe doctor` did the same on the tick it prints for every passing check --
the command plugin/INSTALL.md tells people to run when something looks wrong.

Python talks to a real Windows console in UTF-16 and is fine there. Redirect the
output and it falls back to the locale encoding, cp1252 on an English or French
Windows, and the first character outside that set raises. Redirected output is not
exotic: `memorysafe doctor > log.txt` does it, and so does an assistant running the
command and reading what comes back, which the skill tells it to do.

`find` prints memory content, so this is not only about decoration: one accented
character in a memory would have killed the command the same way.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], database: Path) -> subprocess.CompletedProcess:
    """Run the CLI in a child whose stdout is a pipe with a legacy code page.

    PYTHONIOENCODING is how a child process is told what its streams are, and cp1252
    is what a redirected stream falls back to on the Windows machines this ships to.
    A child is the only honest way to test this: an in-process StringIO has no
    encoding to get wrong.
    """

    # Inherit the real environment, then force the one thing under test. Building an
    # env from scratch left out USERPROFILE, so every command died on Path.home()
    # instead -- and a test that only looks for UnicodeEncodeError passes happily
    # when the command never got far enough to print anything.
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "cp1252"
    environment["MEMORYSAFE_DB_PATH"] = str(database)
    return subprocess.run(
        [sys.executable, "-m", "memorysafe_chatgpt.cli", *command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=120,
    )


class LegacyCodePageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "memorysafe.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_survived(self, result: subprocess.CompletedProcess, what: str) -> None:
        error = result.stderr.decode("utf-8", "replace")
        self.assertNotIn("UnicodeEncodeError", error, f"{what} died on its own output:\n{error}")
        # Without this the test passes when the command fell over for any other
        # reason and never reached the character that used to kill it.
        self.assertEqual(result.returncode, 0, f"{what} exited {result.returncode}" + error)
        self.assertTrue(result.stdout.strip(), f"{what} printed nothing, so nothing was exercised")

    def test_the_doctor_survives_a_legacy_code_page(self) -> None:
        """It prints a tick for every passing check. That was enough to kill it."""
        self._assert_survived(_run(["doctor"], self.database), "doctor")

    def test_the_uninstaller_survives_a_legacy_code_page(self) -> None:
        """It prints an arrow for every item it would remove. A dry run changes nothing.

        The install root is a fixture rather than this computer's: on a machine with no
        MemorySafe the command prints that there is nothing to remove, and no arrow, so
        the test would pass without exercising anything. CI is exactly such a machine.
        """
        root = Path(self.temporary.name) / "MemorySafe"
        runtime = root / "runtime" / "abc123"
        runtime.mkdir(parents=True)
        (runtime / "ready").write_text("ready", encoding="utf-8")
        result = _run(["--install-root", str(root), "uninstall"], self.database)
        self._assert_survived(result, "uninstall")
        self.assertIn("would remove", result.stdout.decode("utf-8", "replace"))

    def test_memory_content_outside_the_code_page_survives(self) -> None:
        """A memory is whatever the person typed. `find` has to be able to print it."""
        remembered = _run(
            ["remember", "Dana's café rebuild ships in Montréal — naïve estimate 日本語", "--category", "project"],
            self.database,
        )
        self._assert_survived(remembered, "remember")
        found = _run(["find", "café"], self.database)
        self._assert_survived(found, "find")
        self.assertEqual(found.returncode, 0, found.stderr.decode("utf-8", "replace"))

    def test_json_output_is_still_machine_readable(self) -> None:
        """--json is what an assistant parses; replacement characters in it would be worse
        than a crash, because nothing would report them."""
        import json

        result = _run(["doctor", "--json"], self.database)
        self._assert_survived(result, "doctor --json")
        payload = json.loads(result.stdout.decode("utf-8"))
        self.assertIn("overall_status", payload)


if __name__ == "__main__":
    unittest.main()
