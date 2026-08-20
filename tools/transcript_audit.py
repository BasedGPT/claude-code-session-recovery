"""Bounded, read-only structural inspection of JSONL transcripts.

This module intentionally does not expose transcript records, paths, titles, or
other user content in its result.  The two audit commands use it as a common
parser so their limits, anomaly counts, and graph facts cannot drift apart.
"""

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
import json
import os


SCHEMA_VERSION = "transcript-integrity-audit-v1"
DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_LINES_PER_FILE = 2_000_000
DEFAULT_MAX_NODES_PER_FILE = 1_000_000
_READ_CHUNK_BYTES = 64 * 1024


class AuditConfigurationError(ValueError):
    """Raised when an audit limit or input option is invalid."""


@dataclass(frozen=True)
class BoundedLine:
    """A bounded line prefix and facts collected while consuming its remainder."""

    prefix: bytes
    byte_count: int
    truncated: bool
    has_nul: bool
    nul_count: int


@dataclass
class BoundedReadState:
    """Mutable termination facts for one bounded binary scan."""

    bytes_read: int = 0
    lines_yielded: int = 0
    reached_eof: bool = False
    byte_budget_exhausted: bool = False
    line_budget_exhausted: bool = False


def _positive_limit(value, name):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise AuditConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise AuditConfigurationError(f"{name} must be a positive integer")
    return value


def iter_bounded_binary_lines(
    handle,
    max_line_bytes=None,
    *,
    max_total_bytes=None,
    max_lines=None,
    state=None,
):
    """Yield :class:`BoundedLine` values without retaining oversized lines.

    At most ``max_total_bytes`` and ``max_lines`` are consumed.  A line may be
    drained only while both budgets remain, so a newline-free or growing input
    cannot make the iterator scan indefinitely.  The caller-owned ``state``
    records which termination boundary fired without exposing input content.
    """
    if max_line_bytes is None:
        max_line_bytes = DEFAULT_MAX_LINE_BYTES
    if max_total_bytes is None:
        max_total_bytes = DEFAULT_MAX_FILE_BYTES
    if max_lines is None:
        max_lines = DEFAULT_MAX_LINES_PER_FILE
    max_line_bytes = _positive_limit(max_line_bytes, "max_line_bytes")
    max_total_bytes = _positive_limit(max_total_bytes, "max_total_bytes")
    max_lines = _positive_limit(max_lines, "max_lines")
    if state is None:
        state = BoundedReadState()
    retained = bytearray()
    byte_count = 0
    nul_count = 0
    truncated = False
    saw_line_bytes = False

    while True:
        if state.lines_yielded >= max_lines:
            state.line_budget_exhausted = True
            return
        remaining_bytes = max_total_bytes - state.bytes_read
        if remaining_bytes <= 0:
            state.byte_budget_exhausted = True
            if saw_line_bytes:
                yield BoundedLine(
                    bytes(retained), byte_count, True, bool(nul_count), nul_count
                )
                state.lines_yielded += 1
            return
        chunk = handle.read(min(_READ_CHUNK_BYTES, remaining_bytes))
        if not chunk:
            state.reached_eof = True
            if saw_line_bytes:
                yield BoundedLine(
                    bytes(retained), byte_count, truncated, bool(nul_count), nul_count
                )
                state.lines_yielded += 1
            return
        state.bytes_read += len(chunk)

        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = newline + 1 if newline >= 0 else len(chunk)
            segment = chunk[start:end]
            saw_line_bytes = True
            byte_count += len(segment)
            nul_count += segment.count(b"\x00")

            remaining = max_line_bytes - len(retained)
            if remaining > 0:
                retained.extend(segment[:remaining])
            if len(segment) > max(remaining, 0):
                truncated = True

            if newline < 0:
                break

            yield BoundedLine(
                bytes(retained), byte_count, truncated, bool(nul_count), nul_count
            )
            state.lines_yielded += 1
            retained.clear()
            byte_count = 0
            nul_count = 0
            truncated = False
            saw_line_bytes = False
            start = end
            if state.lines_yielded >= max_lines:
                state.line_budget_exhausted = True
                return

        if state.bytes_read >= max_total_bytes:
            state.byte_budget_exhausted = True
            if saw_line_bytes:
                yield BoundedLine(
                    bytes(retained), byte_count, True, bool(nul_count), nul_count
                )
                state.lines_yielded += 1
            return


