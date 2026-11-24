"""Output ID, nucleotide count and GC percentage for each FASTA record."""

import argparse
from decimal import Decimal
from pathlib import Path

if __package__:
    from .fasta import Record, run_analysis
else:
    from fasta import Record, run_analysis


def gc_percentage(gc_count: int, known_bases: int, decimal_places: int = 2) -> Decimal | None:
    if not known_bases:
        return None
    scale = 10 ** decimal_places
    digits, remainder = divmod(100 * scale * gc_count, known_bases)
    if remainder * 2 >= known_bases:
        digits += 1
    return Decimal(digits).scaleb(-decimal_places)


def seq_stats(record: Record) -> dict:
    sequence = record.sequence
    gc_count = sequence.count("G") + sequence.count("C")
    known_bases = gc_count + sequence.count("A") + sequence.count("T")
    percentage = gc_percentage(gc_count, known_bases)
    return {
        "id": record.id,
        "length_nt": len(sequence),
        "gc_pct": float(percentage) if percentage is not None else None,
        "gc_count": gc_count,
        "known_bases": known_bases,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    raise SystemExit(run_analysis(parser.parse_args().path, seq_stats))
