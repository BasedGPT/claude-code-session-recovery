"""Validate versioned public toolkit releases.

The repository has no package installer metadata, so VERSION is the single
source of the human release identity. This module is the shared seam for the
local check, pull-request CI, and tag verification.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence


VERSION_PATH = "VERSION"
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleasePolicyError(RuntimeError):
    """A controlled release-policy or Git evidence failure."""


@dataclass(frozen=True, order=True)
class Version:
    """Strict numeric SemVer used by this repository's release policy."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class TransitionCheck:
    """Evidence returned after a version transition passes all gates."""

    base_commit: str
    head_commit: str
    base_version: str | None
    head_version: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class TagCheck:
    """Evidence returned after an annotated tag is bound to one commit."""

    tag: str
    version: str
    commit: str


def parse_version(value: str) -> Version:
    """Parse one strict numeric ``major.minor.patch`` version."""
    if not isinstance(value, str):
        raise ReleasePolicyError("version must be text")
    match = SEMVER_RE.fullmatch(value.strip())
    if not match:
        raise ReleasePolicyError(
            f"invalid version {value!r}; expected numeric major.minor.patch"
        )
    return Version(*(int(part) for part in match.groups()))


def _validate_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip() or "\x00" in ref:
        raise ReleasePolicyError("Git ref must be non-empty text without NUL")
    return ref.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ReleasePolicyError(f"could not start Git: {error}") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleasePolicyError(
            f"Git command failed ({' '.join(args)}): {detail or result.returncode}"
        )
    return result.stdout


def _git_text(root: Path, *args: str) -> str:
    return _git_bytes(root, *args).decode("utf-8", errors="strict")


def resolve_commit(root: Path, ref: str) -> str:
    """Resolve a ref to a full commit SHA."""
    ref = _validate_ref(ref)
    commit = _git_text(root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    if not COMMIT_RE.fullmatch(commit):
        raise ReleasePolicyError(f"Git returned an invalid commit for {ref!r}")
    return commit


def _read_blob(root: Path, ref: str, path: str) -> bytes | None:
    """Read a committed blob, returning None only when the path is absent."""
    ref = _validate_ref(ref)
    resolve_commit(root, ref)
    try:
        return _git_bytes(root, "cat-file", "blob", f"{ref}:{path}")
    except ReleasePolicyError as error:
        if (
            "Not a valid object name" in str(error)
            or "does not exist" in str(error)
            or "not in" in str(error)
        ):
            return None
        raise


def read_version_at(root: Path, ref: str) -> Version | None:
    """Read VERSION from a commit, returning None for a pre-version baseline."""
    blob = _read_blob(root, ref, VERSION_PATH)
    if blob is None:
        return None
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ReleasePolicyError(f"{ref}: VERSION is not UTF-8") from error
    return parse_version(text.strip())


def changed_paths(root: Path, base: str, head: str) -> tuple[str, ...]:
    """Return changed repository paths between two commit refs."""
    base = _validate_ref(base)
    head = _validate_ref(head)
    resolve_commit(root, base)
    resolve_commit(root, head)
    raw = _git_bytes(
        root,
        "diff",
        "--name-only",
        "--no-renames",
        "--diff-filter=ACDMRTUXB",
        "-z",
        f"{base}...{head}",
        "--",
    )
    paths = {
        item.replace("\\", "/")
        for item in raw.decode("utf-8", errors="strict").split("\x00")
        if item
    }
    return tuple(sorted(paths))


def check_tag(root: Path, tag: str, ref: str = "HEAD") -> TagCheck:
    """Require an annotated ``vX.Y.Z`` tag to point exactly at ``ref``."""
    tag = _validate_ref(tag)
    ref = _validate_ref(ref)
    tag_match = TAG_RE.fullmatch(tag)
    if not tag_match:
        raise ReleasePolicyError(f"invalid release tag {tag!r}")
    expected_version = parse_version(".".join(tag_match.groups()))
    ref_commit = resolve_commit(root, ref)
    try:
        tag_type = _git_text(root, "cat-file", "-t", tag).strip()
    except ReleasePolicyError as error:
        raise ReleasePolicyError(
            f"release tag {tag} is missing; it must be annotated"
        ) from error
    if tag_type != "tag":
        raise ReleasePolicyError(f"release tag {tag} must be annotated, not {tag_type}")
    tag_commit = resolve_commit(root, tag)
    if tag_commit != ref_commit:
        raise ReleasePolicyError(
            f"release tag {tag} points to {tag_commit}, expected {ref_commit}"
        )
    version = read_version_at(root, ref)
    if version != expected_version:
        actual = "absent" if version is None else str(version)
        raise ReleasePolicyError(
            f"release tag {tag} expects VERSION {expected_version}, found {actual}"
        )
    return TagCheck(tag=tag, version=str(version), commit=ref_commit)


def check_transition(root: Path, base: str, head: str = "HEAD") -> TransitionCheck:
    """Verify that one public change creates the next versioned release."""
    base = _validate_ref(base)
    head = _validate_ref(head)
    base_commit = resolve_commit(root, base)
    head_commit = resolve_commit(root, head)
    base_version = read_version_at(root, base)
    head_version = read_version_at(root, head)
    if head_version is None:
        raise ReleasePolicyError(f"{head}: VERSION is missing")
    paths = changed_paths(root, base, head)
    if VERSION_PATH not in paths:
        raise ReleasePolicyError("every public change must change VERSION")
    if base_version is not None:
        if head_version <= base_version:
            raise ReleasePolicyError(
                f"VERSION must increase from {base_version} to {head_version}"
            )
        check_tag(root, f"v{base_version}", base)
    return TransitionCheck(
        base_commit=base_commit,
        head_commit=head_commit,
        base_version=None if base_version is None else str(base_version),
        head_version=str(head_version),
        changed_paths=paths,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (defaults to the current directory)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--base", required=True)
    check_parser.add_argument("--head", default="HEAD")

    tag_parser = subparsers.add_parser("tag-check")
    tag_parser.add_argument("--tag", required=True)
    tag_parser.add_argument("--ref", default="HEAD")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"ERROR repository root does not exist: {root}", file=sys.stderr)
        return 1
    try:
        if args.command == "check":
            result = check_transition(root, args.base, args.head)
            print(
                "RELEASE_POLICY_OK "
                f"version={result.head_version} "
                f"base={result.base_commit} head={result.head_commit} "
                f"changed={len(result.changed_paths)}"
            )
            return 0
        if args.command == "tag-check":
            result = check_tag(root, args.tag, args.ref)
            print(
                f"RELEASE_TAG_OK tag={result.tag} version={result.version} "
                f"commit={result.commit}"
            )
            return 0
        raise ReleasePolicyError(f"unknown command {args.command}")
    except ReleasePolicyError as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
