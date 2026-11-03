#!/usr/bin/env python3
"""Publish this commit, without private/, as a candidate for the public CI repository.

MemorySafe-Labs/memorysafe is private and on the free plan, where Actions minutes are
metered and a macOS minute counts ten times; from 2026-09-24 to 2026-09-28 no job could
start at all. Hosted runners are free on a public repository, so pull requests are
checked on MemorySafe-Labs/memorysafe-ci instead. The package under src/ and plugin/
already ships as source in the public plugin; the rest outside private/ (scripts, tests,
workflows, packaging, legal) was reviewed and judged publishable. Anything that is not
belongs under private/.

A candidate is one commit with no parent, authored by nobody: this repository's history,
its commit messages and its authors' addresses never leave the machine.

Stdlib only. publish-candidate.yml runs this with the runner's python3 and no venv.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SOURCE_REPOSITORY = "MemorySafe-Labs/memorysafe"
PUBLIC_REPOSITORY = "MemorySafe-Labs/memorysafe-ci"
PUBLIC_REMOTE = f"https://github.com/{PUBLIC_REPOSITORY}.git"

PRIVATE_DIR = "private"

# Private code that has to live outside private/ for now, and so would be published
# unless it is named here. Empty: the hosted edition's Postgres backend and its tests
# moved into private/ with PR #39. The tuple stays as the one place to list anything
# private that must temporarily sit elsewhere, and the leak guard still checks it
# before every push. Matched with fnmatch, where * also crosses "/".
PRIVATE_PATHS_OUTSIDE_PRIVATE: tuple[str, ...] = ()

# A fixed identity, so a candidate carries no developer's name or address.
_IDENTITY = {
    "GIT_AUTHOR_NAME": "MemorySafe CI",
    "GIT_AUTHOR_EMAIL": "ci@memorysafe.invalid",
    "GIT_COMMITTER_NAME": "MemorySafe CI",
    "GIT_COMMITTER_EMAIL": "ci@memorysafe.invalid",
}

# How long to wait for the public run to appear after a push: attempts, and seconds
# between them. Five minutes; GitHub usually lists it within ten seconds.
_WATCH = (60, 5.0)


class Refusal(Exception):
    """Nothing was published, and the message says why and what to do."""


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout


def source_commit(repo: Path) -> str:
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").splitlines()
    if dirty:
        raise Refusal(
            f"{len(dirty)} tracked file(s) differ from HEAD. Commit or stash them first: "
            "a candidate is exactly one commit, so its result can be attached to that commit."
        )
    return _git(repo, "rev-parse", "HEAD").strip()


def public_tree(repo: Path) -> str:
    """HEAD without private/, built in a temporary index.

    The real index is left alone: using it would unstage the developer's work and leave
    private/ staged for deletion.
    """
    with tempfile.TemporaryDirectory() as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(repo, "read-tree", "HEAD", env=env)
        _git(repo, "rm", "--cached", "-r", "--quiet", "--ignore-unmatch", PRIVATE_DIR, env=env)
        return _git(repo, "write-tree", env=env).strip()


def leaks(repo: Path, tree: str) -> list[str]:
    """Paths in the tree that must not be published. Checked before every push, because
    the boundary test runs in CI, and by then the tree is public."""
    paths = _git(repo, "ls-tree", "-r", "--name-only", "-z", tree).split("\0")
    found = []
    for path in filter(None, paths):
        if path.startswith(f"{PRIVATE_DIR}/") or any(
            fnmatch.fnmatchcase(path, pattern) for pattern in PRIVATE_PATHS_OUTSIDE_PRIVATE
        ):
            found.append(path)
    return sorted(found)


def candidate_ref(repo: Path, branch: str | None, release_tag: str | None) -> str:
    if release_tag:
        # Its own namespace, apart from candidate/<branch>: a branch named release/0.4.5
        # existed here, and the public-ci/release context lets release.yml publish.
        return f"release-candidate/{release_tag}"
    if not branch:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch == "HEAD":
            raise Refusal("HEAD is detached, so there is no branch to name the candidate after. Pass --branch.")
    return f"candidate/{branch}"


def candidate_commit(repo: Path, tree: str, ref: str, source: str) -> str:
    message = f"Candidate for {ref}\n\nSource-Commit: {source}\n"
    return _git(repo, "commit-tree", tree, "-m", message, env=_IDENTITY).strip()


def export(repo: Path, tree: str, directory: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", tree], check=True, capture_output=True
    ).stdout
    directory.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(directory, filter="data")


def push(repo: Path, remote: str, commit: str, ref: str) -> None:
    """Force-push one commit. git sends only objects reachable from it, and it has no
    parent, so nothing of this repository's history goes with it."""
    _git(repo, "push", "--quiet", "--force", remote, f"{commit}:refs/heads/{ref}")


