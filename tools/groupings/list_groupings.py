"""
List Claude Desktop session groupings.

Reads group definitions and session assignments from Claude Desktop's
Chromium Local Storage (LevelDB) and reports which sessions belong to
which group. This tool is read-only. It does not modify any files.

Files read:
  - Claude Desktop's platform-specific Local Storage/leveldb/*.ldb (SSTable data)
  - Claude Desktop's platform-specific Local Storage/leveldb/*.log (WAL data)

Files written:
  - Nothing. Read-only.

Exit codes:
  0  script ran successfully
  1  store not found, no readable files, dframe-store key absent, or deletion marker
  2  dframe-store value could not be parsed as JSON

Usage:
  python tools/groupings/list_groupings.py
  python tools/groupings/list_groupings.py --quiet
  python tools/groupings/list_groupings.py --limit 20
  python tools/groupings/list_groupings.py --state <fixture-state-path>

Output schema (load_groupings return value):
  groups:      list[{id: str, name: str}]
  assignments: dict[str, str]           session_key -> group_id
  group_order: dict[str, list[str]]     group_id -> [session_key, ...]
  skipped:     list[(filename, error)]  files that could not be read

Session key formats:
  "code:local_<uuid>"   worktree / local sessions
  "code:session_<id>"   cloud sessions
Group ID format: "cg-<uuid>"
"""

import argparse
import json
import os
import struct
import sys
from collections import Counter

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from platform_support import default_groupings_store  # noqa: E402

# Derived at module level from the environment; no I/O at import time.
_DEFAULT_STORE = default_groupings_store()

# Chromium's LevelDB magic differs from the upstream LevelDB magic.
# Pure-Python parsers using the upstream magic silently skip all Chromium files.
_CHROMIUM_MAGIC = b'\x57\xfb\x80\x8b\x24\x75\x47\xdb'

# Chromium Local Storage key format: <origin>\x00\x01<script_key>
# The origin is ASCII, not UTF-16LE.
_TARGET_ORIGIN = b'_https://claude.ai'
_DFRAME_STORE_KEY = b'dframe-store'
_LS_KEY_PREFIX = _TARGET_ORIGIN + b'\x00\x01'  # full prefix for exact matching

# LevelDB WAL log constants
_LOG_BLOCK_SIZE = 32768
_LOG_HEADER_SIZE = 7  # CRC(4) + length(2) + type(1)


# ---------------------------------------------------------------------------
# LevelDB SSTable parser
# ---------------------------------------------------------------------------

def _read_varint(data, pos):
    result = shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _snappy_decode(src):
    """Pure-Python Snappy decompressor (no external deps)."""
    out = bytearray()
    pos = 0
    _, pos = _read_varint(src, pos)  # uncompressed length (discard)
    while pos < len(src):
        tag = src[pos]; pos += 1
        typ = tag & 3
        if typ == 0:  # literal
            lbits = tag >> 2
            if lbits < 60:
                length = lbits + 1
            elif lbits == 60:
                length = src[pos] + 1; pos += 1
            elif lbits == 61:
                length = struct.unpack_from('<H', src, pos)[0] + 1; pos += 2
            elif lbits == 62:
                length = (struct.unpack_from('<H', src, pos)[0] | (src[pos + 2] << 16)) + 1; pos += 3
            else:
                length = struct.unpack_from('<I', src, pos)[0] + 1; pos += 4
            out.extend(src[pos:pos + length]); pos += length
        elif typ == 1:  # 1-byte offset copy
            length = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | src[pos]; pos += 1
            if not (1 <= offset <= len(out)):
                raise ValueError("invalid Snappy back-reference offset {}".format(offset))
            start = len(out) - offset
            for i in range(length):
                out.append(out[start + i % offset])
        elif typ == 2:  # 2-byte offset copy
            length = (tag >> 2) + 1
            offset = struct.unpack_from('<H', src, pos)[0]; pos += 2
            if not (1 <= offset <= len(out)):
                raise ValueError("invalid Snappy back-reference offset {}".format(offset))
            start = len(out) - offset
            for i in range(length):
                out.append(out[start + i % offset])
        elif typ == 3:  # 4-byte offset copy
            length = (tag >> 2) + 1
            offset = struct.unpack_from('<I', src, pos)[0]; pos += 4
            if not (1 <= offset <= len(out)):
                raise ValueError("invalid Snappy back-reference offset {}".format(offset))
            start = len(out) - offset
            for i in range(length):
                out.append(out[start + i % offset])
    return bytes(out)


