from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Callable, Sequence

from pypdf import PdfReader, PdfWriter

from umi_ocr_client import OcrCancelledError, OcrProgress, UmiOcrClient, UmiOcrError


CHUNK_THRESHOLD = 100
MAX_CHUNK_PAGES = 50


@dataclass(frozen=True)
class OcrChunk:
    index: int
    start_page: int
    end_page: int

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1


class ChunkOcrError(UmiOcrError):
    def __init__(self, message: str, cache_dir: Path, completed_chunks: int) -> None:
        super().__init__(message)
        self.cache_dir = cache_dir
        self.completed_chunks = completed_chunks


def plan_ocr_chunks(total_pages: int, max_pages: int = MAX_CHUNK_PAGES) -> list[OcrChunk]:
    if total_pages < 1 or max_pages < 1:
        raise ValueError("PDF 页数和分块页数必须大于 0")
    count = (total_pages + max_pages - 1) // max_pages
    base, extra = divmod(total_pages, count)
    start = 1
    chunks = []
    for index in range(count):
        size = base + (1 if index < extra else 0)
        chunks.append(OcrChunk(index + 1, start, start + size - 1))
        start += size
    return chunks


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _save_json(path: Path, data: dict) -> None:
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        staging.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def _check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise OcrCancelledError("分块 OCR 已取消，已完成的分块会保留供下次继续。")


def _copy_outline(reader: PdfReader, writer: PdfWriter) -> None:
    def visit(items: list, parent=None) -> None:
        previous = parent
        for item in items:
            if isinstance(item, list):
                visit(item, previous)
                continue
            try:
                page = reader.get_destination_page_number(item)
                if page is not None and 0 <= page < len(reader.pages):
                    previous = writer.add_outline_item(str(item.title), page, parent=parent)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    try:
        visit(reader.outline)
    except (ValueError, TypeError, KeyError):
        pass