def _empty_file_stats(path, reference):
    return {
        "reference": reference,
        "path": path,
        "state": "missing",
        "bytes": 0,
        "physical_lines": 0,
        "blank_lines": 0,
        "invalid_utf8_lines": 0,
        "nul_bytes": 0,
        "nul_lines": 0,
        "malformed_json": 0,
        "non_object_json": 0,
        "duplicate_uuid_values": 0,
        "duplicate_message_id_values": 0,
        "explicit_roots": 0,
        "missing_parent_references": 0,
        "parent_reference_count": 0,
        "parent_references_retained": 0,
        "parent_references_truncated": 0,
        "fork_points": 0,
        "leaves": 0,
        "weak_components": 0,
        "reachable_from_explicit_roots": 0,
        "unrooted_nodes": 0,
        "cycle_count": 0,
        "cycle_node_count": 0,
        "node_count": 0,
        "bounded": False,
        "partial": False,
        "bytes_read": 0,
        "errors": [],
    }


def _cycle_facts(nodes, children):
    """Return cyclic SCC facts using iterative Kosaraju traversal."""
    ordered_nodes = sorted(nodes)
    visited = set()
    finish_order = []

    for root in ordered_nodes:
        if root in visited:
            continue
        visited.add(root)
        stack = [(root, iter(sorted(children.get(root, ()))))]
        while stack:
            node, child_iterator = stack[-1]
            try:
                child = next(child_iterator)
            except StopIteration:
                stack.pop()
                finish_order.append(node)
                continue
            if child in nodes and child not in visited:
                visited.add(child)
                stack.append((child, iter(sorted(children.get(child, ())))))

    reverse_children = defaultdict(set)
    for parent in ordered_nodes:
        for child in children.get(parent, ()):
            if child in nodes:
                reverse_children[child].add(parent)

    assigned = set()
    cycle_components = 0
    cycle_node_count = 0
    for root in reversed(finish_order):
        if root in assigned:
            continue
        component = []
        stack = [root]
        assigned.add(root)
        while stack:
            node = stack.pop()
            component.append(node)
            for parent in sorted(reverse_children.get(node, ()), reverse=True):
                if parent not in assigned:
                    assigned.add(parent)
                    stack.append(parent)
        self_cycle = len(component) == 1 and root in children.get(root, ())
        if len(component) > 1 or self_cycle:
            cycle_components += 1
            cycle_node_count += len(component)
    return cycle_components, cycle_node_count