def _read_block(data, offset, size):
    """Read one LevelDB block, Snappy-decompressing if the type byte is 0x01."""
    if offset < 0 or size < 0 or offset + size >= len(data):
        raise ValueError("block handle out of bounds (offset={}, size={}, file={})".format(
            offset, size, len(data)))
    raw = data[offset:offset + size]
    type_byte = data[offset + size]
    if type_byte == 1:
        return _snappy_decode(raw)
    return raw


def _parse_data_block(block_data):
    """Parse key-value pairs from a decompressed LevelDB data block.

    Returns [(raw_key_bytes, value_bytes), ...]. For data blocks the keys are
    InternalKeys (user_key + 8-byte seq/type suffix); for index blocks they
    are also InternalKeys but we only use the value (block handle) there.
    """
    if len(block_data) < 4:
        return []
    restart_count = struct.unpack_from('<I', block_data, len(block_data) - 4)[0]
    if restart_count > 100_000:
        return []
    data_end = len(block_data) - 4 - restart_count * 4
    if data_end < 0 or data_end > len(block_data):
        return []
    entries = []
    pos = 0
    last_key = b''
    while pos < data_end:
        try:
            shared, pos = _read_varint(block_data, pos)
            non_shared, pos = _read_varint(block_data, pos)
            val_len, pos = _read_varint(block_data, pos)
        except Exception:
            break
        if pos + non_shared + val_len > len(block_data):
            break
        key_delta = block_data[pos:pos + non_shared]; pos += non_shared
        value = block_data[pos:pos + val_len]; pos += val_len
        key = last_key[:shared] + key_delta
        last_key = key
        entries.append((bytes(key), bytes(value)))
    return entries


def _parse_ldb(data):
    """Parse a Chromium-format SSTable.

    Returns [(user_key, seq, ktype, value), ...] where:
      seq   -- LevelDB sequence number (higher = more recent write)
      ktype -- 1 = live value, 0 = deletion tombstone
    """
    result = []
    if len(data) < 48:
        return result
    footer = data[-48:]
    if footer[40:48] != _CHROMIUM_MAGIC:
        return result
    p = 0
    _, p = _read_varint(footer, p); _, p = _read_varint(footer, p)   # meta block handle
    io, p = _read_varint(footer, p); is_, p = _read_varint(footer, p)  # index block handle
    index_entries = _parse_data_block(_read_block(data, io, is_))
    for _sep_key, handle_bytes in index_entries:
        p2 = 0
        try:
            bo, p2 = _read_varint(handle_bytes, p2)
            bs, p2 = _read_varint(handle_bytes, p2)
            for ik, val in _parse_data_block(_read_block(data, bo, bs)):
                if len(ik) >= 8:
                    tag = struct.unpack_from('<Q', ik, len(ik) - 8)[0]
                    result.append((ik[:-8], tag >> 8, tag & 0xFF, val))
        except Exception:
            continue  # skip corrupt blocks; keep results from other blocks
    return result


# ---------------------------------------------------------------------------
# LevelDB WAL log parser
# ---------------------------------------------------------------------------

def _parse_log(data):
    """Parse a LevelDB WAL log file.

    Returns [(user_key, seq, ktype, value), ...] where seq and ktype have the
    same meaning as in _parse_ldb.  Log entries are not InternalKeys -- the key
    stored in the WriteBatch is the raw user key, and the sequence number comes
    from the batch header (incremented once per entry within the batch).
    """
    result = []
    pos = 0
    fragment = b''

    while pos + _LOG_HEADER_SIZE <= len(data):
        # If fewer than LOG_HEADER_SIZE bytes remain in the current 32 KB block,
        # the rest is padding -- advance to the next block.
        block_offset = pos % _LOG_BLOCK_SIZE
        remaining_in_block = _LOG_BLOCK_SIZE - block_offset
        if remaining_in_block < _LOG_HEADER_SIZE:
            pos += remaining_in_block
            fragment = b''
            continue

        length = struct.unpack_from('<H', data, pos + 4)[0]
        rtype = data[pos + 6]
        pos += _LOG_HEADER_SIZE

        if pos + length > len(data):
            break
        payload = data[pos:pos + length]
        pos += length

        if rtype == 0:    # kZeroType -- pre-allocated region, end of written data
            break
        elif rtype == 1:  # kFullType
            fragment = payload
        elif rtype == 2:  # kFirstType
            fragment = payload
            continue
        elif rtype == 3:  # kMiddleType
            fragment += payload
            continue
        elif rtype == 4:  # kLastType
            fragment += payload
        else:
            fragment = b''
            continue

        batch, fragment = fragment, b''
        if len(batch) < 12:
            continue

        try:
            seq = struct.unpack_from('<Q', batch, 0)[0]
            count = struct.unpack_from('<I', batch, 8)[0]
            p = 12
            for i in range(count):
                if p >= len(batch):
                    break
                ktype = batch[p]; p += 1
                klen, p = _read_varint(batch, p)
                if p + klen > len(batch):
                    break
                key = batch[p:p + klen]; p += klen
                if ktype == 1:  # kTypeValue
                    vlen, p = _read_varint(batch, p)
                    if p + vlen > len(batch):
                        break
                    val = batch[p:p + vlen]; p += vlen
                    result.append((key, seq + i, 1, val))
                else:  # kTypeDeletion
                    result.append((key, seq + i, 0, b''))
        except Exception:
            continue

    return result


