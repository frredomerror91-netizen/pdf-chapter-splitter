from __future__ import annotations

import gc
import json
import shutil
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from pdf_chapter_splitter_core import ChapterRange
from split_pdf_by_chapter import PdfChapterSplitterApp
from umi_ocr_service import OcrSettings, UmiExecutableNotFoundError, save_settings
from umi_ocr_client import OcrProgress
from tests.test_umi_ocr_client import _FakeUmiOcrServer


class PdfChapterSplitterAppTests(unittest.TestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.window = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.window.withdraw()
        self.root = Path(__file__).parent / "_fixtures" / self._testMethodName
        self.root.mkdir(parents=True, exist_ok=True)
        self.server = _FakeUmiOcrServer()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        save_settings(self.root / "umi-ocr.local.json", OcrSettings(base_url=f"http://127.0.0.1:{self.server.server_port}"))
        self.app = PdfChapterSplitterApp(self.window, project_dir=self.root)
        self.source = self.root / "original.pdf"
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=595, height=842)
        writer.write(self.source)
        self.app.source_path = self.source
        self.app.output_var.set(str(self.root / "output"))
        self.app.open_after_var.set(False)

    def tearDown(self) -> None:
        if not hasattr(self, "app"):
            return
        if self.app._busy:
            self.app.cancel_processing()
            self.wait_until_idle()
        if not self.app._closed:
            self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        # Tk objects must be released by the main test thread, before any OCR thread can trigger GC.
        self.app = None
        self.window = None
        gc.collect()
        self.assertTrue(self.root.resolve().is_relative_to(Path(__file__).resolve().parent))
        shutil.rmtree(self.root)

    def wait_until_idle(self) -> None:
        deadline = time.monotonic() + 6
        while self.app._busy and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.01)
        self.assertFalse(self.app._busy, "Background processing did not finish")

    def test_analyze_button_automatically_ocrd_scanned_pdf_and_exported_searchable_result(self) -> None:
        self.app.analyze_button.invoke()
        self.assertTrue(self.app._busy)
        self.assertEqual(str(self.app.choose_button["state"]), "disabled")
        self.wait_until_idle()
        self.assertEqual(self.app.method_var.get(), "OCR 页面标题")
        self.assertEqual([item.start_page for item in self.app.ranges], [1, 3])
        self.assertNotEqual(self.app._processing_path, self.source)
        cached = self.app._processing_path
        with patch("split_pdf_by_chapter.messagebox.showinfo"):
            self.app.export_button.invoke()
            self.wait_until_idle()
        outputs = sorted((self.root / "output").glob("*.pdf"))
        self.assertEqual(len(outputs), 2)
        self.assertIn("CHAPTER 2", PdfReader(outputs[1]).pages[0].extract_text())
        self.assertEqual(len(PdfReader(self.source).pages), 3)
        self.app.close()
        self.assertTrue(cached.exists(), "OCR output PDF should remain available after closing")

    def test_ocr_failure_still_allows_adding_manual_chapters(self) -> None:
        self.server.result_response = {"code": 100, "state": "failure", "is_done": True, "message": "engine failed"}
        with patch("split_pdf_by_chapter.messagebox.showerror"):
            self.app.analyze()
            self.wait_until_idle()
        self.assertEqual(self.app.total_pages, 3)
        self.assertEqual(self.app.status_tone, "error")
        self.assertIn("engine failed", self.app.notice_var.get())
        with patch("split_pdf_by_chapter.simpledialog.askstring", return_value="Manual chapter"), patch(
            "split_pdf_by_chapter.simpledialog.askinteger", return_value=1,
        ):
            self.app.add_chapter()
        self.assertEqual(self.app.ranges, [ChapterRange(1, "Manual chapter", 1, 3, "manual")])

    def test_forced_ocr_button_handles_a_pdf_with_existing_text(self) -> None:
        pdf = canvas.Canvas(str(self.source))
        for _ in range(3):
            pdf.drawString(72, 720, "Unreadable chapter structure")
            pdf.showPage()
        pdf.save()
        self.app.ocr_button.invoke()
        self.wait_until_idle()
        self.assertIn(b"fullPage", self.server.upload_body)
        self.assertEqual(len(self.app.ranges), 2)

    def test_automatic_ocr_can_be_disabled_and_remembered(self) -> None:
        self.app.auto_ocr_var.set(False)
        self.app.save_ocr_preference()
        self.app.analyze()
        self.wait_until_idle()
        self.assertEqual(self.server.events, [])
        self.assertEqual(self.app.total_pages, 3)
        self.assertFalse(self.app.ocr_settings.auto_ocr)

    def test_cancel_ocr_clears_remote_task_and_restores_controls(self) -> None:
        self.server.result_response = {"code": 100, "state": "running", "is_done": False}
        self.app.analyze()
        deadline = time.monotonic() + 3
        while "result" not in self.server.events and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.01)
        self.app.cancel_processing()
        self.wait_until_idle()
        self.assertEqual(self.server.events[-1], "clear")
        self.assertIn("取消", self.app.status_var.get())
        self.assertEqual(str(self.app.analyze_button["state"]), "normal")

    def test_first_use_path_selection_is_saved_and_analysis_continues_automatically(self) -> None:
        executable = self.root / "Umi-OCR.exe"
        executable.touch()
        with patch(
            "split_pdf_by_chapter.ensure_umi_service",
            side_effect=[UmiExecutableNotFoundError("Please configure OCR"), None],
        ) as prepare, patch(
            "split_pdf_by_chapter.filedialog.askopenfilename", return_value=str(executable),
        ) as choose:
            self.app.analyze_button.invoke()
            self.wait_until_idle()
        self.assertEqual(len(self.app.ranges), 2)
        self.assertEqual(prepare.call_count, 2)
        choose.assert_called_once()
        config = json.loads((self.root / "umi-ocr.local.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(config["executable"]), executable.resolve())

    def test_close_during_ocr_waits_for_task_cleanup(self) -> None:
        self.server.result_response = {"code": 100, "state": "running", "is_done": False}
        self.app.analyze()
        deadline = time.monotonic() + 3
        while "result" not in self.server.events and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.01)
        self.app.close()
        self.wait_until_idle()
        self.assertTrue(self.app._closed)
        self.assertEqual(self.server.events[-1], "clear")

    def test_export_error_returns_to_main_thread_and_restores_controls(self) -> None:
        self.app.total_pages = 3
        self.app.ranges = [ChapterRange(1, "Manual", 1, 3, "manual")]
        with patch("split_pdf_by_chapter.split_pdf", side_effect=OSError("disk full")), patch(
            "split_pdf_by_chapter.messagebox.showerror",
        ) as show_error:
            self.app.export()
            self.wait_until_idle()
        self.assertIn("disk full", str(show_error.call_args))
        self.assertEqual(str(self.app.export_button["state"]), "normal")

    def test_reanalysis_reuses_ocr_pdf_and_keeps_ocr_source_labels(self) -> None:
        self.app.analyze()
        self.wait_until_idle()
        cached = self.app._processing_path
        uploads = self.server.events.count("upload")
        self.app.analyze()
        self.wait_until_idle()
        self.assertEqual(self.server.events.count("upload"), uploads)
        self.assertEqual(self.app._processing_path, cached)
        self.assertTrue(all(item.source == "ocr" for item in self.app.ranges))
        self.assertEqual(self.app.method_var.get(), "OCR 页面标题")

    def test_choosing_pdf_uses_fresh_default_folder_when_previous_results_exist(self) -> None:
        old_output = self.source.with_name(f"{self.source.stem}_章节拆分")
        old_output.mkdir()
        old_file = old_output / "001_旧结果.pdf"
        old_file.write_bytes(b"preserve existing results")
        with patch("split_pdf_by_chapter.filedialog.askopenfilename", return_value=str(self.source)):
            self.app.choose_pdf()
        self.assertEqual(Path(self.app.output_var.get()), old_output.with_name(f"{old_output.name} (2)"))
        self.assertEqual(old_file.read_bytes(), b"preserve existing results")

    def test_dark_mode_rebuilds_the_window_and_persists_the_preference(self) -> None:
        original_button = self.app.analyze_button
        self.app.dark_mode_var.set(True)
        self.app.toggle_theme()
        self.assertIsNot(self.app.analyze_button, original_button)
        self.assertEqual(self.app.ocr_settings.dark_mode, True)
        self.assertEqual(self.app.root.cget("bg"), "#151A21")
        self.assertEqual(str(self.app.analyze_button["state"]), "normal")
        saved = json.loads((self.root / "umi-ocr.local.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["dark_mode"])

    def test_dark_mode_is_loaded_on_the_next_window(self) -> None:
        self.app.dark_mode_var.set(True)
        self.app.toggle_theme()
        old_app = self.app
        old_window = self.window
        old_app.close()
        self.app = None
        self.window = None
        del old_app
        old_window = None
        gc.collect()
        self.window = tk.Tk()
        self.window.withdraw()
        self.app = PdfChapterSplitterApp(self.window, project_dir=self.root)
        self.assertTrue(self.app.dark_mode_var.get())
        self.assertEqual(self.app.root.cget("bg"), "#151A21")


    def test_batch_progress_shows_total_pages_and_completed_chunks(self) -> None:
        self.app._show_ocr_progress(OcrProgress("batch", "running", 143, 657,
                                              completed_chunks=2, total_chunks=14))
        self.assertIn("2/14", self.app.status_var.get())
        self.assertIn("143/657", self.app.status_var.get())
        self.assertEqual(float(self.app.progress["maximum"]), 657)
        self.assertEqual(float(self.app.progress["value"]), 143)

    def test_batch_merge_phase_is_visible_in_status(self) -> None:
        self.app._show_ocr_progress(OcrProgress("batch", "merging", 657, 657,
                                              message="正在按原页序合并", completed_chunks=14, total_chunks=14))
        self.assertIn("合并", self.app.status_var.get())

    def test_preparation_progress_is_visible_before_ocr_starts(self) -> None:
        self.app._show_ocr_progress(OcrProgress('batch', 'splitting', 0, 306,
                                              prepared_pages=17, total_chunks=7, total_workers=7))
        self.assertEqual(self.app.page_count_var.get(), '306')
        self.assertIn('17/306', self.app.status_var.get())
        self.assertIn('0/7', self.app.status_var.get())
        self.assertEqual(float(self.app.progress['value']), 17)

    def test_running_progress_displays_preparation_and_active_instances(self) -> None:
        self.app._show_ocr_progress(OcrProgress('batch', 'splitting', 20, 306,
                                              prepared_pages=100, prepared_chunks=2,
                                              total_chunks=7, total_workers=7, active_workers=3))
        self.assertIn('100/306', self.app.status_var.get())
        self.assertIn('20/306', self.app.status_var.get())
        self.assertIn('3/7', self.app.status_var.get())
        self.assertIn('OCR', self.app.method_var.get())

    def test_worker_limit_can_be_saved_from_settings_dialog(self) -> None:
        self.app.open_ocr_settings()
        dialog = next(child for child in self.window.winfo_children() if isinstance(child, tk.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        spinbox = next(widget for widget in descendants(dialog) if isinstance(widget, ttk.Spinbox))
        spinbox.delete(0, 'end')
        spinbox.insert(0, '7')
        save = next(widget for widget in descendants(dialog)
                    if isinstance(widget, ttk.Button) and widget.cget('text') == '保存设置')
        save.invoke()
        self.assertEqual(self.app.ocr_settings.max_workers, 7)
        saved = json.loads((self.root / 'umi-ocr.local.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['max_workers'], 7)
