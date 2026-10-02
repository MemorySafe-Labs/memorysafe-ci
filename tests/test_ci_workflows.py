"""This repository is private and on GitHub's free plan: 2,000 Actions minutes a month,
a Windows minute counting twice and a macOS minute ten times. On 2026-09-23 no job could
start, and from 2026-09-24 to 2026-09-28 none started for a billing error. Hosted runners
are free on a public repository, so the checks run on MemorySafe-Labs/memorysafe-ci, on
candidates scripts/push_candidate.py pushes there. These keep each workflow on the side
it belongs to: a check that starts here is billed, and a secret that reaches the public
side is published."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# Workflows candidate.yml calls with a list of runners.
CALLED = ("ci.yml", "first-run.yml")

PRIVATE_ONLY = "github.repository == 'MemorySafe-Labs/memorysafe'"
PUBLIC_ONLY = "github.repository == 'MemorySafe-Labs/memorysafe-ci'"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _report_job() -> str:
    """candidate.yml's last job, from its key to the end of the file."""
    text = _read("candidate.yml")
    return text[text.index("\n  report:\n"):]


class PublicCiTests(unittest.TestCase):
    def test_the_checks_can_only_be_called(self) -> None:
        """Any trigger of their own would run them here, on metered runners, as well as
        on the public repository."""
        for name in CALLED:
            text = _read(name)
            self.assertIn("\non:\n  workflow_call:\n    inputs:\n      os:\n", text, name)
            for trigger in ("pull_request", "  push:", "schedule", "workflow_dispatch"):
                self.assertNotIn(trigger, text, f"{name} has {trigger}")
            self.assertIn("os: ${{ fromJSON(inputs.os || '[\"ubuntu-latest\"]') }}", text, name)

    def test_only_the_candidate_workflow_names_windows_and_macos(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if path.name == "candidate.yml":
                continue
            text = path.read_text(encoding="utf-8")
            for runner in ("windows-latest", "macos-latest"):
                self.assertNotIn(runner, text, f"{path.name} names {runner}")

    def test_a_candidate_runs_every_check_on_every_platform(self) -> None:
        text = _read("candidate.yml")
        every = "os: '[\"ubuntu-latest\", \"windows-latest\", \"macos-latest\"]'\n"
        self.assertIn(f"uses: ./.github/workflows/ci.yml\n    with:\n      {every}", text)
        self.assertIn(f"uses: ./.github/workflows/first-run.yml\n    with:\n      {every}", text)

    def test_a_candidate_run_starts_only_from_a_candidate_branch(self) -> None:
        """Nobody pushes candidate/* to the private repository, so this never runs there."""
        text = _read("candidate.yml")
        self.assertIn('\non:\n  push:\n    branches: ["candidate/**", "release-candidate/**"]\n', text)
        for trigger in ("pull_request", "schedule", "workflow_dispatch", "tags:"):
            self.assertNotIn(trigger, text)
        self.assertEqual(text.count(PUBLIC_ONLY), 3)

    def test_a_newer_candidate_cancels_the_older_run(self) -> None:
        text = _read("candidate.yml")
        self.assertIn("  group: candidate-${{ github.ref }}\n  cancel-in-progress: true\n", text)

    def test_the_status_key_reaches_one_job_that_runs_no_candidate_code(self) -> None:
        """The status App's key can write a commit status on the private repository.
        Candidate code runs in the test jobs, so the key must never be in their
        environment, and the job that holds it must not check the candidate out."""
        key = "secrets.STATUS_APP_PRIVATE_KEY"
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if path.name != "candidate.yml":
                self.assertNotIn("secrets.STATUS_", path.read_text(encoding="utf-8"), path.name)
        text = _read("candidate.yml")
        report = _report_job()
        self.assertEqual(text.count(key), 2)
        self.assertEqual(report.count(key), 2)
        self.assertIn("    environment: report\n", report)
        self.assertNotIn("actions/checkout", report)

    def test_the_one_action_that_sees_the_key_is_pinned_to_a_commit(self) -> None:
        """A tag can be moved to different code. This action is handed the App's
        private key, so it is named by the commit it was read at."""
        uses = re.findall(r"^\s*uses: (\S+)$", _report_job(), re.MULTILINE)
        self.assertEqual(len(uses), 1)
        self.assertRegex(uses[0], r"^actions/create-github-app-token@[0-9a-f]{40}$")

    def test_the_report_is_an_app_not_a_persons_token(self) -> None:
        """A fine-grained personal token expires within a year and belongs to whoever
        made it. The App belongs to the organisation and mints an hour-long token for
        the one private repository each run. And a run without the key must pass, not
        fail: the checks still ran, and the private commit just stays pending."""
        report = _report_job()
        self.assertIn("          owner: MemorySafe-Labs\n          repositories: memorysafe\n", report)
        self.assertIn("          GH_TOKEN: ${{ steps.app.outputs.token }}\n", report)
        self.assertIn("        if: steps.key.outputs.present == 'true'\n", report)
        self.assertNotIn("STATUS_TOKEN", _read("candidate.yml"))

    def test_the_report_runs_whatever_happened(self) -> None:
        """Without always(), a failed check skips the report and the pull request waits
        on a pending status forever."""
        report = _report_job()
        self.assertIn(f"    if: always() && {PUBLIC_ONLY}\n", report)
        self.assertIn("    needs: [test, first-run]\n", report)
        # A branch candidate is candidate/<branch>, so no branch name, release/x
        # included, can produce the ref that authorises a release.
        self.assertIn("            release-candidate/*) context=public-ci/release ;;\n", report)

    def test_the_report_reads_the_message_from_the_environment(self) -> None:
        """A commit message is text someone else wrote. Interpolated into the script it
        would be run as shell."""
        report = _report_job()
        self.assertIn("          MESSAGE: ${{ github.event.head_commit.message }}\n", report)
        script = report[report.rindex("        run: |"):]
        self.assertNotIn("${{", script)


class PublishCandidateTests(unittest.TestCase):
    def test_every_pull_request_push_main_push_and_tag_publishes_a_candidate(self) -> None:
        """Nothing is filtered by path: a documentation-only pull request would otherwise wait on a
        status that never arrives."""
        text = _read("publish-candidate.yml")
        self.assertIn('\non:\n  pull_request:\n  push:\n    branches: [main]\n    tags: ["v*"]\n', text)
        self.assertNotIn("paths", text)

    def test_it_runs_only_here_and_only_on_linux(self) -> None:
        text = _read("publish-candidate.yml")
        self.assertIn(PRIVATE_ONLY, text)
        self.assertEqual(text.count("runs-on:"), 1)
        self.assertIn("    runs-on: ubuntu-latest\n", text)
        # A Dependabot pull request's head repository is this one, so the job would start,
        # find no secret (Dependabot's runs get none) and fail red on every update.
        self.assertIn("github.actor != 'dependabot[bot]'", text)

    def test_it_publishes_the_pull_request_head_not_the_merge_commit(self) -> None:
        """actions/checkout checks out refs/pull/N/merge by default. A status set on
        that commit shows on no pull request."""
        text = _read("publish-candidate.yml")
        self.assertIn("          ref: ${{ github.event.pull_request.head.sha || github.sha }}\n", text)

    def test_it_does_not_wait_for_the_public_run(self) -> None:
        """Waiting would bill this repository for every minute the public run takes."""
        text = _read("publish-candidate.yml")
        self.assertIn("scripts/push_candidate.py --no-wait", text)
        self.assertIn("    timeout-minutes: 5\n", text)


if __name__ == "__main__":
    unittest.main()
