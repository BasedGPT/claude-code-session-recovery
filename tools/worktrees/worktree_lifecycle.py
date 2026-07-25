"""Shared contracts for the chosen worktree lifecycle.

This module deliberately owns the details that must agree across lifecycle
tools: shrink-marker claims, shrink sentinels, and the sparse-index recipe
that makes a ``git worktree add --no-checkout`` stub quiet.  Callers retain
their own command-line policy and reporting.

The PowerShell inspector mirrors the marker and sentinel literals below. It
does not execute this Python module so that inspection remains read-only and
does not depend on a Python executable at report time.
"""

import json
import os
import subprocess
from datetime import datetime, timezone


MARKER_READY = '.shrink-when-safe'
MARKER_IN_PROGRESS_PREFIX = '.shrink-in-progress.'
SENTINEL_FILE = '.worktree-shrunk.txt'
SENTINEL_REQUIRED_FIELDS = (
    'Operation ID:', 'Branch:', 'Quarantine:', 'Manifest:', 'Shrunk:'
)


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def ready_marker_path(worktree_path):
    return os.path.join(worktree_path, MARKER_READY)


def find_in_progress_marker(worktree_path):
    if not os.path.isdir(worktree_path):
        return None
    for entry in os.listdir(worktree_path):
        if entry.startswith(MARKER_IN_PROGRESS_PREFIX):
            return entry
    return None


def marker_payload(name, op_ts, manifest_path=''):
    return {
        'op_id': op_ts,
        'pid': os.getpid(),
        'worktree': name,
        'manifest_path': manifest_path,
        'claimed_at': _iso_now(),
    }


def write_marker_payload(marker_path, payload):
    with open(marker_path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2)


def claim_marker(worktree_path, name, op_ts):
    """Atomically claim a ready marker, returning its new filename.

    ``None`` means that no ready marker was present. A pre-existing in-progress
    marker belongs to another processor or a prior failed run and therefore
    refuses the claim rather than guessing ownership.
    """
    ready = ready_marker_path(worktree_path)
    existing = find_in_progress_marker(worktree_path)
    if existing:
        raise RuntimeError(
            f'in-progress marker already present: {existing}. '
            f'Another processor may be running, or a previous shrink crashed. '
            f'Use --resume <manifest> to continue, or remove the marker after '
            f'verifying no process is alive.')
    if not os.path.exists(ready):
        return None
    new_name = f'{MARKER_IN_PROGRESS_PREFIX}{os.getpid()}.{op_ts}'
    new_path = os.path.join(worktree_path, new_name)
    os.rename(ready, new_path)
    write_marker_payload(new_path, marker_payload(name, op_ts))
    return new_name


def remove_ready_marker(worktree_path):
    """Remove a ready marker for a resumed session, returning whether it existed."""
    marker_path = ready_marker_path(worktree_path)
    if not os.path.isfile(marker_path):
        return False
    os.remove(marker_path)
    return True