def _exclusive_source(function):
    @wraps(function)
    def guarded(source_path: Path, destination_path: Path, clients, **kwargs):
        directory = Path(kwargs["cache_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(os.path.normcase(str(source_path.resolve())).encode("utf-8")).hexdigest()
        lock_file = directory / f".{key}.active.lock"
        with lock_file.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise UmiOcrError("这份 PDF 已有分块 OCR 任务正在运行，请等待或取消该任务后再试。") from exc
            try:
                return function(source_path, destination_path, clients, **kwargs)
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
    return guarded


@_exclusive_source
def ocr_pdf_in_chunks(
    source_path: Path,
    destination_path: Path,
    clients: Sequence[UmiOcrClient],
    *,
    cache_dir: Path,
    max_chunk_pages: int = MAX_CHUNK_PAGES,
    extraction_mode: str = "mixed",
    on_progress: Callable[[OcrProgress], None] | None = None,
    cancel_event: threading.Event | None = None,
    engine_key: str = "default",
) -> Path:
    """Run one queue per independent client and publish only a verified complete PDF."""
    if source_path.resolve() == destination_path.resolve():
        raise UmiOcrError("分块 OCR 输出不能覆盖原始 PDF。")
    if not clients:
        raise UmiOcrError("没有可用的 OCR 引擎。")
    _check_cancelled(cancel_event)
    source = PdfReader(source_path)
    if source.is_encrypted:
        raise UmiOcrError("PDF 已加密，请先另存为未加密 PDF。")
    chunks = plan_ocr_chunks(len(source.pages), max_chunk_pages)
    signature = {"version": 1, "source_sha256": _sha256(source_path), "pages": len(source.pages),
                 "max_chunk_pages": max_chunk_pages, "mode": extraction_mode, "engine": engine_key}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    job_dir = cache_dir / key
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("signature") != signature:
            manifest = {}
    except (OSError, ValueError):
        manifest = {}
    entries = manifest.get("completed", {})
    if not isinstance(entries, dict):
        entries = {}
    lock = threading.Lock()
    completed: dict[int, Path] = {}
    local_progress: dict[int, int] = {}
    failures: list[str] = []
    remaining: deque[OcrChunk] = deque()

    def output_path(chunk: OcrChunk) -> Path:
        return job_dir / f"chunk_{chunk.index:04d}_ocr.pdf"

    for chunk in chunks:
        target = output_path(chunk)
        entry = entries.get(str(chunk.index), {})
        try:
            valid = (isinstance(entry, dict) and entry.get("pages") == chunk.page_count
                     and target.is_file() and _sha256(target) == entry.get("sha256")
                     and len(PdfReader(target).pages) == chunk.page_count)
        except Exception:
            valid = False
        if valid:
            completed[chunk.index] = target
            local_progress[chunk.index] = chunk.page_count
        else:
            entries.pop(str(chunk.index), None)
            remaining.append(chunk)

    def report(status: str = "running", message: str | None = None) -> None:
        # Called under the same lock that protects page counts, so UI updates stay ordered.
        if on_progress is not None:
            on_progress(OcrProgress(key, status, sum(local_progress.values()), len(source.pages),
                                    status == "success", message,
                                    completed_chunks=len(completed), total_chunks=len(chunks)))

    report("splitting", f"均匀分为 {len(chunks)} 份，已复用 {len(completed)} 份已完成结果。")
    # PdfReader streams are not thread safe. Build inputs on this one thread.
    for chunk in remaining:
        _check_cancelled(cancel_event)
        writer = PdfWriter()
        for index in range(chunk.start_page - 1, chunk.end_page):
            writer.add_page(source.pages[index])
        writer.write(job_dir / f"chunk_{chunk.index:04d}_input.pdf")
    _save_json(manifest_path, {"signature": signature, "completed": entries})

    def process_queue(client: UmiOcrClient) -> None:
        while True:
            with lock:
                if not remaining or (cancel_event is not None and cancel_event.is_set()):
                    return
                chunk = remaining.popleft()
                local_progress[chunk.index] = 0
            input_path = job_dir / f"chunk_{chunk.index:04d}_input.pdf"
            staging = job_dir / f"chunk_{chunk.index:04d}_{uuid.uuid4().hex}.incoming.pdf"
            def update(progress: OcrProgress) -> None:
                with lock:
                    local_progress[chunk.index] = max(local_progress[chunk.index], min(chunk.page_count, progress.processed_pages))
                    report(message=f"原 PDF 第 {chunk.start_page}–{chunk.end_page} 页正在识别。")
            try:
                client.ocr_pdf(input_path, staging, extraction_mode=extraction_mode,
                               on_progress=update, cancel_event=cancel_event)
                _check_cancelled(cancel_event)
                if len(PdfReader(staging).pages) != chunk.page_count:
                    raise UmiOcrError(f"第 {chunk.index} 份 OCR 结果页数不一致。")
                staging.replace(output_path(chunk))
                digest = _sha256(output_path(chunk))
                with lock:
                    completed[chunk.index] = output_path(chunk)
                    local_progress[chunk.index] = chunk.page_count
                    entries[str(chunk.index)] = {"pages": chunk.page_count, "sha256": digest,
                                                "start": chunk.start_page, "end": chunk.end_page}
                    _save_json(manifest_path, {"signature": signature, "completed": entries})
                    report()
                input_path.unlink(missing_ok=True)
            except OcrCancelledError:
                if cancel_event is not None:
                    cancel_event.set()
                return
            except Exception as exc:
                with lock:
                    failures.append(f"第 {chunk.index} 份（{chunk.start_page}–{chunk.end_page} 页）：{exc}")
                    report()
            finally:
                staging.unlink(missing_ok=True)

    with ThreadPoolExecutor(max_workers=min(len(clients), len(chunks))) as executor:
        futures = [executor.submit(process_queue, client) for client in clients[:len(chunks)]]
        for future in futures:
            future.result()
    _check_cancelled(cancel_event)
    if failures or len(completed) != len(chunks):
        raise ChunkOcrError(f"分块 OCR 未完成，已保留 {len(completed)}/{len(chunks)} 份结果；"
                            f"重试可继续。{'；'.join(failures)}\n断点目录：{job_dir}",
                            job_dir, len(completed))
    if _sha256(source_path) != signature["source_sha256"]:
        raise ChunkOcrError("原 PDF 在识别期间发生变化，已停止合并，请重新分析当前文件。", job_dir, len(completed))
    report("merging", "全部分块识别完成，正在按原页序合并。")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    staging = destination_path.with_name(f".{destination_path.stem}.{uuid.uuid4().hex}.merge.pdf")
    try:
        writer = PdfWriter()
        for chunk in chunks:
            _check_cancelled(cancel_event)
            result = PdfReader(completed[chunk.index])
            if len(result.pages) != chunk.page_count:
                raise UmiOcrError("合并前发现分块页数不一致，请重试该任务。")
            for page in result.pages:
                writer.add_page(page)
        if source.metadata:
            writer.add_metadata({str(k): str(v) for k, v in source.metadata.items() if v is not None})
        _copy_outline(source, writer)
        writer.write(staging)
        if len(PdfReader(staging).pages) != len(source.pages):
            raise UmiOcrError("合并后的 OCR PDF 页数与原文件不一致。")
        _check_cancelled(cancel_event)
        staging.replace(destination_path)
    finally:
        staging.unlink(missing_ok=True)
    report("success", f"完整 OCR PDF 已保存：{destination_path}")
    return destination_path
