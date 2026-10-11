from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from umi_ocr_client import OcrCancelledError, UmiOcrClient, UmiOcrError
from umi_ocr_service import process_executable_path


_MUTABLE_NAMES = {".settings", ".pre_settings", "temp_doc", "logs", "__pycache__"}


def _link_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def prepare_worker_runtime(executable: Path, destination: Path, *, port: int, threads: int) -> Path:
    """Share immutable portable assets, but give every engine private mutable state."""
    source = executable.resolve().parent
    if not (source / "UmiOCR-data").is_dir():
        raise UmiOcrError("并行 OCR 需要包含 UmiOCR-data 的完整 Umi-OCR 便携版。")
    if destination.exists():
        raise UmiOcrError("并行引擎目录已存在，无法覆盖。")
    def ignore_assets(directory: str, names: list[str]) -> list[str]:
        return [name for name in names if name in _MUTABLE_NAMES
                or (Path(directory).resolve() == source and name not in {executable.name, "UmiOCR-data"})]
    shutil.copytree(source, destination, copy_function=_link_or_copy, ignore=ignore_assets)
    data = destination / "UmiOCR-data"
    config_path = source / "UmiOCR-data" / ".settings"
    config = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else "[Global]\n"
    overrides = {"server.enable": "true", "server.host": "127.0.0.1", "server.port": str(port),
                 "window.startupInvisible": "true", "window.hideTrayIcon": "true",
                 "shortcut.startup": "false", "shortcut.desktop": "false", "shortcut.startMenu": "false",
                 "logs.saveLogLevel": "ERROR"}
    api = re.search(r"(?m)^ocr.api=([^\r\n]+)", config)
    if api:
        overrides[f"ocr.{api.group(1)}.numThread"] = str(max(1, threads))
    for key, value in overrides.items():
        pattern = re.compile(r"(?m)^" + re.escape(key) + r"=[^\r\n]*")
        if pattern.search(config):
            config = pattern.sub(lambda _match: f"{key}={value}", config)
        else:
            config = config.replace("[Global]", f"[Global]\n{key}={value}", 1)
    (data / ".settings").write_text(config, encoding="utf-8")
    (data / ".pre_settings").write_text(json.dumps({"i18n": "zh_CN", "opengl": "AA_UseOpenGLES",
        "server_port": port, "last_pid": -1, "last_ptime": -1}), encoding="utf-8")
    return destination / executable.name


