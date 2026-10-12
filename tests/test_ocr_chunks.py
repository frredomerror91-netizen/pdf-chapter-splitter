from __future__ import annotations

import hashlib
import io
import json
import shutil
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from ocr_chunks import ChunkOcrError, ocr_pdf_in_chunks, plan_ocr_chunks
from umi_ocr_client import OcrCancelledError, OcrProgress, UmiOcrError


def make_pdf(path: Path, start: int, count: int) -> None:
    pdf = canvas.Canvas(str(path))
    for number in range(start, start + count):
        pdf.drawString(72, 720, f"Original page {number}")
        pdf.showPage()
    pdf.save()


class Tracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls: list[int] = []
        self.failures: set[int] = set()
        self.bad_page_counts: set[int] = set()


class TestOcrClient:
    def __init__(self, tracker: Tracker) -> None:
        self.tracker = tracker

    def ocr_pdf(self, source: Path, destination: Path, **kwargs: object) -> Path:
        reader = PdfReader(source)
        first = int(reader.pages[0].extract_text().split()[-1])
        callback = kwargs.get("on_progress")
        cancel = kwargs.get("cancel_event")
        with self.tracker.lock:
            self.tracker.calls.append(first)
            self.tracker.active += 1
            self.tracker.peak = max(self.tracker.peak, self.tracker.active)
        try:
            # Different completion times deliberately scramble the output order.
            time.sleep(0.06 if first == 1 else 0.015)
            if first in self.tracker.failures:
                raise UmiOcrError(f"chunk starting on {first} failed")
            if callable(callback):
                callback(OcrProgress(str(first), "running", 1, len(reader.pages)))
            if cancel is not None and cancel.is_set():
                raise OcrCancelledError("cancelled")
            writer = PdfWriter()
            pages = reader.pages[:-1] if first in self.tracker.bad_page_counts else reader.pages
            for page in pages:
                writer.add_page(page)
            writer.write(destination)
            if callable(callback):
                callback(OcrProgress(str(first), "success", len(reader.pages), len(reader.pages), True))
            return destination
        finally:
            with self.tracker.lock:
                self.tracker.active -= 1


class ChunkPlanTests(unittest.TestCase):
    def test_657_pages_are_evenly_split_with_no_gap_or_overlap(self) -> None:
        chunks = plan_ocr_chunks(657, 50)
        self.assertEqual(len(chunks), 14)
        self.assertEqual([chunk.page_count for chunk in chunks], [47] * 13 + [46])
        self.assertEqual(chunks[0].start_page, 1)
        self.assertEqual(chunks[-1].end_page, 657)
        self.assertTrue(all(a.end_page + 1 == b.start_page for a, b in zip(chunks, chunks[1:])))

    def test_plan_rejects_empty_or_invalid_limits(self) -> None:
        for pages, size in ((0, 50), (-1, 50), (1, 0)):
            with self.subTest(pages=pages, size=size), self.assertRaises(ValueError):
                plan_ocr_chunks(pages, size)


class ChunkOcrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / "_fixtures" / ("chunks_" + hashlib.sha256(self._testMethodName.encode()).hexdigest()[:12])
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = self.root / "book.pdf"
        self.target = self.root / "book_OCR.pdf"
        self.cache = self.root / "chunks"
        make_pdf(self.source, 1, 7)
        self.tracker = Tracker()
        self.clients = [TestOcrClient(self.tracker), TestOcrClient(self.tracker)]

    def tearDown(self) -> None:
        self.assertTrue(self.root.resolve().is_relative_to(Path(__file__).resolve().parent))
        shutil.rmtree(self.root)

    def run_ocr(self, **kwargs: object) -> Path:
        return ocr_pdf_in_chunks(self.source, self.target, self.clients, cache_dir=self.cache,
                                 max_chunk_pages=3, **kwargs)

    def test_parallel_chunks_merge_in_original_page_order_and_preserve_source(self) -> None:
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        progress: list[OcrProgress] = []
        result = self.run_ocr(on_progress=progress.append)
        reader = PdfReader(result)
        self.assertEqual(len(reader.pages), 7)
        self.assertEqual([page.extract_text().strip() for page in reader.pages],
                         [f"Original page {number}" for number in range(1, 8)])
        self.assertEqual(self.tracker.peak, 2)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)
        self.assertTrue(all(p.total_pages == 7 for p in progress))
        self.assertEqual(progress[-1].processed_pages, 7)
        self.assertEqual(progress[-1].status, "success")
        self.assertEqual(progress[-1].total_chunks, 3)

    def test_failed_chunks_do_not_replace_complete_output_and_resume_reuses_finished_chunks(self) -> None:
        make_pdf(self.target, 100, 1)
        old_output = self.target.read_bytes()
        self.tracker.failures.add(4)
        with self.assertRaises(ChunkOcrError) as caught:
            self.run_ocr()
        self.assertEqual(self.target.read_bytes(), old_output)
        self.assertEqual(caught.exception.cache_dir.parent, self.cache)
        completed = set(self.tracker.calls) - {4}
        self.tracker.failures.clear()
        self.tracker.calls.clear()
        self.run_ocr()
        self.assertTrue(completed)
        self.assertTrue(all(first not in self.tracker.calls for first in completed))
        self.assertEqual(len(PdfReader(self.target).pages), 7)

    def test_cancel_does_not_publish_a_partial_pdf(self) -> None:
        cancel = threading.Event()
        def report(progress: OcrProgress) -> None:
            if progress.processed_pages:
                cancel.set()
        with self.assertRaises(OcrCancelledError):
            self.run_ocr(on_progress=report, cancel_event=cancel)
        self.assertFalse(self.target.exists())

    def test_source_changes_invalidate_previous_checkpoint(self) -> None:
        self.run_ocr()
        self.tracker.calls.clear()
        make_pdf(self.source, 10, 7)
        self.run_ocr()
        self.assertEqual(len(self.tracker.calls), 3)
        self.assertTrue(PdfReader(self.target).pages[0].extract_text().startswith("Original page 10"))

    def test_corrupt_cached_chunk_is_reprocessed(self) -> None:
        self.run_ocr()
        files = sorted(self.cache.glob("*/chunk_*_ocr.pdf"))
        self.assertEqual(len(files), 3)
        files[0].write_bytes(b"broken cache")
        self.tracker.calls.clear()
        self.run_ocr()
        self.assertEqual(self.tracker.calls, [1])

    def test_wrong_chunk_page_count_blocks_merge(self) -> None:
        self.tracker.bad_page_counts.add(4)
        with self.assertRaisesRegex(ChunkOcrError, "页数"):
            self.run_ocr()
        self.assertFalse(self.target.exists())

    def test_destination_cannot_be_original_file(self) -> None:
        self.target = self.source
        with self.assertRaisesRegex(UmiOcrError, "原始 PDF"):
            self.run_ocr()
        self.assertEqual(self.tracker.calls, [])


    def test_duplicate_batch_cannot_use_the_same_source_checkpoint_at_once(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        client = self.clients[0]
        original = client.ocr_pdf
        def blocking(source: Path, target: Path, **kwargs: object) -> Path:
            entered.set()
            release.wait(3)
            return original(source, target, **kwargs)
        client.ocr_pdf = blocking
        def run_first() -> None:
            try:
                self.run_ocr()
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run_first)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(UmiOcrError, "正在运行"):
                self.run_ocr()
        finally:
            release.set()
            worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_source_modified_during_recognition_blocks_publishing(self) -> None:
        original = self.clients[0].ocr_pdf
        def modifying(source: Path, target: Path, **kwargs: object) -> Path:
            result = original(source, target, **kwargs)
            make_pdf(self.source, 20, 7)
            return result
        self.clients[0].ocr_pdf = modifying
        with self.assertRaisesRegex(ChunkOcrError, "发生变化"):
            self.run_ocr()
        self.assertFalse(self.target.exists())

    def test_preparation_reports_every_page_and_can_cancel_before_ocr(self) -> None:
        cancel = threading.Event()
        progress: list[OcrProgress] = []
        def report(value: OcrProgress) -> None:
            progress.append(value)
            if getattr(value, 'prepared_pages', 0) == 2:
                cancel.set()
        with self.assertRaises(OcrCancelledError):
            self.run_ocr(on_progress=report, cancel_event=cancel)
        self.assertEqual(max(p.prepared_pages for p in progress), 2)
        self.assertEqual(self.tracker.calls, [])
        self.assertFalse(self.target.exists())

    def test_first_chunk_starts_recognition_before_last_chunk_is_prepared(self) -> None:
        entered = threading.Event()
        original_client = self.clients[0].ocr_pdf
        original_add = PdfWriter.add_page
        def client(source: Path, target: Path, **kwargs: object) -> Path:
            entered.set()
            return original_client(source, target, **kwargs)
        for item in self.clients:
            item.ocr_pdf = client
        def add_page(writer: PdfWriter, page, *args, **kwargs):
            text = page.extract_text().strip()
            if threading.current_thread() is threading.main_thread() and text == 'Original page 4':
                self.assertTrue(entered.wait(1), 'OCR must start while later inputs are still preparing')
            return original_add(writer, page, *args, **kwargs)
        with patch.object(PdfWriter, 'add_page', add_page):
            self.run_ocr()
        self.assertEqual(len(PdfReader(self.target).pages), 7)

    def test_resume_reuses_verified_input_after_engine_failure(self) -> None:
        self.tracker.failures.add(4)
        with self.assertRaises(ChunkOcrError):
            self.run_ocr()
        source_inputs = sorted(self.cache.glob('*/chunk_*_input.pdf'))
        saved_times = {item.name: item.stat().st_mtime_ns for item in source_inputs}
        self.tracker.failures.clear()
        observed = {}
        original = self.clients[0].ocr_pdf
        def record(source: Path, target: Path, **kwargs: object) -> Path:
            observed[source.name] = source.stat().st_mtime_ns
            return original(source, target, **kwargs)
        for item in self.clients:
            item.ocr_pdf = record
        self.run_ocr()
        self.assertTrue(observed)
        self.assertEqual(observed, {name: saved_times[name] for name in observed})

    def test_recovery_with_corrupt_input_rebuilds_it_instead_of_uploading_it(self) -> None:
        self.tracker.failures.add(4)
        with self.assertRaises(ChunkOcrError):
            self.run_ocr()
        failed_input = next(self.cache.glob('*/chunk_0002_input.pdf'))
        failed_input.write_bytes(b'broken input')
        self.tracker.failures.clear()
        original = self.clients[0].ocr_pdf
        rebuilt_pages = []
        def record(source: Path, target: Path, **kwargs: object) -> Path:
            if source.name == 'chunk_0002_input.pdf':
                rebuilt_pages.append(len(PdfReader(source).pages))
            return original(source, target, **kwargs)
        for item in self.clients:
            item.ocr_pdf = record
        self.run_ocr()
        self.assertEqual(rebuilt_pages, [2])
        self.assertEqual(len(PdfReader(self.target).pages), 7)

    def test_each_chunk_gets_its_own_engine_when_enough_clients_are_available(self) -> None:
        assignments = [[] for _ in range(7)]
        clients = []
        for index in range(7):
            client = TestOcrClient(self.tracker)
            original = client.ocr_pdf
            def assigned(source: Path, target: Path, _index=index, _call=original, **kwargs: object) -> Path:
                assignments[_index].append(source.name)
                return _call(source, target, **kwargs)
            client.ocr_pdf = assigned
            clients.append(client)
        ocr_pdf_in_chunks(self.source, self.target, clients, cache_dir=self.cache, max_chunk_pages=1)
        self.assertEqual(assignments, [[f'chunk_{number:04d}_input.pdf'] for number in range(1, 8)])
