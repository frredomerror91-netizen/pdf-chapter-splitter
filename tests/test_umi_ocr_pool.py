from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from umi_ocr_pool import prepare_worker_runtime, scoped_stop_worker


class WorkerIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / "_fixtures" / self._testMethodName
        self.source = self.root / "portable"
        self.destination = self.root / "worker"
        data = self.source / "UmiOCR-data"
        data.mkdir(parents=True, exist_ok=True)
        self.executable = self.source / "Umi-OCR.exe"
        self.executable.write_bytes(b"executable placeholder")
        (data / "models").mkdir()
        (data / "models" / "weights.bin").write_bytes(b"immutable model weights")
        (data / ".pre_settings").write_text('{"last_pid":999,"server_port":1224}')
        (data / ".settings").write_text("[Global]\nserver.enable=false\nserver.port=1224\nwindow.startupInvisible=false\nocr.api=win7_x64_RapidOCR-json\nocr.win7_x64_RapidOCR-json.numThread=12\n", encoding="utf-8")
        (data / "temp_doc").mkdir()
        (data / "temp_doc" / "private_book.pdf").write_bytes(b"do not copy active jobs")

    def tearDown(self) -> None:
        self.assertTrue(self.root.resolve().is_relative_to(Path(__file__).resolve().parent))
        shutil.rmtree(self.root)

    def test_each_worker_has_its_own_port_settings_pid_and_temp_directory(self) -> None:
        worker = prepare_worker_runtime(self.executable, self.destination, port=24001, threads=3)
        self.assertTrue(worker.is_file())
        data = worker.parent / "UmiOCR-data"
        config = (data / ".settings").read_text(encoding="utf-8")
        self.assertIn("server.port=24001", config)
        self.assertIn("server.enable=true", config)
        self.assertIn("window.startupInvisible=true", config)
        self.assertIn("numThread=3", config)
        self.assertNotIn("private_book.pdf", [file.name for file in data.rglob("*")])
        state = json.loads((data / ".pre_settings").read_text(encoding="utf-8"))
        self.assertEqual(state["last_pid"], -1)
        self.assertEqual(state["server_port"], 24001)
        self.assertEqual((data / "models" / "weights.bin").read_bytes(), b"immutable model weights")
        self.assertIn("server.port=1224", (self.source / "UmiOCR-data" / ".settings").read_text())

    def test_cleanup_never_terminates_a_different_installation(self) -> None:
        with patch("umi_ocr_pool.process_executable_path", return_value=Path("C:/unrelated/Umi-OCR.exe")), patch(
            "umi_ocr_pool.subprocess.run",
        ) as terminate:
            scoped_stop_worker(123, self.executable)
        terminate.assert_not_called()
