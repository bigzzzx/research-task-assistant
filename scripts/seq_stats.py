"""Output ID, nucleotide count and GC percentage for each FASTA record."""

import argparse
from pathlib import Path

if __package__:
    from .fasta import Record, run_analysis
else:
    from fasta import Record, run_analysis


def seq_stats(record: Record) -> dict:
    sequence = record.sequence
    gc_count = sequence.count("G") + sequence.count("C")
    known_bases = gc_count + sequence.count("A") + sequence.count("T")
    return {
        "id": record.id,
        "length_nt": len(sequence),
        "gc_pct": round(100 * gc_count / known_bases, 2) if known_bases else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    raise SystemExit(run_analysis(parser.parse_args().path, seq_stats))
