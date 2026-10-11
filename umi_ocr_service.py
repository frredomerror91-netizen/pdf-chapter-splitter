from __future__ import annotations

import json
import csv
import io
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from umi_ocr_client import DEFAULT_UMI_OCR_URL, OcrCancelledError, UmiOcrClient, UmiOcrError, validate_local_url


@dataclass(frozen=True)
class OcrSettings:
    executable: str = ""
    base_url: str = DEFAULT_UMI_OCR_URL
    auto_ocr: bool = True
    dark_mode: bool = False


class UmiExecutableNotFoundError(UmiOcrError):
    pass


def load_settings(path: Path) -> OcrSettings:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return OcrSettings()
        base_url = validate_local_url(data.get("base_url", DEFAULT_UMI_OCR_URL))
        executable = data.get("executable", "")
        auto_ocr = data.get("auto_ocr", True)
        dark_mode = data.get("dark_mode", False)
        if not isinstance(executable, str) or not isinstance(auto_ocr, bool) or not isinstance(dark_mode, bool):
            return OcrSettings()
        return OcrSettings(executable, base_url, auto_ocr, dark_mode)
    except (OSError, ValueError, TypeError, AttributeError, UmiOcrError):
        return OcrSettings()


def save_settings(path: Path, settings: OcrSettings) -> None:
    validate_local_url(settings.base_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def find_umi_executable(project_dir: Path, configured_path: str = "") -> Path | None:
    candidates = [Path(configured_path)] if configured_path else []
    candidates.extend([
        project_dir / "tools" / "Umi-OCR" / "Umi-OCR.exe",
        project_dir / "Umi-OCR" / "Umi-OCR.exe",
        project_dir / "vendor" / "Umi-OCR" / "Umi-OCR.exe",
    ])
    for parent in (project_dir / "tools", project_dir):
        candidates.extend(sorted(parent.glob("Umi-OCR*/Umi-OCR.exe")))
    on_path = shutil.which("Umi-OCR.exe")
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.is_file() and candidate.name.lower() == "umi-ocr.exe":
            return candidate.resolve()
    return None


def _running_umi_process_ids() -> list[str]:
    """Return Umi-OCR PIDs on Windows without assuming a Python psutil install."""

    if sys.platform != "win32":
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Umi-OCR.exe", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    process_ids: list[str] = []
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if len(row) >= 2 and row[0].casefold() == "umi-ocr.exe" and row[1].isdigit():
            process_ids.append(row[1])
    return process_ids



def process_executable_path(process_id: int) -> Path | None:
    """Query the executable of one PID; never infer identity only from its name."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, process_id)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).resolve()
        return None
    finally:
        kernel.CloseHandle(handle)


def stop_stale_umi_processes(executable: Path | None = None) -> list[str]:
    """Stop same-name Umi-OCR processes when its HTTP endpoint is unavailable."""

    stopped: list[str] = []
    for process_id in _running_umi_process_ids():
        if executable is not None and process_executable_path(int(process_id)) != executable.resolve():
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", process_id, "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                encoding="mbcs",
                errors="replace",
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stopped.append(process_id)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return stopped


def ensure_umi_service(
    client: UmiOcrClient,
    executable: Path | None,
    *,
    startup_timeout: float = 45.0,
    sleep: Callable[[float], None] = time.sleep,
    cancel_event: threading.Event | None = None,
) -> None:
    def check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise OcrCancelledError("OCR 已取消。")

    check_cancelled()
    if client.is_available():
        return
    check_cancelled()
    if executable is None or not executable.is_file():
        raise UmiExecutableNotFoundError("未找到 Umi-OCR。请打开“OCR 设置”选择 Umi-OCR.exe，或将便携版放入 tools/Umi-OCR。")
    executable = executable.resolve()
    stale_pids = stop_stale_umi_processes(executable)
    try:
        subprocess.Popen(
            [str(executable), "--hide"],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        raise UmiOcrError(f"无法启动 Umi-OCR：{exc}") from exc

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        check_cancelled()
        if client.is_available(timeout=1.0):
            return
        if cancel_event is None:
            sleep(0.5)
        elif cancel_event.wait(0.5):
            raise OcrCancelledError("OCR 已取消。")
    raise UmiOcrError(
        (f"发现残留 Umi-OCR 进程（{', '.join(stale_pids)}），已尝试清理但 HTTP 服务仍未启动。" if stale_pids else "Umi-OCR 的 HTTP 服务未能启动。")
        + "请在 Umi-OCR 的全局设置中开启 HTTP 服务，"
        "选择“仅本地”，并确认端口与“OCR 设置”一致（默认 1224）。文档识别需要 v2.1.4 或更高版本。"
    )
