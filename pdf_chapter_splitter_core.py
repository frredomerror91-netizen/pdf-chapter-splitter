from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from pypdf import PdfReader, PdfWriter


@dataclass(frozen=True)
class ChapterMarker:
    """A detected chapter start, using one-based PDF page numbering."""

    title: str
    page: int
    source: str
    level: int = 0


@dataclass(frozen=True)
class ChapterRange:
    """A continuous, inclusive page range using one-based page numbering."""

    index: int
    title: str
    start_page: int
    end_page: int
    source: str


@dataclass(frozen=True)
class DetectionResult:
    """The result of one PDF chapter detection pass."""

    total_pages: int
    markers: list[ChapterMarker]
    method: str
    warning: str | None = None


_CHINESE_CHAPTER_RE = re.compile(
    r"^第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*章(?:\s*[-—:：.、]?\s*.*)?$",
    re.IGNORECASE,
)
_ENGLISH_CHAPTER_RE = re.compile(
    r"^chapter\s+(?:[0-9]+|[ivxlcdm]+)(?:\s*[-—:：.、]?\s*.*)?$",
    re.IGNORECASE,
)
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_FILENAME_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def detect_heading_title(text: str) -> str | None:
    """Return a normalized chapter heading when a line looks like a chapter title."""

    for raw_line in text.splitlines() or [text]:
        line = _normalise_text(raw_line)
        if not line or len(line) > 180:
            continue
        if _CHINESE_CHAPTER_RE.match(line) or _ENGLISH_CHAPTER_RE.match(line):
            return line
    return None


def _marker_preference(marker: ChapterMarker) -> tuple[int, int]:
    source_rank = {"bookmark": 3, "text": 2, "manual": 1}.get(marker.source, 0)
    return source_rank, -marker.level


def _deduplicate_markers(markers: Iterable[ChapterMarker], total_pages: int) -> list[ChapterMarker]:
    by_page: dict[int, ChapterMarker] = {}
    for marker in markers:
        if not 1 <= marker.page <= total_pages:
            continue
        current = by_page.get(marker.page)
        if current is None or _marker_preference(marker) > _marker_preference(current):
            by_page[marker.page] = marker
    return sorted(by_page.values(), key=lambda marker: marker.page)



def recompute_ranges(ranges: Sequence[ChapterRange], total_pages: int) -> list[ChapterRange]:
    """Recalculate inclusive ends from user-edited one-based starts."""

    if total_pages < 1:
        raise ValueError("PDF 页数必须大于 0")
    if not ranges:
        raise ValueError("没有可用的章节范围")

    ordered = sorted(ranges, key=lambda item: item.start_page)
    starts = [item.start_page for item in ordered]
    if any(start < 1 or start > total_pages for start in starts):
        raise ValueError("章节起始页必须位于 PDF 页面范围内")
    if len(starts) != len(set(starts)):
        raise ValueError("章节起始页不能重复")

    normalized: list[ChapterRange] = []
    first = ordered[0]
    if first.start_page > 1:
        normalized.append(ChapterRange(1, "前置内容", 1, first.start_page - 1, "generated"))

    for position, item in enumerate(ordered):
        end_page = ordered[position + 1].start_page - 1 if position + 1 < len(ordered) else total_pages
        if end_page < item.start_page:
            raise ValueError("章节起始页必须按升序排列")
        normalized.append(ChapterRange(0, item.title, item.start_page, end_page, item.source))

    return [
        ChapterRange(index, item.title, item.start_page, item.end_page, item.source)
        for index, item in enumerate(normalized, start=1)
    ]

def build_ranges(markers: Sequence[ChapterMarker], total_pages: int) -> list[ChapterRange]:
    """Build continuous inclusive ranges from chapter starts."""

    if total_pages < 1:
        raise ValueError("PDF 页数必须大于 0")

    unique_markers = _deduplicate_markers(markers, total_pages)
    if not unique_markers:
        raise ValueError("没有找到有效的章节起始页")

    ranges: list[ChapterRange] = []
    first_marker = unique_markers[0]
    if first_marker.page > 1:
        ranges.append(ChapterRange(1, "前置内容", 1, first_marker.page - 1, "generated"))

    for position, marker in enumerate(unique_markers):
        next_page = (
            unique_markers[position + 1].page - 1
            if position + 1 < len(unique_markers)
            else total_pages
        )
        if marker.page > next_page:
            continue
        ranges.append(
            ChapterRange(
                len(ranges) + 1,
                marker.title or f"第 {len(ranges) + 1} 章",
                marker.page,
                next_page,
                marker.source,
            )
        )

    if not ranges:
        raise ValueError("章节起始页无法构成有效范围")

    # Re-number after adding or skipping ranges so edits have stable display indices.
    return [
        ChapterRange(index, item.title, item.start_page, item.end_page, item.source)
        for index, item in enumerate(ranges, start=1)
    ]


