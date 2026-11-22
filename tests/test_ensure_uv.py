"""plugin/scripts/ensure_uv: the one place MemorySafe downloads an executable.

A binary that runs on first start with the user's permissions must be exactly the one
that was pinned. These tests serve fake releases over file:// so they never touch the
network, and prove that a mismatch leaves nothing behind that could be run.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugin" / "scripts" / "ensure_uv"
UV_VERSION = "0.12.15"
_TARGETS = {
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("Linux", "aarch64"): "aarch64-unknown-linux-musl",
}


class EnsureUvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = _TARGETS.get((platform.system(), platform.machine()))
        if self.target is None:
            self.skipTest(f"no uv build for {platform.system()} {platform.machine()}")
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.scripts = base / "plugin" / "scripts"
        self.scripts.mkdir(parents=True)
        shutil.copy2(SCRIPT, self.scripts / "ensure_uv")
        (self.scripts / "runtime.env").write_text(
            f"RUNTIME_KEY=testkey\nPYTHON_VERSION=3.12\nUV_VERSION={UV_VERSION}\n"
        )
        self.releases = base / "releases"
        self.archive = self.releases / UV_VERSION / f"uv-{self.target}.tar.gz"
        self.archive.parent.mkdir(parents=True)
        payload = b"#!/bin/sh\necho fake-uv\n"
        with tarfile.open(self.archive, "w:gz") as bundle:
            member = tarfile.TarInfo(f"uv-{self.target}/uv")
            member.size = len(payload)
            member.mode = 0o755
            bundle.addfile(member, io.BytesIO(payload))
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.data = base / "data"
        self.installed = self.data / "tools" / f"uv-{UV_VERSION}" / "uv"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _checksums(self, digest: str) -> None:
        (self.scripts / "uv-checksums").write_text(f"{digest}  uv-{self.target}.tar.gz\n")

    def _run(self, **extra: str) -> subprocess.CompletedProcess:
        environment = {key: value for key, value in os.environ.items() if key != "MEMORYSAFE_UV"}
        environment["MEMORYSAFE_INSTALL_ROOT"] = str(self.data)
        environment["MEMORYSAFE_UV_BASE_URL"] = self.releases.as_uri()
        environment.update(extra)
        return subprocess.run(
            ["/bin/sh", str(self.scripts / "ensure_uv")],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _leftovers(self) -> list[str]:
        tools = self.data / "tools"
        return sorted(path.name for path in tools.glob(".uv-download-*")) if tools.is_dir() else []

    def test_installs_a_verified_uv_once(self) -> None:
        self._checksums(self.digest)
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, f"{self.installed}\n")
        self.assertTrue(os.access(self.installed, os.X_OK))

        self.archive.unlink()
        second = self._run()
        self.assertEqual((second.returncode, second.stdout), (0, f"{self.installed}\n"))
        self.assertEqual(self._leftovers(), [])

    def test_a_tampered_download_is_deleted_and_never_installed(self) -> None:
        self._checksums("0" * 64)
        result = self._run()
        self.assertEqual(result.returncode, 3)
        self.assertIn("did not match", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.installed.parent.exists())
        self.assertEqual(self._leftovers(), [])

    def test_a_hashing_tool_failure_is_reported_distinctly_from_a_mismatch(self) -> None:
        """set -eu has no pipefail (not POSIX): "sha256sum ... | awk ..." used to hide a
        failing sha256sum/shasum, because awk still exits 0 on the empty input a failed
        hasher leaves it, so ACTUAL stayed "" and compared unequal to EXPECTED --
        reported as "did not match its recorded checksum", exactly the confusion
        ensure_uv.cmd's checksum verification was rebuilt to eliminate on Windows. Fails
        safe either way (nothing unverified runs), but this is a diagnostics defect, not
        a security hole, and it must report itself as one.
        """
        self._checksums(self.digest)
        fake_bin = Path(self.temporary.name) / "fake-bin"
        fake_bin.mkdir()
        for name in ("sha256sum", "shasum"):
            broken = fake_bin / name
            broken.write_text("#!/bin/sh\nexit 9\n")
            broken.chmod(0o755)
        result = self._run(PATH=f"{fake_bin}:{os.environ.get('PATH', '')}")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("did not match", result.stderr)
        self.assertIn("Could not compute a checksum", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.installed.parent.exists())
        self.assertEqual(self._leftovers(), [])

    def test_an_unreachable_release_is_a_download_failure(self) -> None:
        self._checksums(self.digest)
        self.archive.unlink()
        result = self._run()
        self.assertEqual((result.returncode, result.stdout), (2, ""))

    def test_a_target_without_a_recorded_checksum_is_never_downloaded(self) -> None:
        (self.scripts / "uv-checksums").write_text("")
        result = self._run()
        self.assertEqual(result.returncode, 3)
        self.assertFalse((self.data / "tools").exists())

    def test_an_explicit_uv_wins_and_a_missing_one_is_refused(self) -> None:
        self._checksums(self.digest)
        own = Path(self.temporary.name) / "my-uv"
        own.write_text("#!/bin/sh\n")
        own.chmod(0o755)
        chosen = self._run(MEMORYSAFE_UV=str(own))
        self.assertEqual((chosen.returncode, chosen.stdout), (0, f"{own}\n"))
        self.assertFalse((self.data / "tools").exists())
        self.assertEqual(self._run(MEMORYSAFE_UV=f"{own}-missing").returncode, 2)


class WindowsEnsureUvTextTests(unittest.TestCase):
    """ensure_uv.cmd cannot be executed on the dev machine or in Linux CI.

    Its behaviour is proven by the windows-latest first-run job. These tests guard
    the properties that silently break it: a bare tool name that resolves to Git's
    tar (which cannot read a .zip), a variable the MCP SDK does not pass through to
    the child, and any drift from ensure_uv's exit codes.
    """

    def setUp(self) -> None:
        self.text = (ROOT / "plugin" / "scripts" / "ensure_uv.cmd").read_text(encoding="utf-8")

    def test_uses_absolute_system32_tools(self) -> None:
        for tool in ("curl.exe", "tar.exe", "certutil.exe"):
            self.assertIn("%SystemRoot%\\System32\\" + tool, self.text)

    def test_never_reads_a_variable_the_host_strips(self) -> None:
        for name in ("COMSPEC", "PATHEXT", "TMP", "ProgramFiles(x86)", "windir"):
            self.assertNotIn("%" + name + "%", self.text)

    def test_selects_the_target_from_processor_architecture(self) -> None:
        self.assertIn("PROCESSOR_ARCHITECTURE", self.text)
        self.assertIn("aarch64-pc-windows-msvc", self.text)
        self.assertIn("x86_64-pc-windows-msvc", self.text)

    def test_keeps_the_posix_exit_codes(self) -> None:
        for code in ("exit /b 2", "exit /b 3", "exit /b 4"):
            self.assertIn(code, self.text)

    def test_does_not_strip_a_leading_directory(self) -> None:
        # The Windows zip is flat: uv.exe sits at the archive root. Stripping a
        # component, as the POSIX tarball needs, would leave nothing behind.
        self.assertNotIn("--strip-components", self.text)

    def test_certutil_digest_is_read_from_a_file_not_captured(self) -> None:
        """Incident, rounds 1 and 2, on a real windows-latest first-run job.

        Round 1: `for /f "skip=1 tokens=*" %%H in ('"%CERTUTIL%" ...') do` — a
        single-quoted (not usebackq) command string starting with a literal double
        quote. cmd.exe's for /f parser produced zero output; the loop never ran,
        ACTUAL stayed empty, and empty-vs-real-checksum was reported as "checksum
        mismatch".

        Round 2 traded that trap for another: `for /f "usebackq skip=1 tokens=*"
        %%H in (`"%CERTUTIL%" ...`) do` runs the command through cmd /c, and cmd /c
        strips the outer quotes of a command line that begins with a literal double
        quote. On a real runner this printed "The filename, directory name, or
        volume label syntax is incorrect" and left ACTUAL holding parser leftovers
        (observed as a bare "=") — again reported as a mismatch, because nothing
        validated ACTUAL's shape before comparing it.

        The fix: never capture certutil's output as a command. Redirect its stdout
        to a file inside %WORK% — do not "simplify" this back to a captured
        one-liner.
        """
        self.assertIn(
            '"%CERTUTIL%" -hashfile "%WORK%\\!ARCHIVE_NAME!" SHA256 >"%WORK%\\hash-certutil.txt"',
            self.text,
        )
        # Neither previous command-capture trap survives.
        self.assertNotIn("in ('\"%CERTUTIL%\"", self.text)
        self.assertNotIn("in (`\"%CERTUTIL%\"", self.text)

    def test_the_digest_is_verified_with_findstr_not_parsed(self) -> None:
        """Incident, rounds 3 and 4: reading certutil's output from a file (no

        command-capture) still did not produce a usable digest on a real
        windows-latest runner. Round 3 added a raw-output dump on failure; that dump
        then showed, in round 4, certutil exiting 0 with the byte-for-byte correct
        digest sitting right there in the file, while this file's own extraction
        logic (a per-line `for /f`, delayed-expansion substring surgery, and a
        nested `for %%c in (0 1 2 ... F)` loop stripping hex characters) still
        decided no line was a valid digest — even though `:dump_raw` read the exact
        same file with the same `for /f` primitive and printed it correctly. That
        proved the defect was in this file's own string-parsing machinery, not in
        which bytes were where.

        The fix is to stop parsing the digest out of the file at all. certutil
        already printed it; the only question is whether it equals EXPECTED, and
        findstr answers that without extracting anything: `findstr /c:"<expected>"
        file` succeeds if and only if that exact string appears somewhere in the
        file. There is no for /f over the digest text, no delayed-expansion
        substring surgery, no nested for, and no assumption about which line or
        byte holds what.
        """
        self.assertIn("%SystemRoot%\\System32\\findstr.exe", self.text)
        self.assertIn('/i /c:"!EXPECTED!"', self.text)
        # The entire parsing/normalisation machinery from earlier rounds is gone
        # (":validate_digest" itself still appears, past tense, in the incident
        # comment recording what this file used to do and why that was replaced).
        self.assertNotIn("HEXCHECK", self.text)
        self.assertNotIn("DIGEST_OK", self.text)
        self.assertNotIn("!ACTUAL:", self.text)
        self.assertNotIn("!CANDIDATE:", self.text)

    def test_soundness_of_matching_the_digest_as_a_substring_is_documented(self) -> None:
        # Matching EXPECTED anywhere in a tool's raw output is only sound because
        # that output cannot contain attacker-chosen bytes: the hashed file's path
        # is built from this script's own %RANDOM%, not from the download's
        # contents, and neither tool ever echoes the hashed file's bytes back. A
        # future reader must be told this explicitly so they do not "tighten" this
        # back into a parser.
        self.assertIn("Soundness", self.text)
        self.assertIn("%RANDOM%", self.text)
        self.assertIn("attacker", self.text)

    def test_certutils_own_exit_code_is_checked_before_findstr_runs(self) -> None:
        # A certutil that failed to run must never be reported as a checksum
        # mismatch - findstr is only consulted once certutil's own exit code says
        # it actually produced output.
        certutil_at = self.text.index('"%CERTUTIL%" -hashfile')
        certutil_rc_check_at = self.text.index("!CERTUTIL_RC! neq 0")
        findstr_call_at = self.text.index("call :verify_with_findstr")
        self.assertLess(certutil_at, certutil_rc_check_at)
        self.assertLess(certutil_rc_check_at, findstr_call_at)

    def test_findstr_errors_fail_closed_and_are_never_a_mismatch(self) -> None:
        # findstr's own contract is 0 (found) / 1 (not found). Anything else means
        # findstr itself did not behave as expected, which is a "could not verify"
        # condition (exit 1), never a "does not match" condition (exit 3) - fail
        # closed rather than guess which one an unexpected exit code meant.
        self.assertIn("if errorlevel 2 goto :eof", self.text)
        self.assertIn('set "MATCH=error"', self.text)
        self.assertIn('set "MATCH=no"', self.text)
        self.assertIn('set "MATCH=yes"', self.text)

    def test_a_checksum_mismatch_reports_expected_and_dumps_raw_output(self) -> None:
        # The bare "did not match its recorded checksum" message gave no way to
        # tell, from a CI log or a support bundle, whether the checksum table was
        # wrong or the download was corrupt. There is no parsed "actual" value any
        # more (the whole point of the findstr fix is not extracting one), so the
        # raw tool output - which contains the real digest exactly as the tool
        # printed it - must reach stderr instead.
        mismatch_at = self.text.index(":checksum_mismatch")
        exit3_at = self.text.index("exit /b 3", mismatch_at)
        self.assertIn("expected: !EXPECTED!", self.text[mismatch_at:exit3_at])
        self.assertIn("call :dump_raw", self.text[mismatch_at:exit3_at])

    def test_a_digest_failure_dumps_the_raw_tool_output_before_cleanup(self) -> None:
        """Incident, round 3 (cycle 5): certutil still produced no valid digest, and

        nobody could tell why, because the raw hash file was deleted along with the
        rest of %WORK% before anyone could look at it. The fix is permanent
        diagnostic value, not scaffolding — the same blind spot would recur for any
        future hashing failure (a broken or replaced certutil, unusual security
        software, ...), so the raw output of whichever tool ran, and its exit code,
        must reach stderr before %WORK% is torn down.
        """
        self.assertIn(":dump_raw", self.text)
        self.assertIn("call :dump_raw", self.text)
        self.assertIn("CERTUTIL_RC", self.text)
        self.assertIn("POWERSHELL_RC", self.text)
        # Capped so a pathological or enormous file cannot flood the log.
        self.assertIn("DUMP_N", self.text)
        self.assertIn("truncated", self.text)

    def test_powershell_fallback_exists_and_runs_only_after_certutil(self) -> None:
        # certutil is tried first because it needs no execution-policy exemption;
        # PowerShell (Get-FileHash) is the fallback for a machine where certutil is
        # missing, blocked, or does not produce output findstr can confirm. The
        # PowerShell attempt must be both present and textually positioned after the
        # certutil attempt, and only reached when certutil did not already verify.
        certutil_at = self.text.index(
            '"%CERTUTIL%" -hashfile "%WORK%\\!ARCHIVE_NAME!" SHA256 >"%WORK%\\hash-certutil.txt"'
        )
        powershell_at = self.text.index('"%POWERSHELL%" -NoProfile')
        self.assertLess(
            certutil_at,
            powershell_at,
            "certutil must be attempted before the PowerShell fallback",
        )
        self.assertIn("%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", self.text)
        self.assertIn("-ExecutionPolicy Bypass", self.text)
        self.assertIn(":try_powershell", self.text)
        self.assertIn("goto :hash_verified", self.text)
        # Do not reorder these: the comment explaining why must survive.
        self.assertIn("Do not reorder these", self.text)

    def test_both_hashing_paths_use_the_same_findstr_verification(self) -> None:
        # Both the certutil attempt and the PowerShell fallback route their raw
        # output through the same :verify_with_findstr call, so neither can be
        # checked by different logic than the other.
        self.assertEqual(self.text.count("call :verify_with_findstr"), 2)

    def test_never_falls_through_to_an_unpacked_but_unverified_archive(self) -> None:
        # The one outcome that must be impossible: if neither certutil nor
        # PowerShell lets findstr confirm the checksum, tar must never run against
        # the downloaded archive.
        digest_failure_at = self.text.index(
            "Could not verify !ARCHIVE_NAME! against its recorded checksum with certutil or PowerShell"
        )
        exit_at = self.text.index("exit /b 1", digest_failure_at)
        tar_at = self.text.index('"%TAR%"')
        self.assertLess(exit_at, tar_at)
        # Likewise for a confirmed mismatch (exit 3).
        mismatch_at = self.text.index("The downloaded uv did not match its recorded checksum")
        mismatch_exit_at = self.text.index("exit /b 3", mismatch_at)
        self.assertLess(mismatch_exit_at, tar_at)


if __name__ == "__main__":
    unittest.main()
