from __future__ import annotations

import json

import shutil
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess
import sys
from unittest.mock import Mock, patch

from umi_ocr_client import DEFAULT_UMI_OCR_URL, OcrCancelledError, UmiOcrError
from umi_ocr_service import OcrSettings, ensure_umi_service, find_umi_executable, load_settings, save_settings


class UmiOcrServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / "_fixtures" / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        workspace = Path(__file__).resolve().parent
        self.assertTrue(self.root.resolve().is_relative_to(workspace))
        shutil.rmtree(self.root)

    def test_settings_round_trip_remembers_executable_and_local_port(self) -> None:
        executable = self.root / "Umi-OCR.exe"
        executable.touch()
        settings = OcrSettings(str(executable.resolve()), "http://127.0.0.1:1555", False, True)
        config = self.root / "umi-ocr.local.json"
        save_settings(config, settings)
        self.assertEqual(load_settings(config), settings)

    def test_missing_or_corrupt_settings_uses_defaults(self) -> None:
        config = self.root / "umi-ocr.local.json"
        self.assertEqual(load_settings(config).base_url, DEFAULT_UMI_OCR_URL)
        for contents in ("{bad json", "[]", '{"base_url":"https://example.com"}'):
            config.write_text(contents, encoding="utf-8")
            self.assertEqual(load_settings(config), OcrSettings())

    def test_discovery_finds_portable_distribution_under_tools(self) -> None:
        executable = self.root / "tools" / "Umi-OCR_Rapid_v2.1.5" / "Umi-OCR.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.assertEqual(find_umi_executable(self.root), executable.resolve())

    def test_running_service_is_reused_without_launching(self) -> None:
        client = Mock()
        client.is_available.return_value = True
        with patch("umi_ocr_service.subprocess.Popen") as launch:
            ensure_umi_service(client, None)
        launch.assert_not_called()

    def test_missing_executable_explains_first_time_configuration(self) -> None:
        client = Mock()
        client.is_available.return_value = False
        with self.assertRaisesRegex(UmiOcrError, "OCR 设置"):
            ensure_umi_service(client, None)

    def test_launches_hidden_and_waits_for_http_service(self) -> None:
        executable = self.root / "Umi-OCR.exe"
        executable.touch()
        client = Mock()
        client.is_available.side_effect = [False, False, True]
        with patch("umi_ocr_service.subprocess.Popen") as launch:
            ensure_umi_service(client, executable, sleep=lambda _seconds: None)
        self.assertEqual(launch.call_args.args[0], [str(executable.resolve()), "--hide"])
        self.assertEqual(launch.call_args.kwargs["cwd"], str(executable.resolve().parent))

    def test_unavailable_service_after_launch_has_bounded_timeout(self) -> None:
        executable = self.root / "Umi-OCR.exe"
        executable.touch()
        client = Mock()
        client.is_available.return_value = False
        with patch("umi_ocr_service.subprocess.Popen"), self.assertRaisesRegex(UmiOcrError, "HTTP"):
            ensure_umi_service(client, executable, startup_timeout=0)

    def test_cancelled_startup_does_not_launch_any_process(self) -> None:
        client = Mock()
        cancel_event = threading.Event()
        cancel_event.set()
        with patch("umi_ocr_service.subprocess.Popen") as launch, self.assertRaises(OcrCancelledError):
            ensure_umi_service(client, None, cancel_event=cancel_event)
        launch.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows process enumeration")
    def test_stale_umi_processes_are_cleaned_before_hidden_restart(self) -> None:
        executable = self.root / "Umi-OCR.exe"
        executable.touch()
        client = Mock()
        client.is_available.side_effect = [False, True]
        tasklist = CompletedProcess(
            ["tasklist"], 0,
            stdout='"Umi-OCR.exe","101","Console","1","10,000 K"\n"Umi-OCR.exe","202","Console","1","10,000 K"\n',
            stderr="",
        )
        with patch("umi_ocr_service.subprocess.run", return_value=tasklist) as run, patch(
            "umi_ocr_service.subprocess.Popen"
        ) as launch, patch("umi_ocr_service.process_executable_path", return_value=executable.resolve()):
            ensure_umi_service(client, executable, sleep=lambda _seconds: None)
        taskkill_calls = [call for call in run.call_args_list if call.args and call.args[0][0] == "taskkill"]
        self.assertEqual(
            [call.args[0] for call in taskkill_calls],
            [["taskkill", "/PID", "101", "/T", "/F"], ["taskkill", "/PID", "202", "/T", "/F"]],
        )
        launch.assert_called_once()


    def test_stale_cleanup_ignores_other_portable_installations(self) -> None:
        executable = self.root / "Umi-OCR.exe"
        executable.touch()
        foreign = self.root / "other" / "Umi-OCR.exe"
        client = Mock()
        client.is_available.side_effect = [False, True]
        with patch("umi_ocr_service._running_umi_process_ids", return_value=["333"]), patch(
            "umi_ocr_service.process_executable_path", return_value=foreign,
        ), patch("umi_ocr_service.subprocess.run") as stop, patch("umi_ocr_service.subprocess.Popen"):
            ensure_umi_service(client, executable)
        stop.assert_not_called()

    def test_old_settings_load_with_four_worker_limit(self) -> None:
        config = self.root / 'umi-ocr.local.json'
        config.write_text('{"auto_ocr":false}', encoding='utf-8')
        settings = load_settings(config)
        self.assertEqual(settings.max_workers, 4)
        self.assertFalse(settings.auto_ocr)

    def test_manual_worker_limit_round_trips_and_bad_values_fall_back(self) -> None:
        config = self.root / 'umi-ocr.local.json'
        settings = OcrSettings(max_workers=7)
        save_settings(config, settings)
        self.assertEqual(load_settings(config), settings)
        for value in (-1, 13, True, '7'):
            config.write_text(json.dumps({'max_workers': value}), encoding='utf-8')
            self.assertEqual(load_settings(config).max_workers, 4)

    def test_explicit_automatic_mode_remains_available_after_default_changes(self) -> None:
        config = self.root / 'umi-ocr.local.json'
        save_settings(config, OcrSettings(max_workers=0))
        self.assertEqual(load_settings(config).max_workers, 0)
