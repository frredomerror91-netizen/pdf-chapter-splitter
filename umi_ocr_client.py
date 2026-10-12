from __future__ import annotations

import json
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pypdf import PdfReader


DEFAULT_UMI_OCR_URL = "http://127.0.0.1:1224"
SUCCESS_CODE = 100


class UmiOcrError(RuntimeError):
    """An expected failure while communicating with Umi-OCR."""


class OcrCancelledError(UmiOcrError):
    """The caller cancelled an OCR task."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise UmiOcrError("Umi-OCR 本地接口返回了不支持的重定向。")


def validate_local_url(value: str) -> str:
    """Only accept a plain HTTP origin on the local computer."""

    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UmiOcrError("Umi-OCR 服务端口无效。") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        raise UmiOcrError("OCR 地址必须是本地 HTTP 服务，例如 http://127.0.0.1:1224。")
    return value


@dataclass(frozen=True)
class OcrProgress:
    task_id: str
    status: str
    processed_pages: int = 0
    total_pages: int = 0
    is_done: bool = False
    message: str | None = None
    completed_chunks: int = 0
    total_chunks: int = 0
    prepared_pages: int = 0
    prepared_chunks: int = 0
    total_workers: int = 0
    active_workers: int = 0


ProgressCallback = Callable[[OcrProgress], None]


class UmiOcrClient:
    """Small standard-library client for Umi-OCR's local document API."""

    def __init__(
        self,
        base_url: str = DEFAULT_UMI_OCR_URL,
        *,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = validate_local_url(base_url)
        self.timeout = timeout
        self._sleep = sleep
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirects()).open

    def is_available(self, timeout: float = 1.5) -> bool:
        """Return whether the local Umi-OCR HTTP service answers normally."""

        try:
            options = self._request_json("GET", "/api/doc/get_options", timeout=timeout)
        except (OSError, UmiOcrError):
            return False
        return isinstance(options.get("doc.extractionMode"), dict)

    def ocr_pdf(
        self,
        source_path: Path,
        destination_path: Path,
        *,
        extraction_mode: str = "mixed",
        poll_interval: float = 0.5,
        timeout: float | None = None,
        inactivity_timeout: float = 900.0,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        """OCR one PDF into a searchable PDF and always request task cleanup."""

        if source_path.resolve() == destination_path.resolve():
            raise UmiOcrError("OCR 输出不能覆盖原始 PDF。")
        self._check_cancelled(cancel_event)
        task_id = self.upload_pdf(source_path, extraction_mode=extraction_mode)
        try:
            self.wait_for_completion(
                task_id,
                poll_interval=poll_interval,
                timeout=timeout,
                inactivity_timeout=inactivity_timeout,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            self._check_cancelled(cancel_event)
            return self.download_layered_pdf(task_id, destination_path)
        finally:
            try:
                self.clear_task(task_id)
            except (OSError, UmiOcrError):
                # Cleanup must not hide a successful OCR result or its primary error.
                pass

    def upload_pdf(self, source_path: Path, *, extraction_mode: str = "mixed") -> str:
        if not source_path.is_file():
            raise UmiOcrError(f"找不到待 OCR 的 PDF：{source_path}")

        boundary = f"----PdfChapterSplitter{uuid.uuid4().hex}"
        prefix, suffix = self._multipart_parts(source_path, extraction_mode, boundary)
        length = len(prefix) + source_path.stat().st_size + len(suffix)

        def upload_chunks() -> Iterable[bytes]:
            yield prefix
            with source_path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    yield block
            yield suffix

        response = self._request_json(
            "POST",
            "/api/doc/upload",
            data=upload_chunks(),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(length),
            },
        )
        task_id = self._extract_task_id(response.get("data"))
        if not task_id:
            raise UmiOcrError("Umi-OCR 没有返回有效的任务编号。")
        return task_id

    def wait_for_completion(
        self,
        task_id: str,
        *,
        poll_interval: float = 0.5,
        timeout: float | None = None,
        inactivity_timeout: float = 900.0,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> OcrProgress:
        deadline = time.monotonic() + timeout if timeout is not None else None
        last_advance = time.monotonic()
        best_pages = -1
        while True:
            self._check_cancelled(cancel_event)
            response = self._request_json("POST", "/api/doc/result", {"id": task_id})
            progress = self._progress_from_response(task_id, response)
            now = time.monotonic()
            if progress.processed_pages > best_pages:
                best_pages = progress.processed_pages
                last_advance = now
            if on_progress is not None:
                on_progress(progress)

            if progress.status in {"failure", "error", "failed", "cancelled", "canceled"}:
                raise UmiOcrError(progress.message or f"Umi-OCR 任务失败：{progress.status}")
            if progress.is_done:
                if progress.status == "success":
                    return progress
                raise UmiOcrError(progress.message or "Umi-OCR 任务结束但没有成功完成。")

            remaining = inactivity_timeout - (now - last_advance)
            if remaining <= 0:
                raise UmiOcrError(f"Umi-OCR 连续 {int(inactivity_timeout)} 秒无新增页面进展，请检查引擎并重试。")
            if deadline is not None:
                hard_remaining = deadline - now
                if hard_remaining <= 0:
                    raise UmiOcrError(f"Umi-OCR 处理超时（{int(timeout)} 秒）。")
                remaining = min(remaining, hard_remaining)
            delay = min(max(poll_interval, 0.05), remaining)
            if cancel_event is None:
                self._sleep(delay)
            elif cancel_event.wait(delay):
                raise OcrCancelledError("OCR 已取消。")

    def download_layered_pdf(self, task_id: str, destination_path: Path) -> Path:
        response = self._request_json(
            "POST",
            "/api/doc/download",
            {"id": task_id, "file_types": ["pdfLayered"], "ignore_blank": False},
        )
        target = self._extract_download_target(response.get("data"))
        if not target:
            raise UmiOcrError("Umi-OCR 没有返回 OCR PDF 下载地址。")

        download_url = self._local_request_url(target)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            request = Request(download_url, method="GET")
            with self._opener(request, timeout=self.timeout) as response, tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination_path.stem}_",
                suffix=".tmp",
                dir=destination_path.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                while block := response.read(1024 * 1024):
                    stream.write(block)
            try:
                with temporary_path.open("rb") as downloaded:
                    if not downloaded.read(5) == b"%PDF-":
                        raise ValueError("PDF 文件头无效")
                reader = PdfReader(temporary_path)
                if reader.is_encrypted or not reader.pages:
                    raise ValueError("PDF 没有可读取页面")
            except Exception as exc:
                raise UmiOcrError("Umi-OCR 下载结果不是有效的未加密 PDF。") from exc
            temporary_path.replace(destination_path)
        except (HTTPError, URLError, OSError) as exc:
            raise UmiOcrError(f"Umi-OCR PDF 下载失败：{exc}") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return destination_path

    def clear_task(self, task_id: str) -> None:
        self._request_json("GET", f"/api/doc/clear/{quote(task_id, safe='')}")

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        payload: dict[str, Any] | None = None,
        *,
        data: bytes | Iterable[bytes] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_data = data
        request_headers = dict(headers or {})
        if payload is not None:
            request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request_headers.setdefault("Accept", "application/json")
        body = self._request_bytes(
            method,
            path_or_url,
            data=request_data,
            headers=request_headers,
            timeout=timeout,
        )
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UmiOcrError("Umi-OCR 返回了无法解析的响应。") from exc
        if not isinstance(parsed, dict):
            raise UmiOcrError("Umi-OCR 返回的数据格式不正确。")
        code = parsed.get("code")
        if path_or_url != "/api/doc/get_options" and code != SUCCESS_CODE:
            message = parsed.get("message") or parsed.get("msg") or parsed.get("error") or parsed.get("data")
            raise UmiOcrError(str(message or f"Umi-OCR 返回错误码：{code}"))
        return parsed

    def _request_bytes(
        self,
        method: str,
        path_or_url: str,
        *,
        data: bytes | Iterable[bytes] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> bytes:
        url = self._local_request_url(path_or_url)
        request = Request(url, data=data, headers=headers or {}, method=method)
        try:
            with self._opener(request, timeout=timeout or self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            suffix = f"：{detail}" if detail else ""
            raise UmiOcrError(f"Umi-OCR HTTP {exc.code}{suffix}") from exc
        except (URLError, OSError) as exc:
            raise UmiOcrError(f"无法连接 Umi-OCR：{exc}") from exc

    def _local_request_url(self, target: str) -> str:
        url = urljoin(f"{self.base_url}/", target)
        expected = urlparse(self.base_url)
        actual = urlparse(url)
        if (
            actual.scheme != expected.scheme
            or actual.hostname != expected.hostname
            or actual.port != expected.port
            or actual.username is not None
            or actual.password is not None
        ):
            raise UmiOcrError("OCR 下载地址必须属于同一个本地服务。")
        return actual._replace(
            path=quote(actual.path, safe="/%"),
            query=quote(actual.query, safe="=&%"),
        ).geturl()

    @staticmethod
    def _multipart_parts(source_path: Path, extraction_mode: str, boundary: str) -> tuple[bytes, bytes]:
        filename = source_path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("utf-8")
        options = json.dumps({"doc.extractionMode": extraction_mode}, ensure_ascii=False)
        suffix = (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="json"\r\n\r\n'
            f"{options}\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        return prefix, suffix

    @staticmethod
    def _check_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise OcrCancelledError("OCR 已取消。")

    @staticmethod
    def _extract_task_id(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("id", "task_id", "taskId"):
                candidate = value.get(key)
                if candidate:
                    return str(candidate)
        return None

    @staticmethod
    def _extract_download_target(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("url", "path", "file"):
                candidate = value.get(key)
                if candidate:
                    return str(candidate)
        if isinstance(value, list) and value:
            return UmiOcrClient._extract_download_target(value[0])
        return None

    @staticmethod
    def _progress_from_response(task_id: str, response: dict[str, Any]) -> OcrProgress:
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        source = {**data, **response}
        status = str(source.get("state") or source.get("status") or "running").lower()
        is_done = source.get("is_done") is True
        processed = UmiOcrClient._as_int(source.get("processed_count"))
        total = UmiOcrClient._as_int(source.get("pages_count") or source.get("total_count"))
        message = source.get("message") or source.get("msg") or source.get("error")
        return OcrProgress(task_id, status, processed, total, is_done, str(message) if message else None)

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0
