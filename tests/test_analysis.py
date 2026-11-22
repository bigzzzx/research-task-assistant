from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from backend.app.config import LOG_LIMIT, ROOT
from backend.app.jobs import bounded_log
from scripts.fasta import InvalidFasta, Record, read_fasta
from scripts.find_orf import find_orf
from scripts.seq_stats import seq_stats


class AnalysisTests(TestCase):
    def test_examples_match_reference_values(self):
        records = [
            *read_fasta(ROOT / "data" / "sample1.fasta"),
            *read_fasta(ROOT / "data" / "sample2.fasta"),
        ]
        expected = [
            ("seq1", 45, 48.89, 1, 45, 45),
            ("seq2", 47, 61.70, 30, 47, 18),
            ("gene_A", 94, 53.19, 6, 83, 78),
            ("gene_B", 41, 43.90, 7, 21, 15),
        ]
        for record, values in zip(records, expected, strict=True):
            with self.subTest(record=record.id):
                stats = seq_stats(record)
                orf = find_orf(record)["orf"]
                self.assertEqual(
                    (stats["id"], stats["length_nt"], stats["gc_pct"], orf["start"], orf["end"], orf["length_nt"]),
                    values,
                )

    def test_sample3_aggregates_both_diagnostics(self):
        with self.assertRaises(InvalidFasta) as failure:
            read_fasta(ROOT / "data" / "sample3.fasta")
        issues = failure.exception.diagnostics
        self.assertEqual([(item.code, item.line) for item in issues], [("MISSING_HEADER", 1), ("INVALID_BASE", 2)])
        self.assertEqual(issues[1].characters, ["X", "1", "2", "3"])

    def test_bom_multiline_and_ambiguous_bases(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "种子样本.fa"
            path.write_bytes("\ufeff>id description\r\nac\r\n\r\ngn\r\n".encode())
            records = read_fasta(path)
            self.assertEqual(records, [Record("id", "ACGN")])
            self.assertEqual(seq_stats(records[0]), {"id": "id", "length_nt": 4, "gc_pct": 66.67})
            self.assertIsNone(seq_stats(Record("id", "NNN"))["gc_pct"])

    def test_empty_encoding_and_format(self):
        cases = [
            (b"", {"EMPTY_FILE"}),
            (b">\nACG", {"EMPTY_HEADER"}),
            (b">id\n\n", {"EMPTY_SEQUENCE"}),
            (b">id\nA C", {"INVALID_BASE"}),
            (b">id\n\xff", {"INVALID_ENCODING"}),
            (" >id\nABß".encode(), {"MISSING_HEADER", "INVALID_BASE"}),
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.fna"
            for raw, codes in cases:
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    with self.assertRaises(InvalidFasta) as failure:
                        read_fasta(path)
                    self.assertEqual({item.code for item in failure.exception.diagnostics}, codes)

    def test_complete_forward_orf_boundaries(self):
        cases = {"ATGTAAATGTAG": (1, 6, 6), "CATGATGTAA": (2, 10, 9), "ATGAAA": None,
                 "ATGATAA": None, "ATGNNNTAA": None, "TTATTTCAT": None, "G": None}
        for sequence, expected in cases.items():
            with self.subTest(sequence=sequence):
                result = find_orf(Record("a", sequence))["orf"]
                values = (result["start"], result["end"], result["length_nt"]) if result else None
                self.assertEqual(values, expected)

    def test_dual_byte_bounded_preview(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "output.log"
            path.write_bytes(("HEAD\n" + "甲乙" * LOG_LIMIT + "\nTAIL").encode("utf-8"))
            preview, truncated = bounded_log(path)
            self.assertTrue(truncated)
            self.assertLessEqual(len(preview.encode()), LOG_LIMIT)
            self.assertTrue(preview.startswith("HEAD\n"))
            self.assertTrue(preview.endswith("\nTAIL"))
            self.assertIn("[... log truncated ...]", preview)
            path.write_bytes(b"short")
            self.assertEqual(bounded_log(path), ("short", False))
