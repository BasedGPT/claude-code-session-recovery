"""Tests for _slug_encode in tools/diagnose.py.

Test cases derived from actual slugs observed on disk in ~/.claude/projects/.
Each (input_cwd, expected_slug) pair was confirmed by ls of the real projects dir.
"""
import os
import sys

# Import from tools/diagnose.py
_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
sys.path.insert(0, _TOOLS)
from diagnose import _slug_encode


# Observed on disk: plain Windows path
def test_plain_path():
    assert _slug_encode(r"C:\Users\Alice\Projects\MyProject") == "C--Users-Alice-Projects-MyProject"


# Observed on disk: path with spaces (space -> -)
def test_path_with_space():
    assert _slug_encode(r"C:\Users\Alice\Projects\My Project") == "C--Users-Alice-Projects-My-Project"


# Observed on disk: worktree path with dot-prefix directory (.claude -> -claude)
def test_worktree_path():
    assert (
        _slug_encode(r"C:\Users\Alice\Projects\MyProject\.claude\worktrees\admiring-lehmann-83ce0b")
        == "C--Users-Alice-Projects-MyProject--claude-worktrees-admiring-lehmann-83ce0b"
    )


# Bare user directory (C:\Users -> C--Users)
def test_bare_user_dir():
    assert _slug_encode(r"C:\Users") == "C--Users"


# Forward slashes treated identically to backslashes
def test_forward_slashes():
    assert _slug_encode("C:/Users/Alice/Projects/MyProject") == "C--Users-Alice-Projects-MyProject"


if __name__ == "__main__":
    import unittest
    # Simple self-runner without pytest
    tests = [
        test_plain_path, test_path_with_space, test_worktree_path,
        test_bare_user_dir, test_forward_slashes,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
