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

# Spelled by name below, not typed as a path: this file is published, and
# tests/test_public_boundary.py refuses a public file that names a private path.
# private-ci.yml is the one exception, and it is only allowed to say the folder's name.
PRIVATE_DIR = "private"


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

    def test_a_newer_candidate_cancels_the_older_checks(self) -> None:
        text = _read("candidate.yml")
        for job in ("test", "first-run"):
            self.assertIn(
                f"    concurrency:\n      group: candidate-{job}-${{{{ github.ref }}}}\n"
                "      cancel-in-progress: true\n",
                text,
                job,
            )

    def test_a_stuck_report_holds_no_concurrency_group(self) -> None:
        """On 2026-10-06 three report jobs stayed `waiting` on the report environment,
        which has no reviewers: GitHub never finished evaluating its branch policy. A
        waiting run kept the workflow-level group, cancel-in-progress did not free it,
        and candidate/main ran nothing for five days until the run was force-cancelled.
        So only the check jobs take a group, and the report job takes none."""
        text = _read("candidate.yml")
        self.assertNotIn("\nconcurrency:", text)
        self.assertNotIn("concurrency:", _report_job())

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


class PrivateCiTests(unittest.TestCase):
    """The hosted edition's tests cannot run on the public mirror, because the code
    they test is never published. This workflow is the one check billed to this
    repository, so each test pins a way it could cost more or check less."""

    def test_it_runs_only_on_pull_requests_that_could_break_it(self) -> None:
        """Any other trigger runs billed minutes for changes that cannot affect the
        hosted edition. A documentation-only pull request is exactly that."""
        text = _read("private-ci.yml")
        self.assertIn(
            f'\non:\n  pull_request:\n    paths: ["{PRIVATE_DIR}/**", "src/**", "tests/**", '
            '"pyproject.toml", ".github/workflows/private-ci.yml"]\n',
            text,
        )
        for trigger in ("  push:", "schedule", "workflow_dispatch", "workflow_call", "tags:"):
            self.assertNotIn(trigger, text)

    def test_it_is_guarded_to_the_private_repository(self) -> None:
        """Workflows are published with every candidate. On the public mirror the
        code under test is absent, so the job must not start there at all."""
        self.assertIn(f"    if: {PRIVATE_ONLY}\n", _read("private-ci.yml"))

    def test_it_runs_once_on_linux(self) -> None:
        """Service containers exist only on Linux runners, and a macOS or Windows
        minute costs two to ten times as much on a private repository."""
        text = _read("private-ci.yml")
        self.assertEqual(text.count("runs-on:"), 1)
        self.assertIn("    runs-on: ubuntu-latest\n", text)
        self.assertNotIn("matrix", text)
        self.assertEqual(text.count("    timeout-minutes: 20\n"), 1)

    def test_it_cannot_skip_the_postgres_tests(self) -> None:
        """Without MEMORYSAFE_REQUIRE_POSTGRES a missing database or driver skips every
        Postgres test, and the job goes green having checked nothing."""
        text = _read("private-ci.yml")
        self.assertIn('      MEMORYSAFE_REQUIRE_POSTGRES: "1"\n', text)
        self.assertIn("      MEMORYSAFE_TEST_POSTGRES_URL:", text)
        self.assertIn(
            f"uv run --no-sync python -m unittest discover -s {PRIVATE_DIR}/tests -t .\n", text
        )

    def test_it_runs_only_the_cheap_unit_tests(self) -> None:
        """Billed minutes. The public suite, the plugin build and the real first start
        already run for free on the public mirror, so repeating any of them here would
        pay for the same answer twice."""
        text = _read("private-ci.yml")
        for expensive in (
            "tests/e2e",
            "build_plugin",
            "discover -s tests ",
            "upload-artifact",
            "uv pip compile",
        ):
            self.assertNotIn(expensive, text)
        self.assertEqual(text.count("runs-on:"), 1)