# ---------------------------------------------------------------------------
# Chromium Local Storage value decoder
# ---------------------------------------------------------------------------

def _decode_ls_value(val):
    """Decode a Chromium v8-serialised Local Storage value to a Python string."""
    if not val:
        return ''
    pos = 0
    while pos < len(val) and val[pos] == 0xFF:
        pos += 1
    if pos >= len(val):
        return ''
    pos += 1  # version byte (0x01)
    if pos >= len(val):
        return ''
    tag = val[pos]; pos += 1
    if tag in (0x22, 0x27):  # one-byte Latin-1 string
        str_len, pos = _read_varint(val, pos)
        return val[pos:pos + str_len].decode('latin-1', errors='replace')
    if tag == 0x63:           # two-byte UTF-16LE string
        str_len, pos = _read_varint(val, pos)
        return val[pos:pos + str_len * 2].decode('utf-16-le', errors='replace')
    # tag byte is '{' (0x7b) -- raw UTF-8 JSON follows from the tag byte onward
    return val[pos - 1:].decode('utf-8', errors='replace')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_groupings(store_path=None):
    """
    Read session groupings from the Claude Desktop LevelDB store.

    Reads both .ldb (SSTable) and .log (WAL) files and picks the value with
    the highest LevelDB sequence number, so recent writes not yet compacted
    into an SSTable are included.

    Parameters
    ----------
    store_path : str or None
        Path to the LevelDB directory. Defaults to
        %APPDATA%\\Claude\\Local Storage\\leveldb\\

    Returns
    -------
    groups : list[dict]
        Each entry has keys ``id`` (str, "cg-<uuid>") and ``name`` (str).
    assignments : dict[str, str]
        Maps session_key -> group_id.
    group_order : dict[str, list[str]]
        Maps group_id -> [session_key, ...] in display order.
    skipped : list[tuple[str, str]]
        (filename, error_message) for files that could not be read.

    Raises
    ------
    FileNotFoundError
        The store directory does not exist.
    LookupError
        No readable .ldb or .log files found; dframe-store key absent; or
        the key exists only as a deletion tombstone (Desktop cleared groups).
    ValueError
        The dframe-store value was found but could not be parsed as JSON or
        has an unexpected schema.
    """
    if store_path is None:
        store_path = _DEFAULT_STORE

    if not os.path.isdir(store_path):
        raise FileNotFoundError(
            "LevelDB store not found: {}\n"
            "Is Claude Desktop installed and has it been run at least once?".format(store_path)
        )

    all_files = os.listdir(store_path)
    ldb_files = sorted(f for f in all_files if f.lower().endswith('.ldb'))
    log_files = sorted(f for f in all_files if f.lower().endswith('.log'))

    if not ldb_files and not log_files:
        raise LookupError(
            "No .ldb or .log files found in: {}\n"
            "Claude Desktop may not have written any Local Storage yet.".format(store_path)
        )

    # Track the highest-sequence-number entry seen for the dframe-store key.
    # Value: (seq, ktype, raw_val) -- ktype 0 = deletion, 1 = live value.
    best: tuple | None = None
    skipped = []

    for fname in ldb_files + log_files:
        fpath = os.path.join(store_path, fname)
        try:
            with open(fpath, 'rb') as fh:
                data = fh.read()
        except OSError as exc:
            # File locked or deleted mid-scan (Desktop compaction in progress).
            skipped.append((fname, str(exc)))
            continue

        try:
            if fname.lower().endswith('.ldb'):
                entries = _parse_ldb(data)
            else:
                entries = _parse_log(data)
        except Exception as exc:
            skipped.append((fname, "parse error: {}".format(exc)))
            continue

        for user_key, seq, ktype, val in entries:
            if not user_key.startswith(_LS_KEY_PREFIX):
                continue
            script_key = user_key[len(_LS_KEY_PREFIX):]
            if script_key != _DFRAME_STORE_KEY:
                continue
            if best is None or seq > best[0]:
                best = (seq, ktype, val)

    if best is None:
        detail = ""
        if skipped:
            detail = " ({} file(s) skipped due to read errors)".format(len(skipped))
        raise LookupError(
            "dframe-store key not found in any .ldb or .log file{}.\n"
            "Claude Desktop may not have any groups defined yet, or the\n"
            "Local Storage format is not recognised by this tool.".format(detail)
        )

    _, ktype, raw_val = best
    if ktype == 0:
        raise LookupError(
            "dframe-store deletion marker has the highest sequence number.\n"
            "Claude Desktop appears to have cleared all groupings."
        )

    val_str = _decode_ls_value(raw_val)
    try:
        obj = json.loads(val_str)
    except ValueError as exc:
        raise ValueError(
            "Could not parse dframe-store as JSON: {}".format(exc)
        ) from exc

    state = obj.get('state', obj)
    groups = state.get('customGroups', [])
    assignments = state.get('customGroupAssignments', {})
    group_order = state.get('customGroupOrder', {})

    if not isinstance(groups, list):
        raise ValueError("dframe-store schema unexpected: customGroups is not a list")
    if not isinstance(assignments, dict):
        raise ValueError("dframe-store schema unexpected: customGroupAssignments is not a dict")
    if not isinstance(group_order, dict):
        raise ValueError("dframe-store schema unexpected: customGroupOrder is not a dict")
    for i, g in enumerate(groups):
        if not isinstance(g, dict) or 'id' not in g or 'name' not in g:
            raise ValueError(
                "dframe-store schema unexpected: customGroups[{}] missing 'id' or 'name'".format(i)
            )

    return groups, assignments, group_order, skipped


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------

