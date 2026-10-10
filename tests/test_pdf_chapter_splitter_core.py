from __future__ import annotations

import hashlib
import shutil
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from pdf_chapter_splitter_core import (
    ChapterMarker,
    ChapterRange,
    DetectionResult,
    analyze_pdf,
    build_ranges,
    detect_heading_title,
    extract_bookmark_markers,
    extract_text_markers,
    recompute_ranges,
    safe_filename,
    split_pdf,
)


class HeadingDetectionTests(unittest.TestCase):
    def test_detects_chinese_and_english_chapter_headings(self) -> None:
        self.assertEqual(detect_heading_title("  第 1 章  文件基础  "), "第 1 章 文件基础")
        self.assertEqual(detect_heading_title("CHAPTER 2: Parsing"), "CHAPTER 2: Parsing")
        self.assertEqual(detect_heading_title("普通正文 第 3 章并不在页首"), None)
        self.assertIsNone(detect_heading_title("本章介绍 PDF 的基本概念"))


class RangeConstructionTests(unittest.TestCase):
    def test_build_ranges_deduplicates_markers_and_closes_each_range(self) -> None:
        markers = [
            ChapterMarker("第1章", 1, "bookmark"),
            ChapterMarker("第2章", 4, "bookmark"),
            ChapterMarker("第2章", 4, "text"),
            ChapterMarker("第3章", 7, "bookmark"),
        ]

        ranges = build_ranges(markers, total_pages=9)

        self.assertEqual(
            [(item.title, item.start_page, item.end_page) for item in ranges],
            [("第1章", 1, 3), ("第2章", 4, 6), ("第3章", 7, 9)],
        )
        self.assertEqual([item.index for item in ranges], [1, 2, 3])

    def test_build_ranges_rejects_missing_markers(self) -> None:
        with self.assertRaises(ValueError):
            build_ranges([], total_pages=10)

    def test_safe_filename_removes_windows_unsafe_characters(self) -> None:
        result = safe_filename('第1章: <基础>| "测试"?')
        self.assertEqual(result, "第1章 基础 测试")

    def test_recompute_ranges_keeps_manual_starts_continuous(self) -> None:
        ranges = recompute_ranges([
            ChapterRange(1, "第1章", 1, 99, "manual"),
            ChapterRange(2, "第2章", 5, 10, "manual"),
        ], total_pages=10)

        self.assertEqual([(item.start_page, item.end_page) for item in ranges], [(1, 4), (5, 10)])
        self.assertEqual([item.index for item in ranges], [1, 2])


class PdfDetectionAndSplitTests(unittest.TestCase):
    root = Path(__file__).parent / "_fixtures"

    @classmethod
    def setUpClass(cls) -> None:
        cls.root.mkdir(exist_ok=True)

    def tearDown(self) -> None:
        for path in self.root.glob(f"{self._testMethodName}*"):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    def _write_blank_pdf(self, path: Path, page_count: int) -> None:
        writer = PdfWriter()
        for _ in range(page_count):
            writer.add_blank_page(width=300, height=400)
        with path.open("wb") as stream:
            writer.write(stream)

    def _write_text_pdf(self, path: Path) -> None:
        pdf = canvas.Canvas(str(path), pagesize=letter)
        pages = [
            "CHAPTER 1: Basics",
            "这一页属于第一章的正文。",
            "CHAPTER 2: Parsing",
            "This page belongs to chapter two.",
        ]
        for text in pages:
            pdf.setFont("Helvetica", 14)
            pdf.drawString(72, 720, text)
            pdf.showPage()
        pdf.save()

    def test_extract_bookmark_markers_uses_one_based_pages(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        writer = PdfWriter()
        for _ in range(6):
            writer.add_blank_page(width=300, height=400)
        writer.add_outline_item("第1章 基础", 0)
        writer.add_outline_item("第2章 进阶", 3)
        with source.open("wb") as stream:
            writer.write(stream)

        markers = extract_bookmark_markers(source)

        self.assertEqual(
            [(item.title, item.page, item.source) for item in markers],
            [("第1章 基础", 1, "bookmark"), ("第2章 进阶", 4, "bookmark")],
        )

    def test_extract_text_markers_uses_page_text_when_bookmarks_are_missing(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_text_pdf(source)

        markers = extract_text_markers(source)

        self.assertEqual([(item.title, item.page) for item in markers], [("CHAPTER 1: Basics", 1), ("CHAPTER 2: Parsing", 3)])

    def test_analyze_pdf_auto_prefers_bookmarks(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        writer = PdfWriter()
        for _ in range(6):
            writer.add_blank_page(width=300, height=400)
        writer.add_outline_item("第1章 基础", 0)
        writer.add_outline_item("第2章 进阶", 3)
        with source.open("wb") as stream:
            writer.write(stream)

        result = analyze_pdf(source, mode="auto")

        self.assertIsInstance(result, DetectionResult)
        self.assertEqual(result.total_pages, 6)
        self.assertEqual(result.method, "bookmark")
        self.assertEqual([marker.page for marker in result.markers], [1, 4])

    def test_analyze_pdf_text_mode_ignores_bookmarks(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_text_pdf(source)

        result = analyze_pdf(source, mode="text")

        self.assertEqual(result.method, "text")
        self.assertEqual([marker.page for marker in result.markers], [1, 3])

    def test_analyze_pdf_rejects_unknown_mode(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_blank_pdf(source, page_count=1)

        with self.assertRaises(ValueError):
            analyze_pdf(source, mode="unknown")
    def test_split_pdf_creates_expected_page_counts_without_touching_source(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        output_dir = self.root / f"{self._testMethodName}_output"
        self._write_blank_pdf(source, page_count=5)
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        ranges = [
            ChapterRange(1, "第1章: 基础", 1, 2, "manual"),
            ChapterRange(2, "第2章/进阶", 3, 5, "manual"),
        ]

        outputs = split_pdf(source, output_dir, ranges)

        self.assertEqual([item.name for item in outputs], ["01_第1章 基础.pdf", "02_第2章 进阶.pdf"])
        self.assertEqual([len(PdfReader(item).pages) for item in outputs], [2, 3])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
