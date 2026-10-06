"""Put a built plugin into a clone of the public repository, and publish it with --push.

Publishing is pushing a v* tag on main in the source repository: the
release workflow there runs the suite and a real first start, then runs this with --push.
Both hosts update from the public main branch on their own, so nothing else publishes.

Without --push this commits and tags in the clone and stops, which is how to rehearse a
release locally. With --push it pushes, in one atomic push, main, the tag and a
release-assets/<tag> branch holding the Claude Desktop extension and the release notes.
The public repository's workflow turns that branch into the GitHub Release and deletes
it, because the deploy key this runs with can push but cannot create releases.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_plugin  # noqa: E402
from plugin_meta import project_version  # noqa: E402


PUBLIC_BRANCH = "main"
ASSET_BRANCH_PREFIX = build_plugin.ASSET_BRANCH_PREFIX
NOTES_NAME = build_plugin.NOTES_NAME


def _git(checkout: Path, *arguments: str, stdin: str | None = None) -> str:
    # No text=True / universal_newlines here: that mode's newline translation
    # runs on WRITES to the child's stdin too, not only on reads of its stdout.
    # On Windows it turns every "\n" in `stdin` into "\r\n" before git ever sees
    # it, and _asset_commit() pipes a git mktree spec and a hash-object --stdin
    # payload through this helper -- so the CR would land inside the committed
    # filename. Encode and decode ourselves so no platform-dependent translation
    # happens in either direction.
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        input=stdin.encode("utf-8") if stdin is not None else None,
    )
    return result.stdout.decode("utf-8")


def _asset_commit(checkout: Path, mcpb: Path, notes: str, version: str) -> str:
    """A parentless commit holding only the extension and the notes, built without
    touching the clone's working tree or index."""

    extension = _git(checkout, "hash-object", "-w", str(mcpb)).strip()
    notes_blob = _git(checkout, "hash-object", "-w", "--stdin", stdin=notes).strip()
    tree = _git(
        checkout,
        "mktree",
        stdin=f"100644 blob {extension}\t{build_plugin.MCPB_NAME}\n100644 blob {notes_blob}\t{NOTES_NAME}\n",
    ).strip()
    return _git(checkout, "commit-tree", tree, "-m", f"Release assets for MemorySafe {version}").strip()


def release(
    root: Path,
    checkout: Path,
    output_dir: Path,
    *,
    tag: str | None = None,
    notes: str | None = None,
) -> str:
    if not (checkout / ".git").exists():
        raise SystemExit(f"{checkout} is not a git clone of {build_plugin.PUBLIC_REPOSITORY}.")
    if _git(checkout, "status", "--porcelain").strip():
        raise SystemExit(f"{checkout} has uncommitted changes. Release into a clean clone.")
    version = project_version(root)
    expected = f"v{version}"
    if tag is not None and tag != expected:
        raise SystemExit(f"The tag is {tag}, but pyproject.toml says {version}. Tag {expected} instead.")
    tag = expected
    if _git(checkout, "tag", "--list", tag).strip():
        raise SystemExit(f"{tag} already exists in {checkout}. Bump the version first.")

    tree, mcpb = build_plugin.build_plugin(root, output_dir)
    for child in checkout.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(tree, checkout, dirs_exist_ok=True)
    _git(checkout, "add", "--all")
    staged = subprocess.run(["git", "-C", str(checkout), "diff", "--cached", "--quiet"]).returncode != 0
    # Nothing staged means main already holds this build: a publish that pushed main and
    # then failed. The manifests carry the version, so it cannot be another release.
    if staged:
        _git(checkout, "commit", "--quiet", "--message", f"MemorySafe {version}")
    _git(checkout, "tag", "--annotate", tag, "--message", f"MemorySafe {version}")

    notes = notes.strip() if notes else ""
    asset = _asset_commit(checkout, mcpb, (notes or f"MemorySafe {version}") + "\n", version)
    _git(checkout, "branch", "--force", f"{ASSET_BRANCH_PREFIX}{tag}", asset)
    return tag


def publish(checkout: Path, tag: str) -> None:
    if not _git(checkout, "ls-remote", "--heads", "origin").strip():
        # GitHub makes the first branch pushed into an empty repository its default,
        # and the hosts install from the default branch.
        _git(checkout, "push", "--quiet", "origin", f"HEAD:refs/heads/{PUBLIC_BRANCH}")
    _git(
        checkout,
        "push",
        "--quiet",
        "--atomic",
        "origin",
        f"HEAD:refs/heads/{PUBLIC_BRANCH}",
        f"refs/tags/{tag}",
        f"refs/heads/{ASSET_BRANCH_PREFIX}{tag}",
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist-checkout", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=root / "dist")
    parser.add_argument("--tag", help="the tag that triggered the release; must be v<pyproject version>")
    parser.add_argument("--notes-file", type=Path, help="release notes; defaults to 'MemorySafe <version>'")
    parser.add_argument("--push", action="store_true", help="publish; only the release workflow passes this")
    args = parser.parse_args()
    checkout = args.dist_checkout.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    notes = args.notes_file.read_text(encoding="utf-8") if args.notes_file else None
    tag = release(root, checkout, output, tag=args.tag, notes=notes)
    if args.push:
        publish(checkout, tag)
        print(f"Published {tag} to {build_plugin.PUBLIC_REPOSITORY}.")
        return
    print(f"Committed and tagged {tag} in {checkout}. Nothing has been published.")
    print(f"To publish, push {tag} on main in the source repository:")
    print(f"  git tag --annotate {tag} && git push --atomic origin main {tag}")


if __name__ == "__main__":
    main()
