from __future__ import annotations

import io
import unittest
from pypdf import PdfWriter
from pypdf.generic import IndirectObject, NameObject, NullObject
from pdf_reader_recovery import RecoveryPdfReader


class CountingStream(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.full_reads = 0
    def read(self, size: int = -1) -> bytes:
        if size == -1:
            self.full_reads += 1
        return super().read(size)


class PdfRecoveryTests(unittest.TestCase):
    def test_many_dangling_references_do_not_scan_the_file_repeatedly(self) -> None:
        writer = PdfWriter()
        page = writer.add_blank_page(width=595, height=842)
        for number in range(700, 710):
            page[NameObject(f'/Broken{number}')] = IndirectObject(number, 0, writer)
        stream = io.BytesIO()
        writer.write(stream)
        counted = CountingStream(stream.getvalue())
        reader = RecoveryPdfReader(counted)
        baseline = counted.full_reads
        copy = PdfWriter()
        copy.add_page(reader.pages[0])
        self.assertLessEqual(counted.full_reads - baseline, 1)
        self.assertEqual(len(reader.missing_objects), 10)
        self.assertEqual(tuple(copy.pages[0].mediabox), (0, 0, 595, 842))
        self.assertIsInstance(reader.get_object(IndirectObject(700, 0, reader)), NullObject)

    def test_recovery_finds_a_real_object_missing_from_the_xref_table(self) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        stream = io.BytesIO()
        writer.write(stream)
        reader = RecoveryPdfReader(io.BytesIO(stream.getvalue()))
        reference = reader.pages[0].indirect_reference
        reader.resolved_objects.pop((reference.generation, reference.idnum), None)
        del reader.xref[reference.generation][reference.idnum]
        page = reader.get_object(reference)
        self.assertEqual(page['/Type'], '/Page')
        self.assertEqual(reader.missing_objects, set())