def _print_report(groups, assignments, group_order, skipped, quiet, limit):
    id_to_name = {g['id']: g['name'] for g in groups}
    count_by_group = Counter(assignments.values())
    ungrouped = sum(n for gid, n in count_by_group.items() if gid not in id_to_name)

    for fname, err in skipped:
        print("WARNING: skipped {} ({})".format(fname, err))
    if skipped:
        print()

    print("=== GROUPS ({}) ===".format(len(groups)))
    for g in groups:
        print("  {}  {!r}  ({} sessions)".format(g['id'], g['name'], count_by_group[g['id']]))

    if not quiet:
        print()
        print("=== SESSION ASSIGNMENTS ({}) ===".format(len(assignments)))
        for group in groups:
            gid = group['id']
            name = group['name']
            order = group_order.get(gid, [])
            ordered = [s for s in order if assignments.get(s) == gid]
            extras = sorted(s for s, g in assignments.items() if g == gid and s not in order)
            sessions = ordered + extras
            if not sessions:
                continue
            print()
            print("  [{}]".format(name))
            for sess in sessions[:limit]:
                print("    {}".format(sess))
            if len(sessions) > limit:
                print("    ... and {} more".format(len(sessions) - limit))

        unknown = {s: g for s, g in assignments.items() if g not in id_to_name}
        if unknown:
            print()
            print("  [UNKNOWN GROUP]")
            items = sorted(unknown.items())
            for sess, gid in items[:limit]:
                print("    {}  -> {}".format(sess, gid))
            if len(unknown) > limit:
                print("  ... and {} more".format(len(unknown) - limit))

    print()
    print("=== SUMMARY ===")
    for g in groups:
        print("  {!r}: {}".format(g['name'], count_by_group[g['id']]))
    print("  (ungrouped/unknown): {}".format(ungrouped))
    print("  Total assigned: {}".format(len(assignments)))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    def _positive_int(value):
        try:
            n = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("{!r} is not an integer".format(value))
        if n < 1:
            raise argparse.ArgumentTypeError("--limit must be >= 1")
        return n

    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0].strip())
    ap.add_argument("--quiet", action="store_true",
                    help="Summary only; skip per-session detail.")
    ap.add_argument("--limit", type=_positive_int, default=30,
                    help="Max sessions shown per group (default: 30).")
    ap.add_argument("--state", metavar="PATH", default=None,
                    help="Fixture state directory for testing.")
    args = ap.parse_args()

    if args.state:
        state_abs = os.path.abspath(args.state)
        store_path = os.path.join(
            state_abs, "appdata", "Claude", "Local Storage", "leveldb"
        )
    else:
        store_path = _DEFAULT_STORE

    try:
        groups, assignments, group_order, skipped = load_groupings(store_path)
    except FileNotFoundError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1
    except LookupError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2

    _print_report(groups, assignments, group_order, skipped, args.quiet, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
