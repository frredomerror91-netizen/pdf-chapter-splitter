from __future__ import annotations

import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from umi_ocr_client import UmiOcrClient, UmiOcrError


def make_document_pdf(*, searchable: bool) -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    for text in ("CHAPTER 1: Basics", "Body text", "CHAPTER 2: Parsing"):
        image = Image.new("RGB", (600, 800), "white")
        ImageDraw.Draw(image).text((72, 90), text, fill="black")
        pdf.drawImage(ImageReader(image), 0, 0, width=595, height=842)
        if searchable:
            layer = pdf.beginText(72, 720)
            layer.setTextRenderMode(3)
            layer.textLine(text)
            pdf.drawText(layer)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


class _FakeUmiOcrHandler(BaseHTTPRequestHandler):
    server: "_FakeUmiOcrServer"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _write_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/doc/get_options":
            self._write_json({"doc.extractionMode": {"default": "mixed"}})
            return
        if unquote(path) == self.server.download_path:
            body = self.server.layered_pdf
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/doc/clear/task-1":
            self.server.events.append("clear")
            self._write_json({"code": 100, "data": {}})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if path == "/api/doc/upload":
            self.server.events.append("upload")
            self.server.upload_body = body
            self._write_json(self.server.upload_response)
            return
        if path == "/api/doc/result":
            self.server.events.append("result")
            if self.server.result_response is not None:
                payload = self.server.result_response
            elif self.server.result_calls == 0:
                payload = {
                    "code": 100,
                    "is_done": False,
                    "state": "running",
                    "processed_count": 1,
                    "pages_count": 2,
                }
            else:
                payload = {
                    "code": 100,
                    "is_done": True,
                    "state": "success",
                    "processed_count": 2,
                    "pages_count": 2,
                }
            self.server.result_calls += 1
            self._write_json(payload)
            return
        if path == "/api/doc/download":
            self.server.events.append("download")
            self.server.download_body = body
            url = f"http://127.0.0.1:{self.server.server_port}{self.server.download_path}"
            self._write_json({"code": 100, "data": url})
            return
        self.send_error(404)


class _FakeUmiOcrServer(ThreadingHTTPServer):
    events: list[str]
    upload_body: bytes
    download_body: bytes
    result_calls: int

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeUmiOcrHandler)
        self.events = []
        self.upload_body = b""
        self.download_body = b""
        self.result_calls = 0
        self.download_path = "/download/layered.pdf"
        self.upload_response = {"code": 100, "data": "task-1"}
        self.result_response = None
        self.layered_pdf = make_document_pdf(searchable=True)


class UmiOcrClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _FakeUmiOcrServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp_root = Path(__file__).parent / "_fixtures"
        self.tmp_root.mkdir(exist_ok=True)
        self.source = self.tmp_root / "umi_ocr_client_source.pdf"
        self.destination = self.tmp_root / "umi_ocr_client_output.pdf"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.source.unlink(missing_ok=True)
        self.destination.unlink(missing_ok=True)

    def test_ocr_pdf_uploads_polls_downloads_and_clears_task(self) -> None:
        client = UmiOcrClient(
            f"http://127.0.0.1:{self.server.server_port}",
            sleep=lambda _seconds: None,
        )
        self.assertTrue(client.is_available())
        progress: list[object] = []

        self.source.write_bytes(b"%PDF-source")

        result = client.ocr_pdf(
            self.source,
            self.destination,
            poll_interval=0,
            on_progress=progress.append,
        )

        self.assertEqual(result, self.destination)
        self.assertEqual(self.destination.read_bytes(), self.server.layered_pdf)
        self.assertIn(b"umi_ocr_client_source.pdf", self.server.upload_body)
        self.assertIn(b"pdfLayered", self.server.download_body)
        self.assertEqual(self.server.events, ["upload", "result", "result", "download", "clear"])
        self.assertEqual(getattr(progress[-1], "status"), "success")
        self.assertEqual(len(PdfReader(self.destination).pages), 3)
        download_options = json.loads(self.server.download_body)
        self.assertFalse(download_options["ignore_blank"])

    def make_client(self) -> UmiOcrClient:
        return UmiOcrClient(f"http://127.0.0.1:{self.server.server_port}", sleep=lambda _seconds: None)

    def test_failed_task_is_cleared_without_downloading_partial_result(self) -> None:
        self.source.write_bytes(b"%PDF-source")
        self.server.result_response = {
            "code": 100, "state": "failure", "is_done": True, "message": "OCR engine unavailable",
        }
        with self.assertRaisesRegex(UmiOcrError, "OCR engine unavailable"):
            self.make_client().ocr_pdf(self.source, self.destination, poll_interval=0)
        self.assertEqual(self.server.events, ["upload", "result", "clear"])
        self.assertFalse(self.destination.exists())

    def test_upload_failure_includes_server_reason(self) -> None:
        self.source.write_bytes(b"%PDF-source")
        self.server.upload_response = {"code": 400, "data": "Invalid document"}
        with self.assertRaisesRegex(UmiOcrError, "Invalid document"):
            self.make_client().ocr_pdf(self.source, self.destination)
        self.assertEqual(self.server.events, ["upload"])

    def test_timeout_clears_the_ocr_task(self) -> None:
        self.source.write_bytes(b"%PDF-source")
        self.server.result_response = {"code": 100, "state": "running", "is_done": False}
        with self.assertRaisesRegex(UmiOcrError, "超时"):
            self.make_client().ocr_pdf(self.source, self.destination, timeout=0)
        self.assertEqual(self.server.events, ["upload", "result", "clear"])

    def test_invalid_pdf_is_not_written_over_an_existing_destination(self) -> None:
        self.source.write_bytes(b"%PDF-source")
        self.destination.write_bytes(b"preserve me")
        self.server.layered_pdf = b"<html>download failed</html>"
        with self.assertRaisesRegex(UmiOcrError, "PDF"):
            self.make_client().ocr_pdf(self.source, self.destination, poll_interval=0)
        self.assertEqual(self.destination.read_bytes(), b"preserve me")
        self.assertEqual(self.server.events[-1], "clear")

    def test_ocr_rejects_using_original_as_destination_before_upload(self) -> None:
        self.source.write_bytes(b"%PDF-source")
        with self.assertRaisesRegex(UmiOcrError, "原始 PDF"):
            self.make_client().ocr_pdf(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), b"%PDF-source")
        self.assertEqual(self.server.events, [])

    def test_client_rejects_nonlocal_service_addresses(self) -> None:
        for address in ("https://example.com", "http://192.168.1.5:1224", "http://127.0.0.1:1224/path"):
            with self.subTest(address=address), self.assertRaises(UmiOcrError):
                UmiOcrClient(address)

    def test_download_address_cannot_leave_the_local_service(self) -> None:
        client = self.make_client()
        with patch.object(client, "_request_json", return_value={"code": 100, "data": "https://example.com/doc.pdf"}):
            with self.assertRaisesRegex(UmiOcrError, "本地"):
                client.download_layered_pdf("task-1", self.destination)
        self.assertFalse(self.destination.exists())

    def test_cancelled_ocr_cleans_up_task(self) -> None:
        self.source.write_bytes(b"%PDF-source")
        cancel_event = threading.Event()
        with self.assertRaisesRegex(UmiOcrError, "取消"):
            self.make_client().ocr_pdf(
                self.source, self.destination,
                cancel_event=cancel_event,
                on_progress=lambda _progress: cancel_event.set(),
            )
        self.assertEqual(self.server.events, ["upload", "result", "clear"])
        self.assertFalse(self.destination.exists())

    def test_download_supports_chinese_names_and_spaces_from_real_umi_service(self) -> None:
        self.source.write_bytes(b"%PDF-source")
        self.server.download_path = "/download/[OCR] 大学物理学 第十一章.pdf"
        result = self.make_client().ocr_pdf(self.source, self.destination, poll_interval=0)
        self.assertEqual(result.read_bytes(), self.server.layered_pdf)


if __name__ == "__main__":
    unittest.main()


class OcrDeadlineTests(unittest.TestCase):
    def test_progress_can_continue_past_one_hour_without_a_hard_deadline(self) -> None:
        clock = [0.0]
        def advance(_delay: float) -> None:
            clock[0] += 700
        client = UmiOcrClient(sleep=advance)
        states = [{"code": 100, "state": "running", "is_done": False,
                   "processed_count": count, "pages_count": 6} for count in range(6)]
        states.append({"code": 100, "state": "success", "is_done": True,
                       "processed_count": 6, "pages_count": 6})
        with patch("umi_ocr_client.time.monotonic", side_effect=lambda: clock[0]), patch.object(
            client, "_request_json", side_effect=states,
        ):
            progress = client.wait_for_completion("task", timeout=None, inactivity_timeout=900)
        self.assertTrue(progress.is_done)
        self.assertGreater(clock[0], 3600)

    def test_no_new_pages_eventually_times_out(self) -> None:
        clock = [0.0]
        client = UmiOcrClient(sleep=lambda _delay: clock.__setitem__(0, clock[0] + 500))
        stuck = {"code": 100, "state": "running", "is_done": False,
                 "processed_count": 2, "pages_count": 20}
        with patch("umi_ocr_client.time.monotonic", side_effect=lambda: clock[0]), patch.object(
            client, "_request_json", return_value=stuck,
        ), self.assertRaisesRegex(UmiOcrError, "无.*进展|停滞"):
            client.wait_for_completion("task", timeout=None, inactivity_timeout=900)
