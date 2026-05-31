"""
SessionStart hook: detect silent deletion or truncation of Claude transcript
files (~/.claude/projects/**/*.jsonl) across restarts and version updates.

Mirrors claude-transcript-watch.sh by AiTrillium
(anthropics/claude-code#62272, issuecomment-4584631435), reimplemented as
Python for native Windows support without Git Bash or WSL.

On each SessionStart:
  1. Scan ~/.claude/projects/**/*.jsonl — record sha256, size, mtime.
  2. Compare against the previous run's manifest at
     ~/.claude/transcript-manifests/latest.tsv.
  3. If any transcript disappeared or shrank, write a timestamped ALERT to
     watch.log in the state dir and print it to stderr.
  4. On no loss, write a single OK line to watch.log.
  5. Write the current manifest to latest.tsv.
  6. Exit 0 unconditionally — a hook that exits non-zero blocks every session.

Usage (SessionStart hook in .claude/settings.json):

  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python C:/absolute/path/to/tools/sessions/session_watch.py"
          }
        ]
      }
    ]
  }

Options:
  --test    Compare latest.tsv against current state and print what would be
            alerted, but do not update the manifest or write to watch.log.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECTS_DIR = Path.home() / ".claude" / "projects"
_STATE_DIR = Path.home() / ".claude" / "transcript-manifests"
_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
_MANIFEST_KEEP = 50


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _get_version() -> str:
    try:
        result = subprocess.run(
            ["claude", "--version"],
            timeout=3,
            capture_output=True,
            text=True,
        )
        lines = (result.stdout or "").strip().splitlines()
        return lines[0].strip() if lines else "unknown"
    except Exception:
        return "unknown"


def _get_cleanup_days(settings_path: Path) -> str:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        val = data.get("cleanupPeriodDays")
        return str(val) if val is not None else "(unset → 30)"
    except FileNotFoundError:
        # settings.json absent — default retention applies.
        return "(unset → 30)"
    except Exception:
        return "(unreadable)"


def _sha256(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return "?"


def _scan(projects_dir: Path) -> dict:
    """Return {relpath_str: {sha256, size, mtime}} for all *.jsonl files."""
    result = {}
    if not projects_dir.is_dir():
        return result
    for p in sorted(projects_dir.rglob("*.jsonl")):
        try:
            st = p.stat()
            relpath = str(p.relative_to(projects_dir))
            sha = _sha256(p)
            # TOCTOU guard: if hashing failed AND the file is now gone, the
            # file was deleted between stat() and read. Omit it from the
            # manifest so the next run correctly flags it as disappeared.
            if sha == "?" and not p.exists():
                continue
            result[relpath] = {
                "sha256": sha,
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
        except Exception:
            pass
    return result


def _load_manifest(path: Path) -> dict:
    """Load TSV manifest → {relpath: {sha256, size, mtime}}."""
    result = {}
    if not path.is_file():
        return result
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("\t", 3)
            if len(parts) == 4:
                sha, sz, mt, rel = parts
                result[rel] = {
                    "sha256": sha,
                    "size": int(sz),
                    "mtime": float(mt),
                }
    except Exception:
        pass
    return result


def _write_manifest(path: Path, current: dict) -> None:
    lines = []
    for relpath in sorted(current.keys()):
        info = current[relpath]
        lines.append(f"{info['sha256']}\t{info['size']}\t{info['mtime']}\t{relpath}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _rotate_manifests(state_dir: Path) -> None:
    manifests = sorted(state_dir.glob("manifest.*.tsv"))
    to_delete = manifests[:-_MANIFEST_KEEP] if len(manifests) > _MANIFEST_KEEP else []
    for old in to_delete:
        old.unlink(missing_ok=True)


def _format_alert(
    ts: str,
    cleanup_days: str,
    prev_version: str,
    current_version: str,
    disappeared: list,
    shrank: list,
    cur_count: int,
) -> str:
    lines = [
        f"=== ALERT {ts} — transcript loss detected ===",
        f"  cleanupPeriodDays (configured): {cleanup_days}",
        f"  version: {prev_version} → {current_version}",
        f"  transcript count: {cur_count}",
        f"  DISAPPEARED: {len(disappeared)}",
    ]
    for item in disappeared:
        lines.append(f"    - {item}")
    lines.append(f"  SHRANK: {len(shrank)}")
    for item in shrank:
        lines.append(f"    - {item}")
    lines.append("=== END ALERT ===")
    return "\n".join(lines)


def _run(
    projects_dir: Path,
    state_dir: Path,
    settings_path: Path,
    test_mode: bool,
) -> int:
    try:
        log_path = state_dir / "watch.log"
        latest_path = state_dir / "latest.tsv"
        latest_ver_path = state_dir / "latest.version"

        ts = _ts()
        current_version = _get_version()
        cleanup_days = _get_cleanup_days(settings_path)
        current = _scan(projects_dir)
        cur_count = len(current)

        is_first_run = not latest_path.is_file()
        prev = _load_manifest(latest_path)
        prev_version = (
            latest_ver_path.read_text(encoding="utf-8").strip()
            if latest_ver_path.is_file()
            else "unknown"
        )

        if is_first_run:
            # First run — establish baseline manifest, no ALERT.
            msg = (
                f"{ts} BASELINE count={cur_count} "
                f"version={current_version} cleanupPeriodDays={cleanup_days}"
            )
            sys.stderr.write(f"[session-watch] {msg}\n")
            if not test_mode:
                state_dir.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
        else:
            disappeared = []
            shrank = []
            for relpath, prev_info in prev.items():
                if relpath not in current:
                    disappeared.append(f"{relpath} (was {prev_info['size']}B)")
                elif current[relpath]["size"] < prev_info["size"]:
                    shrank.append(
                        f"{relpath} ({prev_info['size']}B → {current[relpath]['size']}B)"
                    )

            if disappeared or shrank:
                alert = _format_alert(
                    ts, cleanup_days, prev_version, current_version,
                    disappeared, shrank, cur_count,
                )
                sys.stderr.write(alert + "\n")
                if not test_mode:
                    state_dir.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(alert + "\n")
            else:
                msg = (
                    f"{ts} OK count={cur_count} "
                    f"version={current_version} cleanupPeriodDays={cleanup_days}"
                )
                if not test_mode:
                    state_dir.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(msg + "\n")

        if not test_mode:
            state_dir.mkdir(parents=True, exist_ok=True)
            # Nanosecond timestamp avoids filename collision when two sessions
            # start within the same second.
            epoch_ns = time.time_ns()
            timestamped = state_dir / f"manifest.{epoch_ns}.tsv"
            _write_manifest(timestamped, current)
            _write_manifest(latest_path, current)
            latest_ver_path.write_text(current_version, encoding="utf-8")
            _rotate_manifests(state_dir)

    except Exception as exc:
        sys.stderr.write(f"[session-watch] ERROR (non-fatal): {exc}\n")

    return 0


def main() -> int:
    # argparse calls sys.exit(2) on unknown arguments, raising SystemExit
    # (a BaseException subclass, not Exception). Catching SystemExit specifically
    # keeps the hook from blocking every session while still allowing
    # KeyboardInterrupt to propagate so Ctrl+C works during manual --test runs.
    try:
        parser = argparse.ArgumentParser(
            description="SessionStart hook: detect Claude transcript loss on each session start."
        )
        parser.add_argument(
            "--test",
            action="store_true",
            help="Print what would be alerted without updating manifest or watch.log.",
        )
        args = parser.parse_args()
    except SystemExit:
        return 0
    return _run(
        projects_dir=_PROJECTS_DIR,
        state_dir=_STATE_DIR,
        settings_path=_SETTINGS_PATH,
        test_mode=args.test,
    )


if __name__ == "__main__":
    sys.exit(main())