def _gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def status_context(ref: str) -> str:
    return "public-ci/release" if ref.startswith("release-candidate/") else "public-ci"


def set_status(sha: str, state: str, context: str, description: str, target_url: str | None = None) -> None:
    fields = [f"state={state}", f"context={context}", f"description={description}"]
    if target_url:
        fields.append(f"target_url={target_url}")
    arguments = ["api", f"repos/{SOURCE_REPOSITORY}/statuses/{sha}", "--silent"]
    for field in fields:
        arguments += ["-f", field]
    result = _gh(*arguments)
    if result.returncode != 0:
        # The candidate is already pushed and its run will report on its own.
        print(f"push_candidate: could not set the {state} status: {result.stderr.strip()}", file=sys.stderr)


def watch(candidate: str) -> tuple[bool, str | None]:
    """Wait for the public run on this candidate. (passed, url); url is None if no run
    appeared."""
    attempts, pause = _WATCH
    for _ in range(attempts):
        listed = _gh(
            "run", "list", "--repo", PUBLIC_REPOSITORY, "--commit", candidate,
            "--json", "databaseId,url", "--limit", "1",
        )
        try:
            runs = json.loads(listed.stdout or "[]") if listed.returncode == 0 else []
        except json.JSONDecodeError:
            # gh printed something else after the push had succeeded; a traceback here
            # left the private commit on pending. Treat it as no run yet.
            runs = []
        if runs:
            watched = _gh("run", "watch", str(runs[0]["databaseId"]), "--repo", PUBLIC_REPOSITORY, "--exit-status")
            return watched.returncode == 0, runs[0]["url"]
        time.sleep(pause)
    return False, None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remote", default=PUBLIC_REMOTE, help="where to push (default: %(default)s)")
    parser.add_argument("--branch", help="name the candidate after this branch (default: the current one)")
    parser.add_argument("--release-tag", help="push release-candidate/<tag>, which gates a release")
    parser.add_argument("--dry-run", metavar="DIR", help="write the public tree to DIR and push nothing")
    parser.add_argument("--no-wait", action="store_true", help="stop after the push; the public run reports back")
    return parser


def main(argv: list[str] | None = None, repo: Path = ROOT) -> int:
    args = _parser().parse_args(argv)
    try:
        source = source_commit(repo)
        ref = candidate_ref(repo, args.branch, args.release_tag)
        tree = public_tree(repo)
        found = leaks(repo, tree)
        if found:
            raise Refusal(
                "these paths are private and would be published: "
                + ", ".join(found)
                + f". Move them under {PRIVATE_DIR}/, or publish from a branch without them."
            )
        if args.dry_run:
            export(repo, tree, Path(args.dry_run))
            print(f"Wrote the public tree for {ref} to {args.dry_run}. Nothing was pushed.")
            return 0
        commit = candidate_commit(repo, tree, ref, source)
        push(repo, args.remote, commit, ref)
    except Refusal as refusal:
        print(f"push_candidate: {refusal}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as failed:
        print(f"push_candidate: {' '.join(failed.cmd[3:])} failed: {failed.stderr.strip()}", file=sys.stderr)
        return 1

    context = status_context(ref)
    print(f"Pushed {ref} for {source[:12]} to {args.remote}.")
    set_status(source, "pending", context, "Running on memorysafe-ci")
    if args.no_wait:
        return 0
    passed, url = watch(commit)
    if url is None:
        set_status(source, "error", context, "No run started on memorysafe-ci")
        print(f"push_candidate: no run appeared on {PUBLIC_REPOSITORY}. Is Actions enabled there?", file=sys.stderr)
        return 1
    set_status(source, "success" if passed else "failure", context,
               "Passed on Linux, Windows and macOS" if passed else "Failed on memorysafe-ci", url)
    print(f"{'Passed' if passed else 'Failed'}: {url}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