def scoped_stop_worker(process_id: int, executable: Path) -> bool:
    if process_executable_path(process_id) != executable.resolve():
        return False
    try:
        result = subprocess.run(["taskkill", "/PID", str(process_id), "/T", "/F"],
                                capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _free_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UmiOcrPool:
    """Two hidden, independently configured OCR instances owned by one job."""
    def __init__(self, executable: Path, workspace: Path, *, workers: int = 2,
                 cancel_event: threading.Event | None = None,
                 on_status: Callable[[str], None] | None = None) -> None:
        self.executable = executable.resolve()
        self.workspace = workspace.resolve()
        self.worker_count = max(1, min(workers, 2, (os.cpu_count() or 2)))
        self.cancel = cancel_event
        self.on_status = on_status
        self.run_dir = self.workspace / ".ocr-cache" / "engines" / uuid.uuid4().hex
        self.clients: list[UmiOcrClient] = []
        self.executables: list[Path] = []
        self.logs: list[object] = []
        self.launchers: list[subprocess.Popen] = []
        self.engine_key = self._engine_fingerprint()

    def _engine_fingerprint(self) -> str:
        config = self.executable.parent / "UmiOCR-data" / ".settings"
        text = config.read_text(encoding="utf-8-sig") if config.exists() else ""
        settings = sorted(line for line in text.splitlines() if line.startswith("ocr.") and ".numThread=" not in line)
        identity = json.dumps({"binary": self.executable.stat().st_mtime_ns, "settings": settings}, sort_keys=True)
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _check_cancelled(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise OcrCancelledError("准备并行 OCR 引擎时已取消。")

    def __enter__(self) -> "UmiOcrPool":
        self.run_dir.mkdir(parents=True)
        try:
            threads = max(1, min(6, (os.cpu_count() or 2) // self.worker_count))
            for index in range(self.worker_count):
                self._check_cancelled()
                port = _free_local_port()
                if self.on_status:
                    self.on_status(f"正在准备并行 OCR 引擎 {index + 1}/{self.worker_count}……")
                executable = prepare_worker_runtime(self.executable, self.run_dir / f"worker_{index + 1}", port=port, threads=threads)
                self.executables.append(executable)
                client = UmiOcrClient(f"http://127.0.0.1:{port}", timeout=120)
                self.clients.append(client)
                log = (executable.parent / "startup.log").open("wb")
                self.logs.append(log)
                process = subprocess.Popen([str(executable), "--hide"], cwd=str(executable.parent),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                self.launchers.append(process)
            deadline = time.monotonic() + 60
            pending = set(range(len(self.clients)))
            while pending:
                self._check_cancelled()
                for index in list(pending):
                    state_file = self.executables[index].parent / "UmiOCR-data" / ".pre_settings"
                    try:
                        state = json.loads(state_file.read_text(encoding="utf-8"))
                        actual_port = int(state["server_port"])
                        if (1024 <= actual_port <= 65535
                            and process_executable_path(int(state["last_pid"])) == self.executables[index].resolve()
                            and self.clients[index].base_url != f"http://127.0.0.1:{actual_port}"):
                            self.clients[index] = UmiOcrClient(f"http://127.0.0.1:{actual_port}", timeout=120)
                    except (OSError, ValueError, KeyError):
                        pass
                    if self.clients[index].is_available(timeout=0.5):
                        pending.remove(index)
                if not pending:
                    break
                if time.monotonic() >= deadline:
                    raise UmiOcrError(f"独立 OCR 引擎启动失败，未就绪引擎：{sorted(index + 1 for index in pending)}；日志：{self.run_dir}")
                if self.cancel is not None:
                    self.cancel.wait(0.25)
                else:
                    time.sleep(0.25)
            if len({client.base_url for client in self.clients}) != len(self.clients):
                raise UmiOcrError("独立 OCR 引擎端口重复，已停止并行任务。")
            return self
        except BaseException:
            self.close(remove_runtime=False)
            raise

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self, *, remove_runtime: bool = True) -> None:
        # Quit only the private copies owned by this pool. The normal 1224 service stays intact.
        for executable, client in zip(self.executables, self.clients):
            try:
                state = json.loads((executable.parent / "UmiOCR-data" / ".pre_settings").read_text(encoding="utf-8"))
                if (process_executable_path(int(state["last_pid"])) == executable.resolve()
                    and client.is_available(timeout=0.5)):
                    client._request_bytes("POST", "/argv", data=b'["--quit"]',
                                          headers={"Content-Type": "application/json"}, timeout=2)
            except (OSError, ValueError, KeyError, UmiOcrError):
                pass
        for executable in self.executables:
            state = executable.parent / "UmiOCR-data" / ".pre_settings"
            try:
                pid = int(json.loads(state.read_text(encoding="utf-8"))["last_pid"])
                deadline = time.monotonic() + 3
                while process_executable_path(pid) == executable.resolve() and time.monotonic() < deadline:
                    time.sleep(0.1)
                if process_executable_path(pid) == executable.resolve():
                    scoped_stop_worker(pid, executable)
            except (OSError, ValueError, KeyError):
                pass
        for log in self.logs:
            log.close()
        self.logs.clear()
        # Removing private runtime links does not remove the original portable installation.
        if remove_runtime and self.run_dir.resolve().is_relative_to(self.workspace / ".ocr-cache" / "engines"):
            try:
                shutil.rmtree(self.run_dir)
            except OSError:
                pass