class HostedDeployTests(unittest.TestCase):
    """These two workflows deploy the hosted edition to AWS. They are published with every
    candidate, so each test pins a way one could start on the public mirror, deploy to the
    wrong place, or hand credentials to code that a tag can change."""

    FILES = ("hosted-staging.yml", "hosted-release.yml")

    def test_they_run_only_here(self) -> None:
        for name in self.FILES:
            self.assertIn(f"    if: {PRIVATE_ONLY}\n", _read(name), name)

    def test_no_job_names_an_environment(self) -> None:
        """The deploy role trusts the branch and tag subjects. An environment would change
        the OIDC subject to repo:...:environment:<name>, and the role would refuse it."""
        for name in self.FILES:
            self.assertNotIn("environment:", _read(name), name)

    def test_staging_runs_on_main_pushes_that_touch_hosted_code(self) -> None:
        text = _read("hosted-staging.yml")
        self.assertIn(
            f'\non:\n  push:\n    branches: [main]\n    paths: ["{PRIVATE_DIR}/**", "src/**", '
            '"pyproject.toml", ".github/workflows/hosted-staging.yml"]\n',
            text,
        )
        for trigger in ("pull_request", "schedule", "workflow_dispatch"):
            self.assertNotIn(trigger, text)

    def test_release_runs_on_hosted_tags_only(self) -> None:
        text = _read("hosted-release.yml")
        self.assertIn('\non:\n  push:\n    tags: ["hosted-v*"]\n', text)
        for trigger in ("pull_request", "schedule", "workflow_dispatch", "branches"):
            self.assertNotIn(trigger, text)

    def test_a_deploy_is_never_cancelled(self) -> None:
        for name, group in (("hosted-staging.yml", "hosted-staging"), ("hosted-release.yml", "hosted-prod")):
            self.assertIn(
                f"concurrency:\n  group: {group}\n  cancel-in-progress: false\n", _read(name), name
            )

    def test_the_release_never_builds(self) -> None:
        """A release ships the image staging already built and tested."""
        text = _read("hosted-release.yml")
        for build in ("build-push-action", "setup-buildx", "docker build"):
            self.assertNotIn(build, text)

    def test_the_role_comes_from_a_variable(self) -> None:
        for name in self.FILES:
            text = _read(name)
            self.assertIn("vars.HOSTED_DEPLOY_ROLE_ARN", text, name)
            self.assertNotRegex(text, r"\b\d{12}\b", name)
            self.assertNotIn("aws-access-key-id", text, name)

    def test_actions_that_see_credentials_are_pinned(self) -> None:
        """A tag can be moved to different code."""
        for name in self.FILES:
            uses = re.findall(r"^\s*(?:- )?uses: (\S+)", _read(name), re.MULTILINE)
            self.assertTrue(uses, name)
            for action in uses:
                if action.startswith(("actions/checkout@", "actions/setup-python@")):
                    continue
                self.assertRegex(action, r"@[0-9a-f]{40}$", f"{name}: {action}")

    def test_only_a_missing_stack_starts_a_first_deploy(self) -> None:
        """stack-exists exits 1 for a missing stack and 3 for any other error. Branching
        on a bare `if` would run the empty first deploy on a live stack after a throttle."""
        for name in self.FILES:
            text = _read(name)
            self.assertIn("deploy stack-exists", text, name)
            self.assertIn("|| rc=$?", text, name)
            self.assertNotIn("if python -m memorysafe_infra.deploy stack-exists", text, name)
            self.assertIn('exit "$rc"', text, name)

    def test_the_release_says_nothing_was_deployed_without_a_zone(self) -> None:
        self.assertIn(
            "::notice::Nothing deployed; production deploys start once the memorysafe.ca zone is in this account.",
            _read("hosted-release.yml"),
        )

    def test_a_first_deploy_has_an_hour(self) -> None:
        """A first deploy creates a database and certificates, and cannot be resumed by a re-run."""
        for name in self.FILES:
            self.assertIn("    timeout-minutes: 60\n", _read(name), name)

    def test_the_interpreter_and_packages_are_set_up_before_the_credentials(self) -> None:
        """The interpreter and pip install run first, so no action or package install code
        runs with credentials in the environment. The ECR login needs them, so it follows."""
        for name in self.FILES:
            text = _read(name)
            creds = text.index("configure-aws-credentials@")
            self.assertLess(text.index("actions/setup-python@"), creds, name)
            self.assertLess(text.index("pip install"), creds, name)
            if "amazon-ecr-login" in text:
                self.assertGreater(text.index("amazon-ecr-login"), creds, name)

    def test_the_release_tags_its_image_after_checking_it_and_before_deploying(self) -> None:
        text = _read("hosted-release.yml")
        check = text.index("deploy check-release")
        retag = text.index('deploy retag "$SHA" "$GITHUB_REF_NAME"')
        self.assertLess(check, retag)
        self.assertLess(retag, text.index("Find the zone"))

    def test_the_release_checks_the_commit_is_on_main_before_tagging_its_image(self) -> None:
        """A tag on a commit that is not on main must not get a protected, immutable hosted-v tag on its image."""
        text = _read("hosted-release.yml")
        self.assertLess(text.index("merge-base --is-ancestor"), text.index('deploy retag "$SHA"'))

    def test_private_ci_runs_infra_tests_only_when_infra_changes(self) -> None:
        text = _read("private-ci.yml")
        self.assertIn("git diff --quiet", text)
        self.assertIn(f"unittest {PRIVATE_DIR}.tests.test_infra", text)

    def test_the_web_checks_run_only_when_the_web_app_changes(self) -> None:
        """The hosted edition's web app is checked in a browser, which costs a node install,
        a browser download and a few billed minutes. A pull request that leaves the app and
        the code it calls alone pays none of it; one that changes them must run it, in the one
        job, so the 20-minute ceiling above still holds."""
        text = _read("private-ci.yml")
        # The browser suite is the only real-browser test of the server code the app shares
        # with the older pages (the account pages, organizations, the sign-in), so a change to
        # any of the hosted package, the suite's harness or this workflow runs it.
        watched = " ".join((
            f"{PRIVATE_DIR}/web",
            f"{PRIVATE_DIR}/src/memorysafe_hosted",
            f"{PRIVATE_DIR}/tests/webapp_e2e_server.py",
            f"{PRIVATE_DIR}/tests/pg_support.py",
            ".github/workflows/private-ci.yml",
        ))
        self.assertIn(
            "      - name: Web tests\n        if: github.event_name == 'pull_request'\n        run: |\n"
            f"          git diff --quiet ${{{{ github.event.pull_request.base.sha }}}} HEAD -- {watched} || {{\n",
            text,
        )
        for step in ("npm ci", "npm run e2e", "node-version: 22"):
            self.assertIn(step, text)
        self.assertEqual(text.count("runs-on:"), 1)
        self.assertEqual(text.count("    timeout-minutes: 20\n"), 1)


if __name__ == "__main__":
    unittest.main()