def _graph_facts(
    uuid_values,
    parent_edges,
    explicit_root_values,
    *,
    graph_bounded=False,
    parent_reference_count=0,
    parent_references_truncated=0,
):
    nodes = set(uuid_values)
    children = defaultdict(set)
    roots = set(explicit_root_values) & nodes
    missing_parent_occurrences = 0
    for parent, child in parent_edges:
        children[parent].add(child)
        if parent not in nodes:
            missing_parent_occurrences += 1

    node_children = {node: children.get(node, set()) for node in nodes}
    fork_points = sum(1 for node in nodes if len(node_children[node]) > 1)
    leaves = sum(1 for node in nodes if not node_children[node])

    adjacency = defaultdict(set)
    for parent, child_values in children.items():
        if parent not in nodes:
            continue
        for child in child_values:
            if child in nodes:
                adjacency[parent].add(child)
                adjacency[child].add(parent)
    components = 0
    visited = set()
    for node in sorted(nodes):
        if node in visited:
            continue
        components += 1
        queue = deque([node])
        visited.add(node)
        while queue:
            current = queue.popleft()
            for neighbour in sorted(adjacency.get(current, ())):
                if neighbour not in visited:
                    visited.add(neighbour)
                    queue.append(neighbour)

    reachable = set()
    queue = deque(sorted(roots))
    while queue:
        node = queue.popleft()
        if node in reachable:
            continue
        reachable.add(node)
        queue.extend(sorted(node_children.get(node, ())))

    cycle_count, cycle_node_count = _cycle_facts(nodes, node_children)
    return {
        "duplicate_uuid_values": sum(1 for count in uuid_values.values() if count > 1),
        "duplicate_message_id_values": 0,
        "explicit_roots": len(roots),
        "missing_parent_references": missing_parent_occurrences,
        "parent_reference_count": parent_reference_count,
        "parent_references_retained": len(parent_edges),
        "parent_references_truncated": parent_references_truncated,
        "fork_points": fork_points,
        "leaves": leaves,
        "weak_components": components,
        "reachable_from_explicit_roots": len(reachable),
        "unrooted_nodes": len(nodes - reachable),
        "cycle_count": cycle_count,
        "cycle_node_count": cycle_node_count,
        "node_count": len(nodes),
        "bounded": graph_bounded,
    }


def audit_transcript_file(
    path,
    *,
    reference="transcript-0001",
    max_line_bytes=None,
    max_file_bytes=None,
    max_lines_per_file=None,
    max_nodes_per_file=None,
):
    """Audit one path without exposing record content."""
    if max_line_bytes is None:
        max_line_bytes = DEFAULT_MAX_LINE_BYTES
    if max_file_bytes is None:
        max_file_bytes = DEFAULT_MAX_FILE_BYTES
    if max_lines_per_file is None:
        max_lines_per_file = DEFAULT_MAX_LINES_PER_FILE
    if max_nodes_per_file is None:
        max_nodes_per_file = DEFAULT_MAX_NODES_PER_FILE
    max_line_bytes = _positive_limit(max_line_bytes, "max_line_bytes")
    max_file_bytes = _positive_limit(max_file_bytes, "max_file_bytes")
    max_lines_per_file = _positive_limit(
        max_lines_per_file, "max_lines_per_file"
    )
    max_nodes_per_file = _positive_limit(max_nodes_per_file, "max_nodes_per_file")
    path = os.path.abspath(os.fspath(path))
    stats = _empty_file_stats(path, reference)

    if not os.path.exists(path):
        stats["errors"] = ["missing"]
        return stats
    if not os.path.isfile(path):
        stats["state"] = "unreadable"
        stats["errors"] = ["not_a_file"]
        return stats

    stats["state"] = "present"
    try:
        stats["bytes"] = os.path.getsize(path)
    except OSError:
        stats["state"] = "unreadable"
        stats["errors"] = ["stat_failed"]
        return stats
    if stats["bytes"] == 0:
        stats["state"] = "empty"
        return stats
    if stats["bytes"] > max_file_bytes:
        stats["bounded"] = True
        stats["partial"] = True
        stats["errors"] = ["byte_budget_exceeded"]
        return stats

    uuid_values = Counter()
    message_values = Counter()
    parent_edges = []
    explicit_root_values = set()
    graph_bounded = False
    parent_reference_count = 0
    parent_references_truncated = 0
    read_state = BoundedReadState()
    try:
        with open(path, "rb") as handle:
            for bounded_line in iter_bounded_binary_lines(
                handle,
                max_line_bytes,
                max_total_bytes=max_file_bytes,
                max_lines=max_lines_per_file,
                state=read_state,
            ):
                stats["physical_lines"] += 1
                if bounded_line.truncated:
                    graph_bounded = True
                    stats["bounded"] = True
                if bounded_line.has_nul:
                    stats["nul_lines"] += 1
                    stats["nul_bytes"] += bounded_line.nul_count
                if not bounded_line.prefix.strip():
                    stats["blank_lines"] += 1
                    continue
                if bounded_line.truncated:
                    continue
                try:
                    text = bounded_line.prefix.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    stats["invalid_utf8_lines"] += 1
                    continue
                try:
                    record = json.loads(text)
                except (ValueError, RecursionError):
                    stats["malformed_json"] += 1
                    continue
                if not isinstance(record, dict):
                    stats["non_object_json"] += 1
                    continue

                uuid_value = record.get("uuid")
                if isinstance(uuid_value, str) and uuid_value:
                    retained_uuid = (
                        uuid_value in uuid_values
                        or len(uuid_values) < max_nodes_per_file
                    )
                    if retained_uuid:
                        uuid_values[uuid_value] += 1
                        parent_value = record.get("parentUuid")
                        if isinstance(parent_value, str) and parent_value:
                            parent_reference_count += 1
                            if len(parent_edges) < max_nodes_per_file:
                                parent_edges.append((parent_value, uuid_value))
                            else:
                                parent_references_truncated += 1
                                graph_bounded = True
                                stats["bounded"] = True
                        else:
                            explicit_root_values.add(uuid_value)
                    else:
                        parent_value = record.get("parentUuid")
                        if isinstance(parent_value, str) and parent_value:
                            parent_reference_count += 1
                            parent_references_truncated += 1
                        graph_bounded = True
                        stats["bounded"] = True

                message_value = record.get("messageId")
                if isinstance(message_value, str) and message_value:
                    if (
                        message_value in message_values
                        or len(message_values) < max_nodes_per_file
                    ):
                        message_values[message_value] += 1
                    else:
                        graph_bounded = True
                        stats["bounded"] = True
    except (OSError, ValueError):
        stats["state"] = "unreadable"
        stats["errors"] = ["read_failed"]
        return stats
    stats["bytes_read"] = read_state.bytes_read
    if read_state.byte_budget_exhausted:
        stats["bounded"] = True
        stats["partial"] = True
        stats["errors"].append("byte_budget_exceeded")
    if read_state.line_budget_exhausted:
        stats["bounded"] = True
        stats["partial"] = True
        stats["errors"].append("line_budget_exceeded")

    graph = _graph_facts(
        uuid_values,
        parent_edges,
        explicit_root_values,
        graph_bounded=graph_bounded,
        parent_reference_count=parent_reference_count,
        parent_references_truncated=parent_references_truncated,
    )
    graph["duplicate_message_id_values"] = sum(
        1 for count in message_values.values() if count > 1
    )
    stats.update(graph)
    return stats


