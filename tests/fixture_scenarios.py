"""Shared fixture scenario execution for fixture verification and regeneration.

This module deliberately does not read, compare, or write golden files.  Its
responsibility is to run the scenario against an isolated copy of fixture
state, return the subprocess and diagnosis outcomes, and clean up that copy.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Any, Iterator, Mapping, Optional, Sequence


@dataclass(frozen=True)
class FixturePaths:
    """The fixed on-disk layout for one fixture scenario."""

    fixture_dir: str
    state_dir: str
    golden_dir: str
    diagnose_json: str
    diagnose_text: str
    dry_run_text: str
    dry_run_exit: str
    post_mutation_json: str
    mutator_json: str

    @classmethod
    def from_fixture_dir(cls, fixture_dir: str) -> "FixturePaths":
        golden_dir = os.path.join(fixture_dir, "golden")
        return cls(
            fixture_dir=fixture_dir,
            state_dir=os.path.join(fixture_dir, "state"),
            golden_dir=golden_dir,
            diagnose_json=os.path.join(golden_dir, "diagnose.json"),
            diagnose_text=os.path.join(golden_dir, "diagnose.txt"),
            dry_run_text=os.path.join(golden_dir, "dry-run.txt"),
            dry_run_exit=os.path.join(golden_dir, "dry-run.exit"),
            post_mutation_json=os.path.join(golden_dir, "post-mutation.json"),
            mutator_json=os.path.join(golden_dir, "mutator.json"),
        )

    @property
    def name(self) -> str:
        return os.path.basename(self.fixture_dir)


@dataclass(frozen=True)
class CommandOutcome:
    """Captured result of one fixture-tool subprocess invocation."""

    args: Sequence[str]
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class DiagnosisOutcome:
    """The parsed JSON diagnosis and the command that produced it."""

    command: CommandOutcome
    payload: Mapping[str, Any]

    @property
    def diagnosis_id(self) -> str:
        return self.payload["diagnosis_id"]


@dataclass(frozen=True)
class DryRunOutcome:
    """A dry-run command and fingerprints of its isolated fixture state."""

    command: CommandOutcome
    state_before: str
    state_after: str

    @property
    def stdout(self) -> str:
        return self.command.stdout

    @property
    def stderr(self) -> str:
        return self.command.stderr

    @property
    def returncode(self) -> int:
        return self.command.returncode

    @property
    def state_unchanged(self) -> bool:
        return self.state_before == self.state_after


@dataclass(frozen=True)
class ApplyOutcome:
    """Successful apply command plus the diagnosis of its isolated state."""

    command: CommandOutcome
    post_diagnosis: DiagnosisOutcome


class FixtureScenario:
    """Execute one fixture scenario without ever mutating its source state."""

    def __init__(
        self,
        fixture_dir: str,
        repo_root: str,
        *,
        temp_parent: Optional[str] = None,
    ) -> None:
        self.paths = FixturePaths.from_fixture_dir(fixture_dir)
        self.repo_root = repo_root
        self.diagnose_path = os.path.join(repo_root, "tools", "diagnose.py")
        self.temp_parent = temp_parent

    def diagnose_json(self, state_dir: Optional[str] = None) -> DiagnosisOutcome:
        """Run the JSON diagnosis for fixture state and parse its output."""
        state = state_dir or self.paths.state_dir
        command = self._run(
            [sys.executable, self.diagnose_path, "--state", state, "--json"],
            check=True,
        )
        return DiagnosisOutcome(command=command, payload=json.loads(command.stdout))

    def diagnose_text(self) -> CommandOutcome:
        """Run the human-readable diagnosis for the original fixture state."""
        return self._run(
            [sys.executable, self.diagnose_path, "--state", self.paths.state_dir],
            check=True,
        )

    def find_mutator(self, diagnosis: Mapping[str, Any]) -> Optional[str]:
        """Return the first matched mutator as an absolute path, if any."""
        contract = self._mutator_contract()
        if contract is not None:
            return os.path.join(
                self.repo_root, os.path.normpath(contract["mutator"])
            )
        for problem in diagnosis.get("matched_problems", []):
            mutator_rel = problem.get("mutator")
            if mutator_rel:
                return os.path.join(self.repo_root, os.path.normpath(mutator_rel))
        return None

    def run_dry_mutator(
        self,
        mutator_path: str,
        diagnosis_id: str,
        *,
        temp_prefix: str,
    ) -> DryRunOutcome:
        """Run a mutator in dry-run mode against an isolated state copy."""
        with self._temporary_state(temp_prefix) as state_dir:
            state_before = state_fingerprint(state_dir)
            command = self._run(
                [
                    sys.executable,
                    mutator_path,
                    *self._mutator_arguments(state_dir),
                    "--state",
                    state_dir,
                    "--diagnosis-id",
                    diagnosis_id,
                ],
                check=False,
            )
            return DryRunOutcome(
                command=command,
                state_before=state_before,
                state_after=state_fingerprint(state_dir),
            )

    def apply_and_diagnose(
        self,
        mutator_path: str,
        diagnosis_id: str,
        *,
        temp_prefix: str,
    ) -> ApplyOutcome:
        """Apply a mutator to an isolated state copy and diagnose that copy."""
        with self._temporary_state(temp_prefix) as state_dir:
            command = self._run(
                [
                    sys.executable,
                    mutator_path,
                    *self._mutator_arguments(state_dir),
                    "--state",
                    state_dir,
                    "--diagnosis-id",
                    diagnosis_id,
                    "--apply",
                ],
                check=True,
            )
            return ApplyOutcome(
                command=command,
                post_diagnosis=self.diagnose_json(state_dir),
            )

    def _run(self, args: Sequence[str], *, check: bool) -> CommandOutcome:
        """Run a fixture tool with the capture and failure semantics callers use."""
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=check,
        )
        return CommandOutcome(
            args=tuple(args),
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )

    def _mutator_contract(self) -> Optional[Mapping[str, Any]]:
        if not os.path.isfile(self.paths.mutator_json):
            return None
        with open(self.paths.mutator_json, encoding="utf-8") as source:
            contract = json.load(source)
        if not isinstance(contract, dict) or not isinstance(contract.get("mutator"), str):
            raise ValueError("golden/mutator.json must declare one mutator path")
        return contract

    def _mutator_arguments(self, state_dir: str) -> list[str]:
        contract = self._mutator_contract()
        if contract is None:
            return []
        arguments = contract.get("arguments", [])
        if not isinstance(arguments, list) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            raise ValueError("mutator arguments must be a list of strings")
        return [argument.replace("{state}", state_dir) for argument in arguments]

    def _prepare_state_assets(self, state_dir: str) -> None:
        contract = self._mutator_contract()
        if contract is None or "zip_tree" not in contract:
            return
        zip_tree = contract["zip_tree"]
        zip_output = contract.get("zip_output")
        if not isinstance(zip_tree, str) or not isinstance(zip_output, str):
            raise ValueError("zip_tree fixtures require a string zip_output")
        source_root = os.path.join(state_dir, os.path.normpath(zip_tree))
        destination = os.path.join(state_dir, os.path.normpath(zip_output))
        if not os.path.isdir(source_root):
            raise ValueError("fixture zip_tree source is missing")
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for dirpath, dirnames, filenames in os.walk(source_root):
                dirnames.sort()
                for filename in sorted(filenames):
                    path = os.path.join(dirpath, filename)
                    archive.write(
                        path,
                        os.path.relpath(path, source_root).replace(os.sep, "/"),
                    )

    @contextmanager
    def _temporary_state(self, prefix: str) -> Iterator[str]:
        """Copy fixture state to a disposable directory and remove it always."""
        temp_dir = tempfile.mkdtemp(prefix=prefix, dir=self.temp_parent)
        try:
            state_dir = os.path.join(temp_dir, "state")
            shutil.copytree(self.paths.state_dir, state_dir)
            self._prepare_state_assets(state_dir)
            yield state_dir
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def state_fingerprint(path: str) -> str:
    """Return a deterministic content-and-layout fingerprint for fixture state."""
    digest = hashlib.sha256()
    if not os.path.isdir(path):
        digest.update(b"not-a-directory\0")
        return digest.hexdigest()

    digest.update(b"directory\0")
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        filenames.sort()
        relative_dir = os.path.relpath(dirpath, path)
        digest.update(b"directory\0")
        digest.update(relative_dir.encode("utf-8"))
        digest.update(b"\0")
        for filename in filenames:
            file_path = os.path.join(dirpath, filename)
            relative_path = os.path.relpath(file_path, path)
            digest.update(b"file\0")
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            with open(file_path, "rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def read_exit_contract(path: str) -> int:
    """Read one non-negative exit status from a fixture dry-run contract."""
    with open(path, encoding="utf-8") as source:
        text = source.read()
    try:
        value = int(text.strip())
    except ValueError as error:
        raise ValueError("dry-run exit contract must be an integer") from error
    if value < 0:
        raise ValueError("dry-run exit contract must be non-negative")
    return value
