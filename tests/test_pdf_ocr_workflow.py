from __future__ import annotations

import hashlib
import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from pdf_chapter_splitter_core import build_ranges, split_pdf
from pdf_ocr_workflow import OcrAnalysisError, analyze_with_ocr
from umi_ocr_client import UmiOcrClient, UmiOcrError
from tests.test_umi_ocr_client import _FakeUmiOcrServer, make_document_pdf


class PdfOcrWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / "_fixtures"
        self.root.mkdir(exist_ok=True)
        self.source = self.root / f"{self._testMethodName}_source.pdf"
        self.destination = self.root / f"{self._testMethodName}_ocr.pdf"
        self.outputs = []
        self.batch_workspace = self.root / (self._testMethodName + "_workspace")
        self.batch_workspace.mkdir(exist_ok=True)

    def tearDown(self) -> None:
        for path in (self.source, self.destination, *self.outputs):
            path.unlink(missing_ok=True)
        self.assertTrue(self.batch_workspace.resolve().is_relative_to(self.root.resolve()))
        shutil.rmtree(self.batch_workspace)

    def write_source(self, pages: int = 3, with_bookmark: bool = False) -> None:
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=595, height=842)
        if with_bookmark:
            writer.add_outline_item("CHAPTER 1: Basics", 0)
        writer.write(self.source)

    def test_scanned_pdf_pipeline_preserves_source_and_exports_searchable_chapters(self) -> None:
        self.source.write_bytes(make_document_pdf(searchable=False))
        original_pdf = PdfReader(self.source)
        self.assertFalse(original_pdf.pages[0].extract_text().strip())
        self.assertEqual(len(original_pdf.pages[0].images), 1)
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        server = _FakeUmiOcrServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = UmiOcrClient(f"http://127.0.0.1:{server.server_port}", sleep=lambda _seconds: None)
            ready = Mock()
            result = analyze_with_ocr(self.source, self.destination, client, prepare_service=ready)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        ready.assert_called_once()
        self.assertTrue(result.used_ocr)
        self.assertEqual(result.pdf_path, self.destination)
        self.assertEqual([marker.page for marker in result.detection.markers], [1, 3])
        self.assertTrue(all(marker.source == "ocr" for marker in result.detection.markers))
        self.outputs = split_pdf(result.pdf_path, self.root, build_ranges(result.detection.markers, 3))
        self.assertEqual([len(PdfReader(path).pages) for path in self.outputs], [2, 1])
        self.assertIn("CHAPTER 2", PdfReader(self.outputs[1]).pages[0].extract_text())
        self.assertEqual(PdfReader(self.outputs[0]).pages[0].images[0].data, original_pdf.pages[0].images[0].data)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), digest)

    def test_bookmarks_and_bookmark_only_mode_never_start_automatic_ocr(self) -> None:
        self.write_source(with_bookmark=True)
        client = Mock()
        ready = Mock()
        result = analyze_with_ocr(self.source, self.destination, client, prepare_service=ready)
        self.assertFalse(result.used_ocr)
        self.assertEqual(result.pdf_path, self.source)
        self.write_source(with_bookmark=False)
        result = analyze_with_ocr(self.source, self.destination, client, mode="bookmark", prepare_service=ready)
        self.assertFalse(result.used_ocr)
        ready.assert_not_called()
        client.ocr_pdf.assert_not_called()

    def test_existing_text_without_headings_is_not_automatically_ocrd(self) -> None:
        pdf = canvas.Canvas(str(self.source))
        pdf.drawString(72, 720, "Ordinary text without a chapter heading")
        pdf.save()
        client = Mock()
        result = analyze_with_ocr(self.source, self.destination, client)
        self.assertFalse(result.used_ocr)
        client.ocr_pdf.assert_not_called()

    def test_forced_ocr_uses_full_page_mode_for_partial_or_bad_text_layers(self) -> None:
        self.write_source(with_bookmark=True)
        self.destination.write_bytes(self.source.read_bytes())
        client = Mock()
        result = analyze_with_ocr(self.source, self.destination, client, force=True)
        self.assertTrue(result.used_ocr)
        self.assertEqual(client.ocr_pdf.call_args.kwargs["extraction_mode"], "fullPage")

    def test_ocr_result_with_different_page_count_is_rejected(self) -> None:
        self.write_source(pages=3)
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.write(self.destination)
        with self.assertRaisesRegex(UmiOcrError, "页数"):
            analyze_with_ocr(self.source, self.destination, Mock())

    def test_ocr_pdf_is_preserved_when_post_ocr_chapter_analysis_fails(self) -> None:
        self.write_source()
        client = Mock()

        def write_ocr_output(_source: Path, destination: Path, **_kwargs: object) -> Path:
            destination.write_bytes(make_document_pdf(searchable=True))
            return destination

        client.ocr_pdf.side_effect = write_ocr_output
        with patch("pdf_ocr_workflow.analyze_pdf", side_effect=[
            Mock(total_pages=3, markers=[], method="none", has_text_layer=False),
            RuntimeError("章节文字层解析失败"),
        ]), self.assertRaises(OcrAnalysisError) as context:
            analyze_with_ocr(self.source, self.destination, client, force=True)

        self.assertEqual(context.exception.ocr_path, self.destination)
        self.assertTrue(self.destination.exists())
        self.assertGreater(self.destination.stat().st_size, 0)


    def test_large_pdf_uses_private_pool_and_balanced_chunks(self) -> None:
        self.write_source(pages=120)
        calls: list[int] = []
        class CopyClient:
            def ocr_pdf(self, source: Path, target: Path, **_kwargs: object) -> Path:
                input_pdf = PdfReader(source)
                calls.append(len(input_pdf.pages))
                writer = PdfWriter()
                for page in input_pdf.pages:
                    writer.add_page(page)
                writer.write(target)
                return target
        pool = Mock(clients=[CopyClient(), CopyClient()], engine_key="test")
        with patch("pdf_ocr_workflow.UmiOcrPool") as factory:
            factory.return_value.__enter__.return_value = pool
            result = analyze_with_ocr(self.source, self.destination, Mock(),
                                      workspace_dir=self.batch_workspace, executable=self.root / "Umi-OCR.exe")
        self.assertTrue(result.used_ocr)
        self.assertEqual(sorted(calls), [40, 40, 40])
        self.assertEqual(len(PdfReader(self.destination).pages), 120)

    def test_failed_new_ocr_does_not_claim_an_old_output_is_a_new_artifact(self) -> None:
        self.write_source()
        self.destination.write_bytes(self.source.read_bytes())
        original = self.destination.read_bytes()
        client = Mock()
        client.ocr_pdf.side_effect = UmiOcrError("new OCR failed")
        with self.assertRaises(OcrAnalysisError) as caught:
            analyze_with_ocr(self.source, self.destination, client, force=True)
        self.assertIsNone(caught.exception.ocr_path)
        self.assertEqual(self.destination.read_bytes(), original)
