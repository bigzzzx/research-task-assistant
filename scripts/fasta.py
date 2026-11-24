"""Read UTF-8 FASTA and report the input problems together."""

import json
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

DNA = frozenset("ACGTRYSWKMBDHVNacgtryswkmbdhvn")
MAX_DIAGNOSTICS = 40
MAX_COLUMN_RANGES = 12
DEMO_DELAY_SECONDS = 4


@dataclass
class Diagnostic:
    code: str
    message: str
    line: int | None = None
    characters: list[str] | None = None
    columns: list[tuple[int, int]] | None = None
    columns_truncated: bool | None = None


class InvalidFasta(Exception):
    def __init__(self, diagnostics: list[Diagnostic]):
        super().__init__("Invalid FASTA")
        self.diagnostics = diagnostics


@dataclass
class Record:
    id: str
    sequence: str


def read_fasta(path: Path) -> list[Record]:
    return parse_fasta(fasta_lines(path))


def fasta_lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8-sig") as source:
        yield from source


def invalid_base_columns(sequence: str, first_column: int) -> tuple[list[tuple[int, int]], bool]:
    ranges: list[tuple[int, int]] = []
    for column, base in enumerate(sequence, first_column):
        if base in DNA:
            continue
        if ranges and ranges[-1][1] == column - 1:
            ranges[-1] = (ranges[-1][0], column)
        elif len(ranges) == MAX_COLUMN_RANGES:
            return ranges, True
        else:
            ranges.append((column, column))
    return ranges, False


def parse_fasta(source: Iterable[str]) -> list[Record]:
    records: list[Record] = []
    errors: list[Diagnostic] = []
    omitted_errors = 0
    record_id: str | None = None
    title_line = 0
    missing_header = False
    parts: list[str] = []

    def report(error: Diagnostic) -> None:
        nonlocal omitted_errors
        if len(errors) < MAX_DIAGNOSTICS:
            errors.append(error)
        else:
            omitted_errors += 1

    def finish_record() -> None:
        if record_id is None:
            return
        if not parts:
            report(Diagnostic("EMPTY_SEQUENCE", "这条标题下面没有序列。", title_line))
        elif record_id:
            records.append(Record(record_id, "".join(parts)))

    try:
        for line_number, raw in enumerate(source, 1):
            if not raw.strip():
                continue
            if raw.startswith(">"):
                finish_record()
                parts = []
                title_line = line_number
                fields = raw[1:].split(maxsplit=1)
                record_id = fields[0] if fields else ""
                if not record_id:
                    report(Diagnostic("EMPTY_HEADER", "“>” 后需要填写序列 ID。", line_number))
                elif len(record_id) > 200 or not record_id.isprintable():
                    report(Diagnostic("INVALID_ID", "序列 ID 须为 1–200 个可见字符。", line_number))
                continue

            if record_id is None and not missing_header:
                missing_header = True
                report(Diagnostic("MISSING_HEADER", "首条记录缺少以“>”开头的标题行。", line_number,
                                  columns=[(1, 1)]))
                # A line with several words is likely the malformed title itself.
                # Still validate a first line that could be sequence data.
                if len(raw.split()) > 1:
                    continue

            sequence = raw.strip()
            invalid = list(dict.fromkeys(base for base in sequence if base not in DNA))
            if invalid:
                first_column = len(raw) - len(raw.lstrip()) + 1
                columns, truncated = invalid_base_columns(sequence, first_column)
                report(Diagnostic(
                    "INVALID_BASE", "序列中含无法识别的 DNA 碱基字符。", line_number,
                    invalid[:12], columns, truncated or None,
                ))
            if record_id is not None:
                parts.append(sequence.upper())
    except UnicodeError:
        report(Diagnostic("INVALID_ENCODING", "请将 FASTA 保存为 UTF-8 文本。"))
    except OSError:
        report(Diagnostic("INPUT_UNREADABLE", "读取文件失败，请确认文件存在且可读。"))

    finish_record()
    if not records and not errors:
        report(Diagnostic("EMPTY_FILE", "文件没有可分析的 FASTA 记录。"))
    if omitted_errors:
        errors.append(Diagnostic("MORE_ERRORS", f"另外 {omitted_errors} 处错误未展开。"))
    if errors:
        raise InvalidFasta(errors)
    return records


def run_analysis(path: Path, analyze: Callable[[Record], dict]) -> int:
    time.sleep(DEMO_DELAY_SECONDS)
    try:
        records = read_fasta(path)
    except InvalidFasta as exc:
        for diagnostic in exc.diagnostics:
            values = {key: value for key, value in asdict(diagnostic).items() if value is not None}
            print(json.dumps(values, ensure_ascii=False), file=sys.stderr)
        return 2

    for record in records:
        print(json.dumps(analyze(record), ensure_ascii=False))
    print(json.dumps({"record_count": len(records)}))
    return 0
