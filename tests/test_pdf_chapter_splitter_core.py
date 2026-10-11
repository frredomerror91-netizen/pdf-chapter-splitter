from __future__ import annotations

import hashlib
import shutil
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
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
    should_auto_ocr,
    split_pdf,
)


class HeadingDetectionTests(unittest.TestCase):
    def test_detects_chinese_and_english_chapter_headings(self) -> None:
        self.assertEqual(detect_heading_title("  第 1 章  文件基础  "), "第 1 章 文件基础")
        self.assertEqual(detect_heading_title("CHAPTER 2: Parsing"), "CHAPTER 2: Parsing")
        self.assertEqual(detect_heading_title("普通正文 第 3 章并不在页首"), None)
        self.assertIsNone(detect_heading_title("本章介绍 PDF 的基本概念"))

    def test_table_of_contents_lines_are_not_chapter_start_titles(self) -> None:
        self.assertIsNone(detect_heading_title("CHAPTER 1: Basics ........ 12"))
        self.assertIsNone(detect_heading_title("第十一章 静电场………1"))

    def test_starred_optional_chapter_titles_are_supported(self) -> None:
        self.assertEqual(detect_heading_title("*第15章 原子核物理和粒子物理简介"), "第15章 原子核物理和粒子物理简介")
        self.assertEqual(detect_heading_title("＊ CHAPTER III: Optional"), "CHAPTER III: Optional")


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

    def _write_heading_pages(self, path: Path, pages: list[list[str]]) -> None:
        pdf = canvas.Canvas(str(path), pagesize=letter)
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        for lines in pages:
            pdf.setFont("STSong-Light", 12)
            for index, line in enumerate(lines):
                pdf.drawString(72, 720 - 10 * index, line)
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

    def test_contents_and_repeated_chapter_headers_do_not_split_every_page(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_heading_pages(source, [
            ["CONTENTS", "CHAPTER 1: Basics", "CHAPTER 2: Parsing"],
            ["Preface"],
            ["CHAPTER 1: Basics", "First chapter begins here"],
            ["CHAPTER 1: Basics", "More body text"],
            ["CHAPTER 2: Parsing", "Second chapter begins here"],
            ["CHAPTER 2: Parsing", "More body text"],
        ])
        result = analyze_pdf(source, mode="text")
        self.assertEqual([(item.title, item.page) for item in result.markers], [("CHAPTER 1: Basics", 3), ("CHAPTER 2: Parsing", 5)])
        ranges = build_ranges(result.markers, result.total_pages)
        self.assertEqual([(item.start_page, item.end_page) for item in ranges], [(1, 2), (3, 4), (5, 6)])

    def test_unlabelled_contents_page_with_multiple_chapters_is_skipped(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_heading_pages(source, [
            ["CHAPTER 1: Basics", "CHAPTER 2: Parsing", "CHAPTER 3: Exports"],
            ["CHAPTER 1: Basics", "Body"],
            ["CHAPTER 2: Parsing", "Body"],
            ["CHAPTER 3: Exports", "Body"],
        ])
        self.assertEqual([item.page for item in extract_text_markers(source)], [2, 3, 4])

    def test_ocr_contents_headers_with_roman_page_numbers_and_extra_glyphs_are_skipped(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_heading_pages(source, [
            ["目录O", "第9章 气体动理论"],
            ["Ⅱ目录", "第10章 热力学基础"],
            ["第9章 气体动理论"],
            ["第10章 热力学基础"],
        ])
        self.assertEqual([item.page for item in extract_text_markers(source)], [3, 4])

    def test_contents_marker_at_page_footer_is_detected_even_after_forty_lines(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_heading_pages(source, [
            ["第15章 原子物理"] + ["15.1 Section index"] * 45 + ["目录V"],
            ["第15章 原子物理"],
        ])
        self.assertEqual([item.page for item in extract_text_markers(source)], [2])

    def test_unlabelled_contents_with_later_chapters_beyond_forty_lines_is_skipped(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_heading_pages(source, [
            ["CHAPTER 4: Algebra"] + ["4.1 Section index"] * 45 + ["CHAPTER 5: Maps"],
            ["CHAPTER 4: Algebra"], ["CHAPTER 5: Maps"],
        ])
        self.assertEqual([item.page for item in extract_text_markers(source)], [2, 3])

    def test_chapter_number_and_name_on_adjacent_lines_are_joined(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_heading_pages(source, [
            ["第9章", "气体动理论", "这是正文的开始。"],
            ["第10章", "热力学基础", "这是下一章正文的开始。"],
        ])
        self.assertEqual(
            [(item.title, item.page) for item in extract_text_markers(source)],
            [("第9章 气体动理论", 1), ("第10章 热力学基础", 2)],
        )

    def test_starred_chapter_start_is_used_before_later_running_header(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_heading_pages(source, [
            ["第14章 量子物理"],
            ["*第15章", "原子核物理和粒子物理简介", "这是选学章节的正文。"],
            ["第15章原子核物理和粒子物理简介·267"],
        ])
        self.assertEqual(
            [(item.title, item.page) for item in extract_text_markers(source)],
            [("第14章 量子物理", 1), ("第15章 原子核物理和粒子物理简介", 2)],
        )

    def test_roman_and_arabic_running_headers_for_same_chapter_are_collapsed(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        self._write_heading_pages(source, [
            ["CHAPTER I: Basics"], ["CHAPTER 1: Basics"],
            ["CHAPTER II: Parsing"], ["CHAPTER 2: Parsing"],
        ])
        self.assertEqual([item.page for item in extract_text_markers(source)], [1, 3])

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

    def test_page_number_bookmarks_and_front_matter_do_not_become_chapters(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        writer = PdfWriter()
        for _ in range(12):
            writer.add_blank_page(width=300, height=400)
        writer.add_outline_item("封面", 0)
        writer.add_outline_item("目录", 1)
        for page in range(2, 12):
            writer.add_outline_item(str(page - 1), page)
        writer.write(source)

        markers = extract_bookmark_markers(source)
        result = analyze_pdf(source)

        self.assertEqual(markers, [])
        self.assertEqual(result.method, "none")
        self.assertTrue(should_auto_ocr(result))

    def test_numbered_page_bookmarks_are_ignored_but_named_top_level_sections_remain(self) -> None:
        source = self.root / f"{self._testMethodName}.pdf"
        writer = PdfWriter()
        for _ in range(10):
            writer.add_blank_page(width=300, height=400)
        for title, page in (("封面", 0), ("Page 2", 1), ("Introduction", 2), ("第 4 页", 3), ("Methods", 5), ("42", 6)):
            writer.add_outline_item(title, page)
        writer.write(source)

        self.assertEqual(
            [(item.title, item.page) for item in extract_bookmark_markers(source)],
            [("Introduction", 3), ("Methods", 6)],
        )

    def test_analyze_pdf_distinguishes_missing_text_layer_from_unmatched_headings(self) -> None:
        blank_source = self.root / f"{self._testMethodName}_blank.pdf"
        self._write_blank_pdf(blank_source, page_count=2)
        blank_result = analyze_pdf(blank_source, mode="auto")

        text_source = self.root / f"{self._testMethodName}_text.pdf"
        pdf = canvas.Canvas(str(text_source), pagesize=letter)
        pdf.drawString(72, 720, "这是普通正文，没有章节标题。")
        pdf.showPage()
        pdf.save()
        text_result = analyze_pdf(text_source, mode="auto")

        self.assertFalse(blank_result.has_text_layer)
        self.assertTrue(text_result.has_text_layer)
        self.assertTrue(should_auto_ocr(blank_result, mode="auto"))
        self.assertFalse(should_auto_ocr(text_result, mode="auto"))

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
