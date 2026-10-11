from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from pdf_chapter_splitter_core import DetectionResult, analyze_pdf, should_auto_ocr
from umi_ocr_client import OcrCancelledError, ProgressCallback, UmiOcrClient, UmiOcrError
from ocr_chunks import CHUNK_THRESHOLD, ocr_pdf_in_chunks
from umi_ocr_pool import UmiOcrPool
from umi_ocr_service import find_umi_executable, load_settings


@dataclass(frozen=True)
class AnalysisOutcome:
    detection: DetectionResult
    pdf_path: Path
    used_ocr: bool = False


class OcrAnalysisError(UmiOcrError):
    def __init__(self, message: str, detection: DetectionResult, ocr_path: Path | None = None) -> None:
        super().__init__(message)
        self.detection = detection
        self.ocr_path = ocr_path


def analyze_with_ocr(
    source_path: Path,
    destination_path: Path,
    client: UmiOcrClient,
    *,
    mode: str = "auto",
    force: bool = False,
    auto_ocr: bool = True,
    prepare_service: Callable[[], None] | None = None,
    on_progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    workspace_dir: Path | None = None,
    executable: Path | None = None,
) -> AnalysisOutcome:
    if cancel_event is not None and cancel_event.is_set():
        raise OcrCancelledError("处理已取消。")
    detection = analyze_pdf(source_path, mode=mode)
    if not force and not (auto_ocr and should_auto_ocr(detection, mode)):
        return AnalysisOutcome(detection, source_path)
    ocr_finished = False
    try:
        if prepare_service is not None:
            prepare_service()
        extraction_mode = "fullPage" if force else "mixed"
        if detection.total_pages >= CHUNK_THRESHOLD:
            workspace = workspace_dir or Path(__file__).resolve().parent
            if executable is None:
                settings = load_settings(workspace / "umi-ocr.local.json")
                executable = find_umi_executable(workspace, settings.executable)
            if executable is None:
                raise UmiOcrError("大型 PDF 并行 OCR 需要在“OCR 设置”中选择完整的 Umi-OCR 便携版主程序。")
            def engine_status(message: str) -> None:
                if on_progress is not None:
                    from umi_ocr_client import OcrProgress
                    on_progress(OcrProgress("pool", "preparing", 0, detection.total_pages, message=message))
            with UmiOcrPool(executable, workspace, cancel_event=cancel_event, on_status=engine_status) as pool:
                ocr_pdf_in_chunks(source_path, destination_path, pool.clients,
                    cache_dir=workspace / ".ocr-cache" / "jobs",
                    extraction_mode=extraction_mode, on_progress=on_progress,
                    cancel_event=cancel_event, engine_key=pool.engine_key)
        else:
            client.ocr_pdf(source_path, destination_path, extraction_mode=extraction_mode,
                           on_progress=on_progress, cancel_event=cancel_event)
        ocr_finished = True
        if cancel_event is not None and cancel_event.is_set():
            raise OcrCancelledError("OCR 已取消。")
        ocr_detection = analyze_pdf(destination_path, mode="text")
        if ocr_detection.total_pages != detection.total_pages:
            raise UmiOcrError("OCR 结果页数与原始 PDF 不一致，无法安全使用章节页码。")
        ocr_detection = replace(
            ocr_detection,
            method="ocr" if ocr_detection.markers else "none",
            markers=[replace(marker, source="ocr") for marker in ocr_detection.markers],
        )
        return AnalysisOutcome(ocr_detection, destination_path, True)
    except OcrCancelledError:
        raise
    except Exception as exc:
        completed_ocr_path = destination_path if ocr_finished and destination_path.is_file() else None
        raise OcrAnalysisError(str(exc), detection, completed_ocr_path) from exc
