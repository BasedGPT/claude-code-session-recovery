"""
worktree_shrink.py
==================
Shrink a git worktree from materialised (~45 MB) to a stub (~2 KB).

NOTE: This implements the maintainer's chosen worktree lifecycle. Not required
by Claude Code. Adopt only if this workflow matches what you want.

Behaviour is governed by the lifecycle policy in docs/worktree-lifecycle.md.
If behaviour drifts from the doc, the doc is canonical and this script has a bug.

Pipeline:
  1. Atomic-claim by renaming .shrink-when-safe -> .shrink-in-progress.<pid>.<ts>
  2. Re-verify on this processor (branch state, content, locks, Desktop, no-op rename)
  3. Write manifest BEFORE any destructive op
  4. Move folder -> .shrink-quarantine/<name>-<ts>/
  5. git worktree prune
  6. git worktree add --no-checkout <orig> <branch>
  7. Write .worktree-shrunk.txt sentinel
  8. Post-validation
  9. Manifest update transactionally after each phase

Usage:
  worktree_shrink.py <name>                       # dry-run (default)
  worktree_shrink.py <name> --apply               # shrink (branch must be --merged master)
  worktree_shrink.py <name> --apply --squash-merged
  worktree_shrink.py <name> --apply --allow-unmerged
  worktree_shrink.py --resume <manifest-path>     # resume failed shrink
  worktree_shrink.py --queue [--apply]            # process all .shrink-when-safe markers

Reproducible after partial failure via the manifest's phase_log; --resume picks
up at the next idempotent step.
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


# ----- Configuration ----------------------------------------------------------
# Set REPO_ROOT to the root of your git repository, or set the environment
# variable CLAUDE_REPO_ROOT before running.
# WORKTREES_DIR is where your Claude Code worktrees live.

REPO_ROOT = os.environ.get('CLAUDE_REPO_ROOT', os.getcwd())
WORKTREES_DIR = os.path.join(REPO_ROOT, '.claude', 'worktrees')
QUARANTINE_DIR = os.path.join(WORKTREES_DIR, '.shrink-quarantine')
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(TOOL_DIR, 'shrink-manifests')

MARKER_READY = '.shrink-when-safe'
MARKER_IN_PROGRESS_PREFIX = '.shrink-in-progress.'
SENTINEL_FILE = '.worktree-shrunk.txt'

# Untracked / ignored top-level paths matching these patterns are purged at
# shrink time. The branch retains nothing about them. Used when scanning
# `git status --porcelain --ignored`.
DISPOSABLE_DIRS = frozenset({
    'node_modules',
    '__pycache__',
    '.pytest_cache',
    '.next',
    'dist',
    'build',
    '.cache',
    '.coverage',
    'htmlcov',
    '.mypy_cache',
    '.ruff_cache',
    '.tox',
})

# Untracked / ignored top-level paths matching these patterns are moved with
# the folder to quarantine. Counts and sizes are recorded in the manifest.
PRESERVED_DIRS = frozenset({
    '.playwright-mcp',
    '.tmp_audit',
    '.transcript-index',
    '.dxt-sources',
    '.obsidian',
    '.agents',
})

MAX_SNAPSHOT_HASH_BYTES = 1_048_576


# ----- Helpers ---------------------------------------------------------------

def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def fs_ts():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')


def desktop_running():
    """True iff claude.exe is in the tasklist (case-insensitive)."""
    try:
        out = subprocess.check_output(
            ['tasklist', '/FI', 'IMAGENAME eq claude.exe', '/NH'],
            stderr=subprocess.DEVNULL, text=True
        )
        return 'claude.exe' in out.lower()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return True  # fail-safe


def git(*args, cwd=None, check=False):
    """Run a git command. Returns (returncode, stdout, stderr)."""
    if cwd is None:
        cwd = REPO_ROOT
    cmd = [
        'git',
        '-c', f'safe.directory={REPO_ROOT}',
        '-c', f'safe.directory={cwd}',
        '-C', cwd,
        *args,
    ]
    res = subprocess.run(
        cmd,
        capture_output=True, text=True, encoding='utf-8', errors='replace'
    )
    if check and res.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} failed: {res.stderr.strip()}')
    return res.returncode, (res.stdout or ''), (res.stderr or '')


def git_ok(*args, cwd=None):
    """True iff a git command exits successfully."""
    return git(*args, cwd=cwd)[0] == 0


def worktree_path(name):
    return os.path.join(WORKTREES_DIR, name)


def parse_worktree_list():
    """Return list of dicts: {path, head, branch, locked}."""
    rc, out, err = git('worktree', 'list', '--porcelain')
    if rc != 0:
        raise RuntimeError(f'git worktree list failed: {err}')
    items, cur = [], None
    for raw in out.splitlines():
        line = raw.rstrip()
        if line.startswith('worktree '):
            if cur:
                items.append(cur)
            cur = {'path': line[len('worktree '):], 'head': '', 'branch': '', 'locked': False}
        elif line.startswith('HEAD '):
            cur['head'] = line[len('HEAD '):]
        elif line.startswith('branch '):
            cur['branch'] = line[len('branch '):].removeprefix('refs/heads/')
        elif line == 'locked':
            cur['locked'] = True
    if cur:
        items.append(cur)
    return items


def find_worktree_entry(name):
    """Return the worktree-list entry for `name`, or None."""
    target = worktree_path(name).lower().replace('/', '\\').rstrip('\\')
    for w in parse_worktree_list():
        wp = w['path'].lower().replace('/', '\\').rstrip('\\')
        if wp == target:
            return w
    return None


def folder_size_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def path_components(rel_path):
    """All path components of a `git status` relative path, top to leaf."""
    rel = rel_path.replace('\\', '/').strip('/')
    return [p for p in rel.split('/') if p] if rel else []


def classify_path(components):
    """Return ('disposable', root) | ('preserved', root) | ('violation', None).
    `root` is the matched component name. Walks path top-to-leaf so that nested
    caches like `.claude/__pycache__/foo.pyc` match `__pycache__` even though the
    top-level component is `.claude`."""
    for c in components:
        if c in DISPOSABLE_DIRS:
            return ('disposable', c)
        if c in PRESERVED_DIRS:
            return ('preserved', c)
    return ('violation', None)


def is_allowlisted_path(rel_path):
    """True when any component is in either shrink allowlist."""
    kind, _ = classify_path(path_components(rel_path))
    return kind != 'violation'


def should_skip_snapshot_dir(rel_path, dirname):
    if dirname in DISPOSABLE_DIRS:
        return True
    rel = os.path.join(rel_path, dirname) if rel_path else dirname
    return classify_path(path_components(rel))[0] == 'disposable'


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def snapshot_tree(base_path):
    """Bounded content snapshot for post-move validation.

    Records relative path, type, size, and mtime for every non-disposable entry.
    Files up to 1 MB also get sha256. Disposable directories are skipped
    entirely so caches do not dominate cost or create noisy diffs.
    """
    entries = {}
    for root, dirs, files in os.walk(base_path):
        rel_root = os.path.relpath(root, base_path)
        rel_root = '' if rel_root == '.' else rel_root
        dirs[:] = [d for d in dirs if not should_skip_snapshot_dir(rel_root, d)]

        for d in dirs:
            path = os.path.join(root, d)
            rel = os.path.relpath(path, base_path).replace('\\', '/')
            try:
                st = os.stat(path)
            except OSError:
                continue
            entries[rel] = {
                'type': 'dir',
                'size': 0,
                'mtime_ns': st.st_mtime_ns,
            }

        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, base_path).replace('\\', '/')
            if classify_path(path_components(rel))[0] == 'disposable':
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            item = {
                'type': 'file',
                'size': st.st_size,
                'mtime_ns': st.st_mtime_ns,
            }
            if st.st_size <= MAX_SNAPSHOT_HASH_BYTES:
                try:
                    item['sha256'] = file_sha256(path)
                except OSError:
                    item['sha256_error'] = True
            entries[rel] = item
    return entries


def compare_snapshot(pre, post):
    problems = []
    pre_keys = set(pre)
    post_keys = set(post)
    for rel in sorted(pre_keys - post_keys)[:20]:
        problems.append(f'quarantine snapshot missing path: {rel}')
    added_unallowed = [rel for rel in sorted(post_keys - pre_keys) if not is_allowlisted_path(rel)]
    for rel in added_unallowed[:20]:
        problems.append(f'quarantine snapshot has unexpected new path: {rel}')
    for rel in sorted(pre_keys & post_keys):
        before = pre[rel]
        after = post[rel]
        keys = ['type', 'size', 'mtime_ns']
        if 'sha256' in before:
            keys.append('sha256')
        for key in keys:
            if before.get(key) != after.get(key):
                problems.append(f'quarantine snapshot changed {rel}: {key}')
                break
        if len(problems) >= 40:
            problems.append('quarantine snapshot diff truncated after 40 problems')
            break
    return problems


# ----- Content classification ------------------------------------------------

def classify_content(target_path):
    """Inspect `git status --porcelain --ignored -uall` for the worktree.

    Returns (disposable, preserved, violations, tracked_changes):
      disposable      -- list of top-level dirs to purge
      preserved       -- list of top-level dirs to keep (manifest-logged)
      violations      -- list of (status_code, rel_path) outside the allowlist
                         These BLOCK shrink unless --allow-unmerged is set.
      tracked_changes -- list of (status_code, rel_path) tracked-file mods
                         (M, D, A, R). These block shrink because uncommitted
                         tracked changes are not reproducible from the branch.
    """
    rc, out, err = git('status', '--porcelain=v1', '--ignored', '-uall', cwd=target_path)
    if rc != 0:
        raise RuntimeError(f'git status failed in {target_path}: {err}')

    disposable, preserved, violations, tracked_changes = set(), set(), [], []
    for line in out.splitlines():
        if not line:
            continue
        # Porcelain v1: first 2 cols are status, then space, then path.
        code = line[:2]
        rel = line[3:].strip().strip('"')
        comps = path_components(rel)
        if not comps:
            continue
        if code == '??' or code == '!!':
            kind, root = classify_path(comps)
            if kind == 'disposable':
                disposable.add(root)
            elif kind == 'preserved':
                preserved.add(root)
            else:
                violations.append((code, rel))
        else:
            tracked_changes.append((code, rel))
    return sorted(disposable), sorted(preserved), violations, tracked_changes


# ----- Branch mode checks ----------------------------------------------------

def branch_is_merged(branch):
    rc, out, _ = git('branch', '--merged', 'master', '--list', branch)
    if rc != 0:
        return False
    return any(line.strip().lstrip('* ').strip() == branch for line in out.splitlines() if line.strip())


def branch_squash_equivalent(branch):
    """`git cherry master <branch>` lists unique commits with '+'. Zero '+'
    lines means every commit is patch-equivalent on master (squash-merged)."""
    rc, out, _ = git('cherry', 'master', branch)
    if rc != 0:
        return False
    return not any(line.startswith('+') for line in out.splitlines())


def unmerged_commit_subjects(branch):
    """Subjects of commits in `branch` not on master. Stderr-logged when
    --allow-unmerged is in effect, so the user sees what they're abandoning."""
    rc, out, _ = git('log', '--oneline', f'master..{branch}')
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line]


