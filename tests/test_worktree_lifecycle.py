"""Focused tests for shared worktree lifecycle contracts."""

import json
import os
import subprocess
import sys

import pytest


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'tools')
WORKTREES = os.path.join(TOOLS, 'worktrees')
sys.path.insert(0, WORKTREES)

import backfill_recovery_stubs
import worktree_lifecycle
import worktree_resume_rule
import worktree_shrink


def _git(cwd, *args):
    return subprocess.run(
        ['git', '-C', str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding='utf-8',
    )


def _make_no_checkout_stub(tmp_path):
    repo = tmp_path / 'repo'
    stub = tmp_path / 'stub'
    repo.mkdir()
    _git(repo, 'init')
    _git(repo, 'config', 'user.name', 'Lifecycle Test')
    _git(repo, 'config', 'user.email', 'lifecycle-test@example.invalid')
    (repo / 'tracked.txt').write_text('tracked\n', encoding='utf-8')
    (repo / 'nested').mkdir()
    (repo / 'nested' / 'unicode-ø.txt').write_text('nested\n', encoding='utf-8')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'fixture')
    _git(repo, 'branch', 'stub-branch')
    _git(repo, 'worktree', 'add', '--no-checkout', str(stub), 'stub-branch')
    return repo, stub


def test_claim_marker_writes_inspectable_payload_and_refuses_competitor(tmp_path):
    worktree = tmp_path / 'queued'
    worktree.mkdir()
    (worktree / worktree_lifecycle.MARKER_READY).write_text('', encoding='utf-8')

    assert worktree_shrink.claim_marker is worktree_lifecycle.claim_marker
    assert worktree_shrink.update_marker_manifest is worktree_lifecycle.update_marker_manifest
    claimed = worktree_lifecycle.claim_marker(str(worktree), 'queued', '20260725T010203')

    assert claimed.startswith(worktree_lifecycle.MARKER_IN_PROGRESS_PREFIX)
    assert not (worktree / worktree_lifecycle.MARKER_READY).exists()
    assert worktree_lifecycle.find_in_progress_marker(str(worktree)) == claimed
    payload = json.loads((worktree / claimed).read_text(encoding='utf-8'))
    assert payload['worktree'] == 'queued'
    assert payload['op_id'] == '20260725T010203'
    worktree_lifecycle.update_marker_manifest(str(worktree), claimed, 'manifests/one.json')
    assert json.loads((worktree / claimed).read_text(encoding='utf-8'))['manifest_path'] == 'manifests/one.json'

    with pytest.raises(RuntimeError, match='in-progress marker already present'):
        worktree_lifecycle.claim_marker(str(worktree), 'queued', '20260725T010204')


def test_sentinel_keeps_recovery_paths_relative_and_legacy_shrink_api(tmp_path):
    stub = tmp_path / 'stub'
    stub.mkdir()
    manifest = {
        'operation_id': 'test-operation',
        'branch': 'test-branch',
        'quarantine_path': str(tmp_path / 'private-root' / 'quarantine'),
        'manifest_path': str(tmp_path / 'private-root' / 'manifest.json'),
        'start_timestamp': '2026-07-25T00:00:00+00:00',
        'head_sha': 'a' * 40,
    }

    assert worktree_shrink.write_sentinel is worktree_lifecycle.write_sentinel
    worktree_lifecycle.write_sentinel(str(stub), manifest)

    content = (stub / worktree_lifecycle.SENTINEL_FILE).read_text(encoding='utf-8')
    assert str(tmp_path) not in content
    assert os.path.relpath(manifest['quarantine_path'], stub) in content
    assert os.path.relpath(manifest['manifest_path'], stub) in content
    assert worktree_lifecycle.validate_sentinel(str(stub)) == (True, '')


def test_resume_rule_clears_the_shared_ready_marker(tmp_path, monkeypatch, capsys):
    (tmp_path / worktree_lifecycle.MARKER_READY).write_text('', encoding='utf-8')
    monkeypatch.chdir(tmp_path)

    assert worktree_resume_rule.main() == 0

    assert not (tmp_path / worktree_lifecycle.MARKER_READY).exists()
    assert '[worktree-resume-rule] Removed .shrink-when-safe' in capsys.readouterr().err


def test_quiet_stub_is_clean_and_backfill_uses_shared_interface(tmp_path):
    repo, stub = _make_no_checkout_stub(tmp_path)

    assert backfill_recovery_stubs.quiet_stub is worktree_lifecycle.quiet_stub
    assert backfill_recovery_stubs.stub_is_quieted is worktree_lifecycle.stub_is_quieted
    assert worktree_lifecycle.quiet_stub(str(stub), repo_root=str(repo))

    status = _git(stub, 'status', '--porcelain', '--untracked-files=no').stdout
    assert status == ''
    assert worktree_lifecycle.stub_is_quieted(str(stub), repo_root=str(repo))
    assert not (stub / 'tracked.txt').exists()
    assert not (stub / 'nested').exists()
