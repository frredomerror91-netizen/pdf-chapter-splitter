from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from umi_ocr_pool import UmiOcrPool, prepare_worker_runtime, scoped_stop_worker


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

    def test_an_engine_that_exits_during_startup_is_retried_once(self) -> None:
        first = Mock(pid=101)
        first.poll.return_value = 1
        second = Mock(pid=102)
        second.poll.return_value = None
        client = Mock()
        client.is_available.side_effect = [False, True]
        statuses = []
        with patch('umi_ocr_pool.subprocess.Popen', side_effect=[first, second]) as launch, patch(
            'umi_ocr_pool.UmiOcrClient', return_value=client
        ), patch('umi_ocr_pool.process_executable_path', return_value=None), patch(
            'umi_ocr_pool.scoped_stop_worker', return_value=True
        ), patch('umi_ocr_pool.time.sleep'), patch('umi_ocr_pool.time.monotonic', side_effect=[0, 1, 2, 3]):
            with UmiOcrPool(self.executable, self.root, workers=1, on_status=statuses.append) as pool:
                self.assertEqual(len(pool.clients), 1)
        self.assertEqual(launch.call_count, 2)
        self.assertTrue(any('重试' in message for message in statuses))

    def test_repeated_startup_exit_fails_without_infinite_relaunches(self) -> None:
        exited = Mock(pid=101)
        exited.poll.return_value = 1
        client = Mock()
        client.is_available.return_value = False
        with patch('umi_ocr_pool.subprocess.Popen', return_value=exited) as launch, patch(
            'umi_ocr_pool.UmiOcrClient', return_value=client
        ), patch('umi_ocr_pool.process_executable_path', return_value=None), patch(
            'umi_ocr_pool.time.sleep'
        ), patch('umi_ocr_pool.time.monotonic', side_effect=[0, 1, 61, 62]), self.assertRaisesRegex(Exception, '启动失败'):
            with UmiOcrPool(self.executable, self.root, workers=1):
                pass
        self.assertEqual(launch.call_count, 2)