# ----- Lock checks -----------------------------------------------------------

def noop_rename_test(path):
    """The actual safety primitive. Rename to a sibling then back; failure means
    a live process holds a handle. Catches every kind of lock-holder."""
    parent = os.path.dirname(path)
    probe = os.path.join(parent, os.path.basename(path) + '.shrink-probe')
    try:
        os.rename(path, probe)
        os.rename(probe, path)
        return True, ''
    except OSError as e:
        # Try to restore if first rename succeeded but second didn't.
        if os.path.exists(probe) and not os.path.exists(path):
            try:
                os.rename(probe, path)
            except OSError:
                pass
        return False, str(e)


# ----- Manifest --------------------------------------------------------------

def write_manifest(manifest_path, data):
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def read_manifest(manifest_path):
    with open(manifest_path, encoding='utf-8') as f:
        return json.load(f)


def append_phase(manifest_data, manifest_path, phase, **extra):
    entry = {'phase': phase, 'ts': iso_now()}
    entry.update(extra)
    manifest_data.setdefault('phase_log', []).append(entry)
    write_manifest(manifest_path, manifest_data)


# ----- Marker handling -------------------------------------------------------

def find_in_progress_marker(target_path):
    if not os.path.isdir(target_path):
        return None
    for entry in os.listdir(target_path):
        if entry.startswith(MARKER_IN_PROGRESS_PREFIX):
            return entry
    return None


