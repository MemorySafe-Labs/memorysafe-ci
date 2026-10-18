"""Runs the consolidate acceptance script (tests/bench/gates_acceptance.py) as a test.

It uses a temporary store and starts no server, so no port is involved."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConsolidateAcceptanceTests(unittest.TestCase):
    def test_dry_run_apply_and_restore_acceptance_checks_pass(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run([sys.executable, str(ROOT / "tests" / "bench" / "gates_acceptance.py")], env=env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("40/40 required checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