def _walk_outline(items: object, level: int = 0) -> Iterable[tuple[object, int]]:
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, list):
            yield from _walk_outline(item, level + 1)
        else:
            yield item, level


def extract_bookmark_markers(pdf_path: Path) -> list[ChapterMarker]:
    """Extract chapter-like PDF outline entries, preserving their outline level."""

    reader = PdfReader(str(pdf_path))
    outline = reader.outline
    collected: list[ChapterMarker] = []
    for item, level in _walk_outline(outline):
        title = _normalise_text(str(getattr(item, "title", "")))
        if not title:
            continue
        try:
            page_index = reader.get_destination_page_number(item)
        except Exception:
            continue
        if page_index is None:
            continue
        collected.append(ChapterMarker(title, page_index + 1, "bookmark", level))

    chapter_like = [marker for marker in collected if detect_heading_title(marker.title)]
    if chapter_like:
        return _deduplicate_markers(chapter_like, len(reader.pages))

    if not collected:
        return []
    top_level = min(marker.level for marker in collected)
    return _deduplicate_markers(
        [marker for marker in collected if marker.level == top_level], len(reader.pages)
    )


def extract_text_markers(pdf_path: Path) -> list[ChapterMarker]:
    """Find the first chapter-like line on each page with a text layer."""

    reader = PdfReader(str(pdf_path))
    markers: list[ChapterMarker] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if not text.strip():
            continue
        for raw_line in text.splitlines()[:40]:
            title = detect_heading_title(raw_line)
            if title:
                markers.append(ChapterMarker(title, page_number, "text"))
                break
    return _deduplicate_markers(markers, len(reader.pages))



def analyze_pdf(pdf_path: Path, mode: str = "auto") -> DetectionResult:
    """Inspect a PDF with the selected detection strategy."""

    valid_modes = {"auto", "bookmark", "text"}
    if mode not in valid_modes:
        raise ValueError(f"未知的检测模式：{mode}")

    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        raise ValueError("PDF 已加密，请先解密或另存为未加密 PDF")
    total_pages = len(reader.pages)

    if mode in {"auto", "bookmark"}:
        bookmark_markers = extract_bookmark_markers(pdf_path)
        if bookmark_markers:
            return DetectionResult(total_pages, bookmark_markers, "bookmark")
        if mode == "bookmark":
            return DetectionResult(total_pages, [], "none", "没有找到可用的 PDF 书签章节。")

    text_markers = extract_text_markers(pdf_path)
    if text_markers:
        return DetectionResult(total_pages, text_markers, "text")
    return DetectionResult(total_pages, [], "none", "没有找到可识别的页面章节标题。")

def safe_filename(value: str) -> str:
    """Make a title safe for a Windows filename while retaining readable text."""

    cleaned = _INVALID_FILENAME_RE.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "未命名章节"
    if _RESERVED_FILENAME_RE.match(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned


def _validate_ranges(ranges: Sequence[ChapterRange], total_pages: int) -> None:
    if not ranges:
        raise ValueError("没有可导出的章节范围")
    expected_start = 1
    for item in ranges:
        if item.start_page != expected_start:
            raise ValueError("章节范围必须从第 1 页开始并连续排列")
        if item.end_page < item.start_page or item.end_page > total_pages:
            raise ValueError(f"无效页码范围：{item.start_page}-{item.end_page}")
        expected_start = item.end_page + 1
    if expected_start != total_pages + 1:
        raise ValueError("章节范围没有覆盖 PDF 的全部页面")


def split_pdf(
    pdf_path: Path, output_dir: Path, ranges: Sequence[ChapterRange]
) -> list[Path]:
    """Write one PDF for each continuous range, without modifying the source."""

    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        raise ValueError("PDF 已加密，请先在 PDFgear 中解密或另存为未加密 PDF")
    total_pages = len(reader.pages)
    _validate_ranges(ranges, total_pages)

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    used_names: set[str] = set()
    source_metadata = reader.metadata or {}

    for item in ranges:
        stem = safe_filename(item.title)
        base_name = f"{item.index:02d}_{stem}"
        candidate = base_name
        suffix = 2
        while candidate.lower() in used_names or (output_dir / f"{candidate}.pdf").exists():
            candidate = f"{base_name} ({suffix})"
            suffix += 1
        used_names.add(candidate.lower())

        writer = PdfWriter()
        for page_index in range(item.start_page - 1, item.end_page):
            writer.add_page(reader.pages[page_index])
        if source_metadata:
            writer.add_metadata({str(key): str(value) for key, value in source_metadata.items() if value is not None})

        destination = output_dir / f"{candidate}.pdf"
        with destination.open("wb") as stream:
            writer.write(stream)
        outputs.append(destination)

    return outputs