def claim_marker(target_path, name, op_ts):
    """Atomic-claim the ready marker by renaming. Returns the new marker name,
    or None if nothing to claim. Raises if a foreign in-progress marker is
    already there (another processor's claim)."""
    ready = os.path.join(target_path, MARKER_READY)
    existing = find_in_progress_marker(target_path)
    if existing:
        raise RuntimeError(
            f'in-progress marker already present: {existing}. '
            f'Another processor may be running, or a previous shrink crashed. '
            f'Use --resume <manifest> to continue, or remove the marker after '
            f'verifying no process is alive.')
    if not os.path.exists(ready):
        return None
    new_name = f'{MARKER_IN_PROGRESS_PREFIX}{os.getpid()}.{op_ts}'
    new_path = os.path.join(target_path, new_name)
    os.rename(ready, new_path)  # atomic on same filesystem
    write_marker_payload(new_path, marker_payload(name, op_ts))
    return new_name


# ----- Shrink phases ---------------------------------------------------------

def run_verification(name, target_path, branch, branch_mode, apply_mode):
    """Execute every pre-move safety check. Returns (ok, reasons, classification).
    `classification` is the (disposable, preserved, violations, tracked) tuple
    from classify_content -- the move step reads it back to know what to log
    and what to purge."""
    reasons = []

    # Branch existence + mode check
    if not branch:
        reasons.append('branch is empty or detached')
    else:
        if branch_mode == 'merged':
            if not branch_is_merged(branch):
                reasons.append(f'branch {branch} is NOT merged into master '
                               f'(use --squash-merged or --allow-unmerged to override)')
        elif branch_mode == 'squash-merged':
            if not branch_squash_equivalent(branch):
                reasons.append(f'branch {branch} has commits not patch-equivalent on master')
        elif branch_mode == 'allow-unmerged':
            subs = unmerged_commit_subjects(branch)
            if subs:
                sys.stderr.write(f'WARNING: --allow-unmerged: {len(subs)} commits '
                                 f'on {branch} not on master. Branch ref preserves them:\n')
                for s in subs[:20]:
                    sys.stderr.write(f'  {s}\n')
                if len(subs) > 20:
                    sys.stderr.write(f'  ... and {len(subs) - 20} more\n')

    # Folder content check
    try:
        classification = classify_content(target_path)
    except RuntimeError as e:
        reasons.append(str(e))
        classification = ([], [], [], [])

    if classification[2]:  # violations
        sample = '; '.join(f'{c} {p}' for c, p in classification[2][:5])
        reasons.append(
            f'{len(classification[2])} untracked/ignored entries outside allowlist '
            f'(first 5: {sample}). Commit them, add to the allowlist, or remove manually.')
    if classification[3]:  # tracked changes
        sample = '; '.join(f'{c} {p}' for c, p in classification[3][:5])
        reasons.append(
            f'{len(classification[3])} uncommitted tracked changes '
            f'(first 5: {sample}). These are not recoverable from the branch; '
            f'commit, stash, or revert before shrink.')

    # CWD check
    try:
        cur = os.path.realpath(os.getcwd())
        tgt = os.path.realpath(target_path)
        if cur == tgt or cur.lower().startswith(tgt.lower() + os.sep):
            reasons.append(f'cannot shrink the worktree we are running inside: {tgt}')
    except OSError:
        pass

    # Desktop tasklist gate (UX, not safety)
    if apply_mode and desktop_running():
        reasons.append(
            'claude.exe is running -- the worktree may be open in a Desktop '
            'session. Quit Claude Desktop fully (window + tray) and re-run. '
            'Verify: tasklist /FI "IMAGENAME eq claude.exe"')

    # No-op rename test (the actual safety)
    if apply_mode:
        ok, err = noop_rename_test(target_path)
        if not ok:
            reasons.append(f'lock check failed -- some process holds the folder: {err}. '
                           f'Close any IDE/terminal/Desktop session pointing here and re-run.')

    return (not reasons), reasons, classification