def update_marker_manifest(worktree_path, claimed_marker, manifest_path):
    if not claimed_marker:
        return
    marker_path = os.path.join(worktree_path, claimed_marker)
    if not os.path.isfile(marker_path):
        return
    try:
        with open(marker_path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        payload = {}
    payload['manifest_path'] = manifest_path
    payload['updated_at'] = _iso_now()
    write_marker_payload(marker_path, payload)


def write_sentinel(stub_path, manifest):
    """Write the recovery breadcrumb with paths relative to the stub itself."""
    quarantine_path = os.path.relpath(manifest['quarantine_path'], stub_path)
    manifest_path = os.path.relpath(manifest['manifest_path'], stub_path)
    text = (
        'This worktree was shrunk to save disk space.\n\n'
        f'Operation ID: {manifest["operation_id"]}\n'
        f'Branch: {manifest["branch"]}\n'
        f'Quarantine: {quarantine_path}\n'
        f'Manifest: {manifest_path}\n'
        f'Shrunk: {manifest["start_timestamp"]}\n\n'
        'To rematerialise: from this directory, run\n'
        f'  git checkout {manifest["branch"]} -- .\n\n'
        'To restore the original folder (untracked files included):\n'
        f'  Move-Item "{quarantine_path}" .\n\n'
        f'The branch is preserved at {manifest["head_sha"]}. If git\'s view of\n'
        'the branch has moved since shrink, the rematerialised tree will differ\n'
        'from the quarantined original.\n'
    )
    with open(os.path.join(stub_path, SENTINEL_FILE), 'w', encoding='utf-8') as handle:
        handle.write(text)


def validate_sentinel(stub_path):
    sentinel = os.path.join(stub_path, SENTINEL_FILE)
    if not os.path.isfile(sentinel):
        return False, 'sentinel missing'
    try:
        with open(sentinel, encoding='utf-8') as handle:
            text = handle.read()
    except OSError as exc:
        return False, f'sentinel unreadable: {exc}'
    missing = [field for field in SENTINEL_REQUIRED_FIELDS if field not in text]
    if missing:
        return False, 'sentinel missing fields: ' + ', '.join(missing)
    return True, ''


def _git(*args, cwd, repo_root):
    command = [
        'git',
        '-c', f'safe.directory={repo_root}',
        '-c', f'safe.directory={cwd}',
        '-C', cwd,
        *args,
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    return result.returncode, (result.stdout or ''), (result.stderr or '')


def _resolve_gitdir(stub_path, repo_root, *, report_error=False):
    rc, gitdir_out, err = _git('rev-parse', '--git-dir', cwd=stub_path, repo_root=repo_root)
    if rc != 0:
        if report_error:
            print(f'  quiet_stub: rev-parse --git-dir failed: {err}')
        return None
    gitdir = gitdir_out.strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(stub_path, gitdir)
    return gitdir


def quiet_stub(stub_path, *, repo_root=None):
    """Silence ``git status`` in a ``--no-checkout`` stub without writing files.

    The sequence intentionally avoids ``git sparse-checkout init``. Its
    internal checkout can materialise tracked files before cone mode is turned
    off. NUL-delimited index input also avoids Windows newline conversion.
    """
    repo_root = repo_root or os.environ.get('CLAUDE_REPO_ROOT', os.getcwd())
    gitdir = _resolve_gitdir(stub_path, repo_root, report_error=True)
    if gitdir is None:
        return False

    rc, _, err = _git('read-tree', 'HEAD', cwd=stub_path, repo_root=repo_root)
    if rc != 0:
        print(f'  quiet_stub: read-tree HEAD failed: {err}')
        return False

    if _git('config', 'core.sparseCheckout', 'true', cwd=stub_path, repo_root=repo_root)[0] != 0:
        print('  quiet_stub: failed to set core.sparseCheckout')
        return False
    if _git('config', 'core.sparseCheckoutCone', 'false', cwd=stub_path, repo_root=repo_root)[0] != 0:
        print('  quiet_stub: failed to set core.sparseCheckoutCone')
        return False

    info_dir = os.path.join(gitdir, 'info')
    os.makedirs(info_dir, exist_ok=True)
    sparse_file = os.path.join(info_dir, 'sparse-checkout')
    temporary_file = sparse_file + '.tmp'
    with open(temporary_file, 'w', encoding='utf-8') as handle:
        handle.write('')
    os.replace(temporary_file, sparse_file)

    listed = subprocess.run(
        ['git', '-C', stub_path, 'ls-files', '-z'], capture_output=True, check=False,
    )
    if listed.returncode != 0:
        print(f'  quiet_stub: ls-files -z failed: {listed.stderr.decode(errors="replace")}')
        return False
    if not listed.stdout:
        return True

    updated = subprocess.run(
        ['git', '-C', stub_path, 'update-index', '--skip-worktree', '-z', '--stdin'],
        input=listed.stdout, capture_output=True, check=False,
    )
    if updated.returncode != 0:
        print(
            '  quiet_stub: update-index --skip-worktree failed: '
            f'{updated.stderr.decode(errors="replace")}'
        )
        return False
    if updated.stderr:
        message = updated.stderr.decode(errors='replace').strip()
        if message:
            print(f'  quiet_stub: update-index reported: {message}')
            return False

    rc, status_out, _ = _git(
        'status', '--porcelain', '--untracked-files=no', cwd=stub_path, repo_root=repo_root
    )
    if rc == 0 and status_out.strip():
        print(
            '  quiet_stub: status not clean after quiet: '
            f'{len(status_out.splitlines())} tracked entries remain'
        )
        return False
    return True


def stub_is_quieted(stub_path, *, repo_root=None):
    """Return whether a stub has the per-worktree quietness artefacts."""
    repo_root = repo_root or os.environ.get('CLAUDE_REPO_ROOT', os.getcwd())
    gitdir = _resolve_gitdir(stub_path, repo_root)
    if gitdir is None:
        return False
    if not os.path.isfile(os.path.join(gitdir, 'info', 'sparse-checkout')):
        return False
    if not os.path.isfile(os.path.join(gitdir, 'index')):
        return False
    rc, listed, _ = _git('ls-files', '-v', cwd=stub_path, repo_root=repo_root)
    if rc != 0:
        return False
    return any(line.startswith('S ') for line in listed.splitlines())