def audit_transcript_paths(
    paths,
    *,
    max_line_bytes=None,
    max_file_bytes=None,
    max_lines_per_file=None,
    max_nodes_per_file=None,
):
    """Audit sorted, de-duplicated paths and return a JSON-safe result."""
    if max_line_bytes is None:
        max_line_bytes = DEFAULT_MAX_LINE_BYTES
    if max_file_bytes is None:
        max_file_bytes = DEFAULT_MAX_FILE_BYTES
    if max_lines_per_file is None:
        max_lines_per_file = DEFAULT_MAX_LINES_PER_FILE
    if max_nodes_per_file is None:
        max_nodes_per_file = DEFAULT_MAX_NODES_PER_FILE
    max_line_bytes = _positive_limit(max_line_bytes, "max_line_bytes")
    max_file_bytes = _positive_limit(max_file_bytes, "max_file_bytes")
    max_lines_per_file = _positive_limit(
        max_lines_per_file, "max_lines_per_file"
    )
    max_nodes_per_file = _positive_limit(max_nodes_per_file, "max_nodes_per_file")
    unique_paths = sorted({os.path.abspath(os.fspath(path)) for path in paths})
    files = [
        audit_transcript_file(
            path,
            reference=f"transcript-{index:04d}",
            max_line_bytes=max_line_bytes,
            max_file_bytes=max_file_bytes,
            max_lines_per_file=max_lines_per_file,
            max_nodes_per_file=max_nodes_per_file,
        )
        for index, path in enumerate(unique_paths, start=1)
    ]

    sum_fields = (
        "bytes", "physical_lines", "blank_lines", "invalid_utf8_lines",
        "nul_bytes", "nul_lines", "malformed_json", "non_object_json",
        "duplicate_uuid_values", "duplicate_message_id_values",
        "explicit_roots", "missing_parent_references", "parent_reference_count",
        "parent_references_retained", "parent_references_truncated",
        "fork_points", "leaves",
        "weak_components", "reachable_from_explicit_roots", "unrooted_nodes",
        "cycle_count", "cycle_node_count", "node_count",
    )
    summary = {
        "files_expected": len(files),
        "files_present": sum(file["state"] == "present" for file in files),
        "files_missing": sum(file["state"] == "missing" for file in files),
        "files_empty": sum(file["state"] == "empty" for file in files),
        "files_unreadable": sum(file["state"] == "unreadable" for file in files),
        "bounded_files": sum(file["bounded"] for file in files),
        "partial_files": sum(file["partial"] for file in files),
    }
    summary.update({
        field: sum(file[field] for file in files)
        for field in sum_fields
    })
    # Stable aliases make the envelope convenient for callers without changing
    # the underlying structural diagnosis snapshot.
    summary.update({
        "expected_file_count": summary["files_expected"],
        "present_file_count": summary["files_present"],
        "missing_file_count": summary["files_missing"],
        "empty_file_count": summary["files_empty"],
        "unreadable_file_count": summary["files_unreadable"],
    })
    bounded = bool(summary["bounded_files"])
    partial = bool(summary["files_unreadable"] or summary["partial_files"])
    status = "partial" if partial else "bounded" if bounded else "complete"
    findings = []
    for file in files:
        reference = file["reference"]
        if file["state"] == "missing":
            findings.append({"kind": "missing_file", "reference": reference})
        elif file["state"] == "empty":
            findings.append({"kind": "empty_file", "reference": reference})
        elif file["state"] == "unreadable":
            findings.append({"kind": "unreadable_file", "reference": reference})
        for field, kind in (
            ("invalid_utf8_lines", "invalid_utf8"),
            ("nul_lines", "nul_bytes"),
            ("malformed_json", "malformed_json"),
            ("non_object_json", "non_object_json"),
            ("duplicate_uuid_values", "duplicate_uuid"),
            ("duplicate_message_id_values", "duplicate_message_id"),
            ("missing_parent_references", "missing_parent"),
            ("parent_references_truncated", "parent_references_truncated"),
            ("fork_points", "fork_point"),
            ("unrooted_nodes", "unrooted_nodes"),
            ("cycle_count", "cycle"),
        ):
            if file.get(field, 0):
                findings.append({"kind": kind, "reference": reference})
    findings.sort(key=lambda finding: (finding["reference"], finding["kind"]))
    errors = [
        {"reference": file["reference"], "code": code}
        for file in files
        for code in file.get("errors", ())
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "status": status,
        "limits": {
            "max_line_bytes": max_line_bytes,
            "max_file_bytes": max_file_bytes,
            "max_lines_per_file": max_lines_per_file,
            "max_nodes_per_file": max_nodes_per_file,
        },
        "summary": summary,
        "findings": findings,
        "errors": errors,
        "files": files,
    }
    if partial:
        suppress_partial_analysis(result)
    return result


