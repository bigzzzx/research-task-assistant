"""Locate FASTA input errors and preview a single header correction."""

from codecs import BOM_UTF8
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re

from scripts.fasta import InvalidFasta, parse_fasta

from .config import UPLOAD_LIMIT
from .failures import INPUT_CODES

DIAGNOSTIC_LOG_LIMIT = 64 * 1024
DIAGNOSTIC_DISPLAY_LIMIT = 12
EXCERPT_LENGTH = 120
EXCERPT_CONTEXT = 16


def source_excerpts(line: str, columns: list[list[int]]) -> list[dict]:
    excerpts = []
    next_column = 0
    for first, _ in columns or [(1, 1)]:
        if first < next_column:
            continue
        start = max(1, first - EXCERPT_CONTEXT)
        next_column = start + EXCERPT_LENGTH
        excerpts.append({
            "start_column": start,
            "text": line[start - 1:next_column - 1],
            "has_more": len(line) >= next_column,
        })
    return excerpts


def input_diagnostics(log_path: Path, source_path: Path | None) -> dict:
    """Read structured stderr and attach excerpts for the signed-in workspace."""
    issues = []
    try:
        with log_path.open("rb") as log:
            raw = log.read(DIAGNOSTIC_LOG_LIMIT + 1)
    except OSError:
        return {"issues": [], "truncated": False}

    truncated = len(raw) > DIAGNOSTIC_LOG_LIMIT
    for line in raw[:DIAGNOSTIC_LOG_LIMIT].splitlines():
        try:
            issue = json.loads(line)
        except ValueError:
            continue
        if not isinstance(issue, dict) or not isinstance(code := issue.get("code"), str):
            continue
        if code not in INPUT_CODES and code != "MORE_ERRORS":
            continue
        if len(issues) >= DIAGNOSTIC_DISPLAY_LIMIT:
            truncated = True
            break
        issues.append({**issue, "excerpts": []})

    if source_path is None:
        return {"issues": issues, "truncated": truncated}

    by_line = defaultdict(list)
    for issue in issues:
        if isinstance(issue.get("line"), int):
            by_line[issue["line"]].append(issue)
    if by_line:
        last_line = max(by_line)
        try:
            with source_path.open(encoding="utf-8-sig") as source:
                for number, raw_line in enumerate(source, 1):
                    for issue in by_line.get(number, ()):
                        issue["excerpts"] = source_excerpts(raw_line.rstrip("\r\n"), issue.get("columns", []))
                    if number >= last_line:
                        break
        except (OSError, UnicodeError):
            pass
    return {"issues": issues, "truncated": truncated}


@dataclass(frozen=True)
class FormatRepair:
    token: str
    line: int
    before: str
    after: str
    content: bytes


def preview_repair(path: Path) -> FormatRepair | None:
    try:
        with path.open("rb") as source:
            raw = source.read(UPLOAD_LIMIT + 1)
        if len(raw) > UPLOAD_LIMIT:
            return None
        lines = StringIO(raw.decode("utf-8-sig"), newline="").readlines()
        first = next((index for index, line in enumerate(lines) if line.strip()), None)
        if first is None:
            return None
        original = lines[first]
        before = original.rstrip("\r\n")
        if len(before) > 500 or before.startswith(">"):
            return None
        after = before.lstrip(" \t")
        if not after.startswith(">"):
            title = after.split(maxsplit=1)
            if (len(title) != 2 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,199}", title[0])
                    or not any(character in "_.-" for character in title[0])):
                return None
            after = ">" + after
        lines[first] = after + original[len(before):]
        corrected = "".join(lines)
        parse_fasta(StringIO(corrected, newline=None))
    except (OSError, UnicodeError, InvalidFasta):
        return None
    content = (BOM_UTF8 if raw.startswith(BOM_UTF8) else b"") + corrected.encode("utf-8")
    if len(content) > UPLOAD_LIMIT:
        return None
    return FormatRepair(sha256(raw + b"\0" + content).hexdigest(), first + 1, before, after, content)