def purge_disposable(target_path, disposable):
    """Recursively delete every directory matching a name in `disposable`. They
    don't travel to quarantine. Walks bottom-up so nested matches inside other
    matches don't error out."""
    purged = []
    for root, dirs, _ in os.walk(target_path, topdown=False):
        for d in dirs:
            if d in disposable:
                p = os.path.join(root, d)
                try:
                    shutil.rmtree(p, ignore_errors=False)
                    purged.append(os.path.relpath(p, target_path))
                except OSError as e:
                    sys.stderr.write(f'  WARNING: could not purge {p}: {e}\n')
    return purged


def measure_preserved(target_path, preserved):
    """Count and size every preserved dir anywhere in the tree, for the manifest."""
    result = []
    seen = set()
    for root, dirs, _ in os.walk(target_path):
        for d in dirs:
            if d not in preserved:
                continue
            p = os.path.join(root, d)
            key = os.path.relpath(p, target_path)
            if key in seen:
                continue
            seen.add(key)
            count = 0
            size = 0
            for r2, _, files in os.walk(p):
                count += len(files)
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(r2, f))
                    except OSError:
                        pass
            result.append({'name': key, 'count': count, 'size_bytes': size})
    return result


def write_sentinel(stub_path, manifest):
    """Drop a UX breadcrumb at the stub. Desktop's session picker may still
    point here -- this file explains where things went."""
    quarantine_path = os.path.relpath(manifest["quarantine_path"], stub_path)
    manifest_path = os.path.relpath(manifest["manifest_path"], stub_path)
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
    with open(os.path.join(stub_path, SENTINEL_FILE), 'w', encoding='utf-8') as f:
        f.write(text)