def suppress_partial_analysis(result, *, extra_errors=()):
    """Make an incomplete integrity result opaque and non-inferential."""
    extra_errors = list(extra_errors)
    if result.get("_analysis_suppressed"):
        result["errors"].extend(extra_errors)
        result["errors"].sort(
            key=lambda error: (error["reference"], error["code"])
        )
        result["summary"]["scan_error_count"] += len(extra_errors)
        return result

    files = result.get("files", ())
    existing_errors = list(result.get("errors", ()))
    result["status"] = "partial"
    result["_analysis_suppressed"] = True
    result["summary"] = {
        "audited_file_count": len(files),
        "retained_bytes_read": sum(file.get("bytes_read", 0) for file in files),
        "retained_physical_line_count": sum(
            file.get("physical_lines", 0) for file in files
        ),
        "partial_file_count": sum(file.get("partial", False) for file in files),
        "file_error_count": len(existing_errors),
        "scan_error_count": len(extra_errors),
    }
    result["findings"] = []
    result["errors"] = existing_errors + extra_errors
    result["errors"].sort(
        key=lambda error: (error["reference"], error["code"])
    )
    result["files"] = []
    return result


def read_first_record_field(
    path,
    field,
    *,
    max_line_bytes=None,
    max_file_bytes=None,
    max_lines=None,
):
    """Return bounded field-read facts without exposing record content."""
    if max_line_bytes is None:
        max_line_bytes = DEFAULT_MAX_LINE_BYTES
    if max_file_bytes is None:
        max_file_bytes = DEFAULT_MAX_FILE_BYTES
    if max_lines is None:
        max_lines = DEFAULT_MAX_LINES_PER_FILE
    max_line_bytes = _positive_limit(max_line_bytes, "max_line_bytes")
    max_file_bytes = _positive_limit(max_file_bytes, "max_file_bytes")
    max_lines = _positive_limit(max_lines, "max_lines")
    bounded = False
    parse_failed = False
    try:
        if os.path.getsize(path) > max_file_bytes:
            return {
                "value": None,
                "bounded": True,
                "error": "byte_budget_exceeded",
            }
    except OSError:
        return {"value": None, "bounded": False, "error": "read_failed"}
    read_state = BoundedReadState()
    try:
        with open(path, "rb") as handle:
            for line in iter_bounded_binary_lines(
                handle,
                max_line_bytes,
                max_total_bytes=max_file_bytes,
                max_lines=max_lines,
                state=read_state,
            ):
                if line.truncated:
                    bounded = True
                    continue
                try:
                    text = line.prefix.decode("utf-8", errors="strict")
                    record = json.loads(text)
                except (ValueError, RecursionError):
                    parse_failed = True
                    continue
                if isinstance(record, dict):
                    value = record.get(field)
                    if isinstance(value, str) and value:
                        return {
                            "value": value,
                            "bounded": bounded,
                            "error": "parse_failed" if parse_failed else None,
                        }
    except OSError:
        return {"value": None, "bounded": bounded, "error": "read_failed"}
    if read_state.byte_budget_exhausted:
        return {
            "value": None,
            "bounded": True,
            "error": "byte_budget_exceeded",
        }
    if read_state.line_budget_exhausted:
        return {
            "value": None,
            "bounded": True,
            "error": "line_budget_exceeded",
        }
    return {
        "value": None,
        "bounded": bounded,
        "error": "parse_failed" if parse_failed else None,
    }


def first_record_field(
    path,
    field,
    *,
    max_line_bytes=None,
    max_file_bytes=None,
    max_lines=None,
):
    """Return the first non-empty string field through the bounded reader."""
    return read_first_record_field(
        path,
        field,
        max_line_bytes=max_line_bytes,
        max_file_bytes=max_file_bytes,
        max_lines=max_lines,
    )["value"]


def redact_user_home(path):
    """Redact the current user's home from an explicitly requested path."""
    path = os.path.abspath(os.fspath(path))
    candidates = []
    for value in (os.path.expanduser("~"), os.environ.get("USERPROFILE")):
        if value and value not in candidates:
            candidates.append(os.path.abspath(value))
    lowered = path.casefold()
    for home in sorted(candidates, key=len, reverse=True):
        home_abs = os.path.abspath(home)
        prefix = home_abs.rstrip("\\/")
        if lowered == prefix.casefold():
            return "%USERPROFILE%"
        if lowered.startswith((prefix + os.sep).casefold()):
            return "%USERPROFILE%" + path[len(prefix):]
    return path
