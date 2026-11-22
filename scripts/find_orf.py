"""Find the longest complete ORF in the three forward reading frames."""

import argparse
from pathlib import Path

if __package__:
    from .fasta import Record, run_analysis
else:
    from fasta import Record, run_analysis

STOP_CODONS = {"TAA", "TAG", "TGA"}
CONCRETE_BASES = set("ACGT")


def find_orf(record: Record) -> dict:
    sequence = record.sequence
    best_start = 0
    best_length = 0

    for frame in range(3):
        start = None
        for position in range(frame, len(sequence) - 2, 3):
            codon = sequence[position:position + 3]
            if codon in STOP_CODONS:
                if start is not None:
                    length = position + 3 - start
                    if length > best_length or (length == best_length and start < best_start):
                        best_start, best_length = start, length
                start = None
            elif codon == "ATG" and start is None:
                start = position
            elif any(base not in CONCRETE_BASES for base in codon):
                start = None

    orf = None
    if best_length:
        orf = {"start": best_start + 1, "end": best_start + best_length, "length_nt": best_length}
    return {"id": record.id, "orf": orf}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    raise SystemExit(run_analysis(parser.parse_args().path, find_orf))