def validate_sentinel(stub_path):
    sentinel = os.path.join(stub_path, SENTINEL_FILE)
    if not os.path.isfile(sentinel):
        return False, 'sentinel missing'
    try:
        with open(sentinel, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        return False, f'sentinel unreadable: {e}'
    required = ('Operation ID:', 'Branch:', 'Quarantine:', 'Manifest:', 'Shrunk:')
    missing = [r for r in required if r not in text]
    if missing:
        return False, 'sentinel missing fields: ' + ', '.join(missing)
    return True, ''


def marker_payload(name, op_ts, manifest_path=''):
    return {
        'op_id': op_ts,
        'pid': os.getpid(),
        'worktree': name,
        'manifest_path': manifest_path,
        'claimed_at': iso_now(),
    }


def write_marker_payload(marker_path, payload):
    with open(marker_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)


def update_marker_manifest(target_path, claimed_marker, manifest_path):
    if not claimed_marker:
        return
    marker_path = os.path.join(target_path, claimed_marker)
    if not os.path.isfile(marker_path):
        return
    try:
        with open(marker_path, encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        payload = {}
    payload['manifest_path'] = manifest_path
    payload['updated_at'] = iso_now()
    write_marker_payload(marker_path, payload)


def post_validation(name, target_path, manifest, manifest_path):
    """Final checks after stub creation. Catches TOCTOU writes between verify
    and move, branch-ref movement during shrink, and sync-conflict races."""
    problems = []

    # 1. Quarantine intact? Folder exists, non-empty.
    q = manifest['quarantine_path']
    if not os.path.isdir(q):
        problems.append(f'quarantine missing: {q}')
    else:
        try:
            if not any(True for _ in os.scandir(q)):
                problems.append(f'quarantine is empty: {q}')
        except OSError as e:
            problems.append(f'quarantine unreadable: {e}')
    if os.path.isdir(q):
        pre_snapshot = manifest.get('verification_snapshot')
        if not pre_snapshot:
            problems.append('manifest missing verification_snapshot')
        else:
            problems.extend(compare_snapshot(pre_snapshot, snapshot_tree(q)))

    # 2. Stub resolves to manifest HEAD SHA. Detects branch-ref movement.
    if os.path.isdir(target_path):
        sentinel_ok, sentinel_problem = validate_sentinel(target_path)
        if not sentinel_ok:
            problems.append(f'sentinel invalid: {sentinel_problem}')
        rc, out, _ = git('rev-parse', 'HEAD', cwd=target_path)
        if rc == 0:
            head_now = out.strip()
            if head_now != manifest['head_sha']:
                problems.append(
                    f'stub HEAD ({head_now}) differs from manifest '
                    f'({manifest["head_sha"]}) -- branch ref moved during shrink')
        else:
            problems.append('stub does not resolve to a HEAD')
    else:
        problems.append(f'stub path missing: {target_path}')

    # 3. git worktree list shows the stub.
    entry = find_worktree_entry(name)
    if not entry:
        problems.append(f'worktree {name} not in git worktree list after shrink')

    # 3a. Stub quietness: sparse-checkout configured so `git status` is silent.
    if os.path.isdir(target_path) and not stub_is_quieted(target_path):
        problems.append('stub is not quieted: core.sparseCheckout/Cone not set as expected')

    # 4. Sync conflict files. Resilio/OneDrive/Sync.com create these when concurrent
    # writes race a move. Cheap to scan at both ends.
    for scope_path in (target_path, q):
        if not os.path.isdir(scope_path):
            continue
        for root, _, files in os.walk(scope_path):
            for f in files:
                lower = f.lower()
                if 'conflict' in lower and ('resilio' in lower or 'onedrive' in lower or 'sync' in lower):
                    problems.append(f'sync conflict file detected: {os.path.join(root, f)}')

    return problems


def manifest_sha_reachable(manifest, manifest_path):
    sha = manifest.get('head_sha', '')
    if not sha or not git_ok('cat-file', '-e', f'{sha}^{{commit}}'):
        manifest['failure_reason'] = 'manifest_sha_unreachable'
        write_manifest(manifest_path, manifest)
        return False
    return True


def stub_matches_manifest(target_path, manifest):
    if not os.path.isdir(target_path):
        return False
    sentinel_ok, _ = validate_sentinel(target_path)
    if not sentinel_ok:
        return False
    rc, out, _ = git('rev-parse', 'HEAD', cwd=target_path)
    return rc == 0 and out.strip() == manifest.get('head_sha')


def recreate_stub_at_manifest_sha(target_path, manifest, manifest_path):
    if not manifest_sha_reachable(manifest, manifest_path):
        print('  FAILED at stub-recreate: manifest SHA is no longer reachable')
        return False
    if os.path.exists(target_path):
        allowed = {'.git', SENTINEL_FILE}
        try:
            entries = set(os.listdir(target_path))
        except OSError as e:
            manifest['failure_reason'] = f'stub inspection failed before repair: {e}'
            write_manifest(manifest_path, manifest)
            print(f'  FAILED at stub-recreate: could not inspect existing stub: {e}')
            return False
        if not entries or any(e not in allowed for e in entries):
            manifest['failure_reason'] = 'stub_recreate_refused_existing_path_not_stub_shaped'
            write_manifest(manifest_path, manifest)
            print('  FAILED at stub-recreate: existing path is not stub-shaped')
            return False
        shutil.rmtree(target_path, ignore_errors=False)
    git('worktree', 'prune')
    rc, _, err = git('worktree', 'add', '--no-checkout', target_path, manifest['head_sha'])
    if rc != 0:
        manifest['failure_reason'] = f'stub recreation failed: {err}'
        write_manifest(manifest_path, manifest)
        print(f'  FAILED at stub-recreate: {err}')
        return False
    if not quiet_stub(target_path):
        manifest['failure_reason'] = 'stub recreation quiet step failed'
        write_manifest(manifest_path, manifest)
        print('  FAILED at stub-recreate: quiet_stub failed')
        return False
    return True


# ----- Stub quietness --------------------------------------------------------

def quiet_stub(stub_path):
    """Silence `git status` inside a --no-checkout stub by marking every index
    entry skip-worktree. Pure index manipulation -- no working-tree writes by
    git itself. Idempotent.

    Recipe:
      1. `git read-tree HEAD` (no `-u`) -- populate the worktree's index from
         HEAD. Newly-created `--no-checkout` stubs do not always have an index
         file; without one, `update-index --skip-worktree --stdin` has nothing
         to mark and `git status` falls back to a HEAD-vs-disk comparison.
      2. Set `core.sparseCheckout=true` and `core.sparseCheckoutCone=false`.
      3. Write an empty pattern file to `info/sparse-checkout`.
      4. Mark every index entry skip-worktree via `update-index --skip-worktree --stdin`.

    Why not `git sparse-checkout init`: that command runs `read-tree` with
    cone-mode defaults internally, materialising root-level files on disk
    before any cone-disable can take effect. Verified: git sparse-checkout
    init+set+disable left root files on disk.
    Read-tree WITHOUT `-u` is safe -- it only updates the index, not disk.
    """
    rc, gitdir_out, err = git('rev-parse', '--git-dir', cwd=stub_path)
    if rc != 0:
        print(f'  quiet_stub: rev-parse --git-dir failed: {err}')
        return False
    gitdir = gitdir_out.strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(stub_path, gitdir)

    # 1. Populate the index from HEAD without touching disk.
    rc, _, err = git('read-tree', 'HEAD', cwd=stub_path)
    if rc != 0:
        print(f'  quiet_stub: read-tree HEAD failed: {err}')
        return False

    # 2. Sparse-checkout config flags.
    if git('config', 'core.sparseCheckout', 'true', cwd=stub_path)[0] != 0:
        print('  quiet_stub: failed to set core.sparseCheckout')
        return False
    if git('config', 'core.sparseCheckoutCone', 'false', cwd=stub_path)[0] != 0:
        print('  quiet_stub: failed to set core.sparseCheckoutCone')
        return False

    # 3. Empty pattern file (no patterns = no files match = all skip-worktree).
    info_dir = os.path.join(gitdir, 'info')
    os.makedirs(info_dir, exist_ok=True)
    sparse_file = os.path.join(info_dir, 'sparse-checkout')
    tmp = sparse_file + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write('')
    os.replace(tmp, sparse_file)

    # 4. Mark every index entry skip-worktree.
    # Use -z (NUL-separated, binary) for both ls-files and update-index --stdin.
    # Windows text-mode subprocess pipes convert \n to \r\n in stdin, which
    # corrupts the paths git sees and triggers "Ignoring path" warnings. The
    # bash pipe (`ls-files | update-index --stdin`) works because it stays
    # binary; subprocess.run(input=text_str, text=True) does not.
    ls_proc = subprocess.run(
        ['git', '-C', stub_path, 'ls-files', '-z'],
        capture_output=True, check=False,
    )
    if ls_proc.returncode != 0:
        print(f'  quiet_stub: ls-files -z failed: {ls_proc.stderr.decode(errors="replace")}')
        return False
    if not ls_proc.stdout:
        # Empty HEAD tree: nothing to skip. Status is already clean.
        return True

    proc = subprocess.run(
        ['git', '-C', stub_path, 'update-index', '--skip-worktree', '-z', '--stdin'],
        input=ls_proc.stdout, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        print(f'  quiet_stub: update-index --skip-worktree failed: {proc.stderr.decode(errors="replace")}')
        return False
    if proc.stderr:
        msg = proc.stderr.decode(errors='replace').strip()
        if msg:
            print(f'  quiet_stub: update-index reported: {msg}')
            return False

    # Exclude untracked files from the cleanliness check -- the sentinel and
    # any other allowed dotfiles are legitimately on-disk-but-untracked.
    rc, status_out, _ = git('status', '--porcelain', '--untracked-files=no', cwd=stub_path)
    if rc == 0 and status_out.strip():
        print(f'  quiet_stub: status not clean after quiet: {len(status_out.splitlines())} tracked entries remain')
        return False
    return True


def stub_is_quieted(stub_path):
    """True if a stub has been quieted by quiet_stub. Checks the per-worktree
    artifacts -- not just `git config --get`, which would return values
    inherited from the main repo's config and misclassify every untouched
    worktree as already-quieted.

    Two artifacts must both be present in the worktree's gitdir:
      - info/sparse-checkout (any size, even empty)
      - index with at least one skip-worktree-flagged entry
    """
    rc, gitdir_out, _ = git('rev-parse', '--git-dir', cwd=stub_path)
    if rc != 0:
        return False
    gitdir = gitdir_out.strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(stub_path, gitdir)

    if not os.path.isfile(os.path.join(gitdir, 'info', 'sparse-checkout')):
        return False
    if not os.path.isfile(os.path.join(gitdir, 'index')):
        return False

    # `ls-files -v` prints S<path> for skip-worktree entries, H<path> for
    # normal. We just need to know one S exists.
    rc, ls_out, _ = git('ls-files', '-v', cwd=stub_path)
    if rc != 0:
        return False
    for line in ls_out.splitlines():
        if line.startswith('S '):
            return True
    return False


# ----- Main shrink flow ------------------------------------------------------

def shrink_one(name, apply_mode=False, branch_mode='merged'):
    """Drive a single worktree through verify -> move -> prune -> stub -> validate."""
    target_path = worktree_path(name)
    if not os.path.isdir(target_path):
        print(f'  REFUSED: {target_path} does not exist on disk')
        return False

    entry = find_worktree_entry(name)
    if not entry:
        print(f'  REFUSED: {name} is not in git worktree list')
        return False
    branch = entry['branch']
    head_sha = entry['head']

    print(f'\n=== {name} ===')
    print(f'  path:      {target_path}')
    print(f'  branch:    {branch}')
    print(f'  head:      {head_sha}')
    print(f'  mode:      {branch_mode}')

    pre_size = folder_size_bytes(target_path)
    print(f'  pre-size:  {pre_size:,} bytes ({pre_size / 1_048_576:.1f} MB)')

    op_ts = fs_ts()

    # Claim marker (or proceed without one when invoked by name)
    claimed_marker = None
    if apply_mode:
        try:
            claimed_marker = claim_marker(target_path, name, op_ts)
        except RuntimeError as e:
            print(f'  REFUSED: {e}')
            return False
        if claimed_marker:
            print(f'  claimed:   {claimed_marker}')

    # Verify
    ok, reasons, classification = run_verification(name, target_path, branch, branch_mode, apply_mode)
    disposable, preserved, violations, tracked = classification

    print(f'  disposable:{disposable if disposable else "(none)"}')
    print(f'  preserved: {preserved if preserved else "(none)"}')
    print(f'  outside-allowlist violations: {len(violations)}')
    print(f'  tracked-mods (blocking; not recoverable from branch): {len(tracked)}')
    if tracked[:5]:
        for code, rel in tracked[:5]:
            print(f'      {code} {rel}')
        if len(tracked) > 5:
            print(f'      ... and {len(tracked) - 5} more')

    if not ok:
        print('  REFUSED:')
        for r in reasons:
            print(f'    - {r}')
        # Roll the marker back so a future run can retry
        if claimed_marker:
            try:
                os.rename(os.path.join(target_path, claimed_marker),
                          os.path.join(target_path, MARKER_READY))
                print(f'  rolled back marker -> {MARKER_READY}')
            except OSError as e:
                print(f'  WARNING: could not roll back marker: {e}')
        return False

    if not apply_mode:
        print('  DRY-RUN: would shrink. Re-run with --apply to execute.')
        return True

    # Build manifest
    ts = op_ts
    quarantine_path = os.path.join(QUARANTINE_DIR, f'{name}-{ts}')
    manifest_backup_dir = os.path.join(BACKUP_DIR, f'worktree-shrink-{name}-{ts}')
    manifest_path = os.path.join(manifest_backup_dir, 'manifest.json')

    manifest = {
        'original_path': target_path,
        'operation_id': ts,
        'branch': branch,
        'head_sha': head_sha,
        'quarantine_path': quarantine_path,
        'manifest_path': manifest_path,
        'processor_pid': os.getpid(),
        'start_timestamp': iso_now(),
        'pre_shrink_size_bytes': pre_size,
        'branch_mode': branch_mode,
        'disposable_dirs_purged': [],
        'preserved_dirs': measure_preserved(target_path, preserved),
        'tracked_changes_blocking': [{'code': c, 'path': p} for c, p in tracked],
        'verification_snapshot': {},
        'unmerged_commits': unmerged_commit_subjects(branch) if branch_mode == 'allow-unmerged' else [],
        'phase_log': [],
        'success': False,
        'failure_reason': None,
    }

    append_phase(manifest, manifest_path, 'claimed')
    update_marker_manifest(target_path, claimed_marker, manifest_path)
    print(f'  manifest:  {manifest_path}')

    # Purge disposables (the only destructive op before move)
    if disposable:
        purged = purge_disposable(target_path, disposable)
        manifest['disposable_dirs_purged'] = purged
        write_manifest(manifest_path, manifest)
        print(f'  purged:    {purged}')

    # Verified phase (post-claim, post-disposable-purge, pre-move)
    manifest['verification_snapshot'] = snapshot_tree(target_path)
    write_manifest(manifest_path, manifest)
    append_phase(manifest, manifest_path, 'verified',
                 disposable=disposable, preserved=preserved,
                 snapshot_entries=len(manifest['verification_snapshot']))

    # Move
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    try:
        os.rename(target_path, quarantine_path)
    except OSError as e:
        manifest['failure_reason'] = f'move failed: {e}'
        append_phase(manifest, manifest_path, 'failed', reason=str(e))
        print(f'  FAILED at move: {e}')
        return False
    append_phase(manifest, manifest_path, 'moved')
    print(f'  moved ->   {quarantine_path}')

    # Prune
    rc, out, err = git('worktree', 'prune')
    if rc != 0:
        manifest['failure_reason'] = f'prune failed: {err}'
        append_phase(manifest, manifest_path, 'failed', reason=err)
        print(f'  FAILED at prune: {err}')
        return False
    append_phase(manifest, manifest_path, 'pruned')
    print('  pruned')

    # Stub
    rc, out, err = git('worktree', 'add', '--no-checkout', target_path, branch)
    if rc != 0:
        manifest['failure_reason'] = f'stub creation failed: {err}'
        append_phase(manifest, manifest_path, 'failed', reason=err)
        print(f'  FAILED at stub-create: {err}')
        return False
    append_phase(manifest, manifest_path, 'stub-created')
    print(f'  stub created at {target_path}')

    # Quiet the stub: skip-worktree every index entry so `git status` doesn't
    # report HEAD as a wall of staged deletions. See quiet_stub() docstring.
    if not quiet_stub(target_path):
        manifest['failure_reason'] = 'quiet_stub failed'
        append_phase(manifest, manifest_path, 'failed', reason='quiet_stub failed')
        print('  FAILED at quiet-stub')
        return False
    append_phase(manifest, manifest_path, 'stub-quieted')
    print('  stub quieted (skip-worktree applied)')

    # Sentinel
    write_sentinel(target_path, manifest)
    append_phase(manifest, manifest_path, 'sentinel-written')

    # Post-validation
    problems = post_validation(name, target_path, manifest, manifest_path)
    if problems:
        manifest['failure_reason'] = '; '.join(problems)
        append_phase(manifest, manifest_path, 'failed', reason=manifest['failure_reason'])
        print('  FAILED post-validation:')
        for p in problems:
            print(f'    - {p}')
        return False

    append_phase(manifest, manifest_path, 'verified-post')
    manifest['success'] = True
    write_manifest(manifest_path, manifest)

    post_size = folder_size_bytes(target_path)
    print(f'  post-size: {post_size:,} bytes ({post_size / 1024:.1f} KB)')
    print('  SUCCESS')
    return True


# ----- Resume flow -----------------------------------------------------------

def resume(manifest_path):
    """Pick up a partial shrink at the next idempotent phase, using the
    last-completed phase in the manifest's phase_log."""
    if not os.path.isfile(manifest_path):
        print(f'manifest not found: {manifest_path}')
        return False
    manifest = read_manifest(manifest_path)
    completed = {e['phase'] for e in manifest.get('phase_log', [])}
    print(f'Resuming shrink for {manifest["branch"]}')
    print(f'  original_path: {manifest["original_path"]}')
    print(f'  quarantine:    {manifest["quarantine_path"]}')
    print(f'  completed:     {sorted(completed)}')

    target_path = manifest['original_path']
    branch = manifest['branch']
    name = os.path.basename(target_path)

    # Move
    if 'moved' not in completed:
        if os.path.isdir(manifest['quarantine_path']) and not os.path.exists(target_path):
            append_phase(manifest, manifest_path, 'moved', recovered=True)
            completed.add('moved')
            print('  recovered moved state from quarantine.')
        elif not os.path.exists(target_path):
            manifest['failure_reason'] = 'original_path_missing_and_quarantine_missing'
            write_manifest(manifest_path, manifest)
            print('  ERROR: original_path missing and quarantine missing -- manual recovery needed')
            return False
        else:
            try:
                os.makedirs(os.path.dirname(manifest['quarantine_path']), exist_ok=True)
                os.rename(target_path, manifest['quarantine_path'])
            except OSError as e:
                manifest['failure_reason'] = f'move failed: {e}'
                write_manifest(manifest_path, manifest)
                print(f'  FAILED at move: {e}')
                return False
            append_phase(manifest, manifest_path, 'moved')
            completed.add('moved')
            print('  moved.')

    # Prune
    if 'pruned' not in completed:
        if find_worktree_entry(name) is None:
            append_phase(manifest, manifest_path, 'pruned', recovered=True)
            completed.add('pruned')
            print('  recovered pruned state from git worktree list.')
        else:
            rc, _, err = git('worktree', 'prune')
            if rc != 0:
                manifest['failure_reason'] = f'prune failed: {err}'
                write_manifest(manifest_path, manifest)
                print(f'  FAILED at prune: {err}')
                return False
            append_phase(manifest, manifest_path, 'pruned')
            completed.add('pruned')
            print('  pruned.')

    # Stub create
    if 'stub-created' not in completed:
        if os.path.isdir(target_path) and git_ok('rev-parse', 'HEAD', cwd=target_path):
            append_phase(manifest, manifest_path, 'stub-created', recovered=True)
            completed.add('stub-created')
            print('  recovered stub-created state from disk.')
        else:
            rc, _, err = git('worktree', 'add', '--no-checkout', target_path, branch)
            if rc != 0:
                manifest['failure_reason'] = f'stub creation failed: {err}'
                write_manifest(manifest_path, manifest)
                print(f'  FAILED at stub-create: {err}')
                return False
            append_phase(manifest, manifest_path, 'stub-created')
            completed.add('stub-created')
            print('  stub created.')

    if 'verified-post' not in completed and 'stub-created' in completed:
        if os.path.isdir(manifest['quarantine_path']) and not stub_matches_manifest(target_path, manifest):
            if not recreate_stub_at_manifest_sha(target_path, manifest, manifest_path):
                return False
            append_phase(manifest, manifest_path, 'stub-recreated',
                         reason='stub did not match manifest SHA or sentinel payload')
            print('  stub recreated at manifest SHA.')

    # Quiet the stub (idempotent; safe to call on already-quieted stubs).
    # Must run AFTER any stub-recreate above so we operate on a valid gitfile.
    if 'stub-quieted' not in completed and 'stub-created' in completed:
        if stub_is_quieted(target_path):
            append_phase(manifest, manifest_path, 'stub-quieted', recovered=True)
            completed.add('stub-quieted')
            print('  recovered stub-quieted state from disk.')
        else:
            if not quiet_stub(target_path):
                manifest['failure_reason'] = 'quiet_stub failed'
                write_manifest(manifest_path, manifest)
                print('  FAILED at quiet-stub')
                return False
            append_phase(manifest, manifest_path, 'stub-quieted')
            completed.add('stub-quieted')
            print('  stub quieted.')

    # Sentinel
    if 'sentinel-written' not in completed:
        sentinel_ok, _ = validate_sentinel(target_path)
        if sentinel_ok:
            append_phase(manifest, manifest_path, 'sentinel-written', recovered=True)
            completed.add('sentinel-written')
            print('  recovered sentinel-written state from disk.')
        else:
            write_sentinel(target_path, manifest)
            append_phase(manifest, manifest_path, 'sentinel-written')
            completed.add('sentinel-written')
            print('  sentinel written.')

    # Post-validation
    if 'verified-post' not in completed:
        problems = post_validation(name, target_path, manifest, manifest_path)
        if problems:
            manifest['failure_reason'] = '; '.join(problems)
            append_phase(manifest, manifest_path, 'failed', reason=manifest['failure_reason'])
            print('  FAILED post-validation:')
            for p in problems:
                print(f'    - {p}')
            return False
        append_phase(manifest, manifest_path, 'verified-post')

    manifest['success'] = True
    write_manifest(manifest_path, manifest)
    print('  RESUME SUCCESS')
    return True


# ----- Queue scan ------------------------------------------------------------

def find_ready_markers():
    """All worktree dirs containing .shrink-when-safe."""
    found = []
    if not os.path.isdir(WORKTREES_DIR):
        return found
    for entry in os.listdir(WORKTREES_DIR):
        d = os.path.join(WORKTREES_DIR, entry)
        if not os.path.isdir(d):
            continue
        if os.path.isfile(os.path.join(d, MARKER_READY)):
            found.append(entry)
    return found


# ----- CLI -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('name', nargs='?', help='Worktree name (basename) to shrink.')
    ap.add_argument('--apply', action='store_true', help='Execute. Default is dry-run.')
    ap.add_argument('--dry-run', action='store_true', help='Preview only (default).')
    ap.add_argument('--squash-merged', action='store_true',
                    help='Permit shrink if every commit is patch-equivalent on master.')
    ap.add_argument('--allow-unmerged', action='store_true',
                    help='Permit shrink even when commits are unique to the branch. '
                         'Branch ref preserves them.')
    ap.add_argument('--resume', metavar='MANIFEST',
                    help='Resume a failed shrink from a manifest path.')
    ap.add_argument('--queue', action='store_true',
                    help='Scan for .shrink-when-safe markers and process each.')
    args = ap.parse_args()

    if sum([bool(args.squash_merged), bool(args.allow_unmerged)]) > 1:
        ap.error('--squash-merged and --allow-unmerged are mutually exclusive')

    branch_mode = 'merged'
    if args.squash_merged:
        branch_mode = 'squash-merged'
    elif args.allow_unmerged:
        branch_mode = 'allow-unmerged'

    if args.resume:
        ok = resume(args.resume)
        sys.exit(0 if ok else 1)

    if args.queue:
        candidates = find_ready_markers()
        if not candidates:
            print('No .shrink-when-safe markers found.')
            sys.exit(0)
        print(f'Found {len(candidates)} marker(s): {candidates}')
        if not args.apply or args.dry_run:
            print('DRY-RUN: would attempt to shrink each. Re-run with --apply.')
            sys.exit(0)
        any_failed = False
        for name in candidates:
            try:
                ok = shrink_one(name, apply_mode=True, branch_mode=branch_mode)
                any_failed = any_failed or (not ok)
            except Exception as e:
                print(f'  EXCEPTION on {name}: {e}')
                any_failed = True
        sys.exit(1 if any_failed else 0)

    if not args.name:
        ap.error('name required unless --resume or --queue is used')

    ok = shrink_one(args.name, apply_mode=args.apply and not args.dry_run, branch_mode=branch_mode)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
