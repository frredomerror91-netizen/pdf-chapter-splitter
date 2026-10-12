from __future__ import annotations

import unittest
from unittest.mock import patch

from umi_ocr_pool import plan_ocr_workers


class OcrWorkerPlanningTests(unittest.TestCase):
    def test_enough_resources_assign_one_instance_to_each_chunk(self) -> None:
        plan = plan_ocr_workers(341, max_workers=0, cpu_count=20, available_memory_mb=16000)
        self.assertEqual((plan.chunk_count, plan.worker_count), (7, 7))
        self.assertLessEqual(plan.threads_per_worker * plan.worker_count, 20)

    def test_worker_count_grows_with_pdf_size(self) -> None:
        for pages, expected in ((100, 2), (120, 3), (341, 7), (1000, 10)):
            with self.subTest(pages=pages):
                self.assertEqual(plan_ocr_workers(pages, max_workers=0, cpu_count=20, available_memory_mb=16000).worker_count, expected)

    def test_cpu_memory_and_user_cap_each_limit_concurrency(self) -> None:
        for cpus, memory, cap, expected in ((4, 16000, 0, 2), (20, 2600, 0, 2),
                                          (20, 16000, 3, 3), (64, 64000, 0, 12),
                                          (1, 600, 0, 1)):
            with self.subTest(cpus=cpus, memory=memory, cap=cap):
                self.assertEqual(plan_ocr_workers(1000, max_workers=cap, cpu_count=cpus,
                                                 available_memory_mb=memory).worker_count, expected)

    def test_unknown_memory_uses_a_conservative_fallback(self) -> None:
        with patch('umi_ocr_pool.get_available_memory_mb', return_value=None):
            self.assertEqual(plan_ocr_workers(500, cpu_count=20).worker_count, 2)

    def test_invalid_settings_are_rejected(self) -> None:
        for pages, cap in ((0, 0), (100, -1), (100, 13), (100, True)):
            with self.subTest(pages=pages, cap=cap), self.assertRaises(ValueError):
                plan_ocr_workers(pages, max_workers=cap, cpu_count=20, available_memory_mb=16000)

    def test_default_concurrency_stays_at_four_when_more_chunks_fit(self) -> None:
        plan = plan_ocr_workers(306, cpu_count=20, available_memory_mb=16000)
        self.assertEqual((plan.chunk_count, plan.worker_count), (7, 4))
        self.assertLessEqual(plan.threads_per_worker * plan.worker_count, 8)

    def test_default_limit_still_respects_small_jobs_and_low_resources(self) -> None:
        for pages, cpus, memory, expected in ((100, 20, 16000, 2), (306, 4, 16000, 2), (306, 20, 2600, 2)):
            with self.subTest(pages=pages, cpus=cpus, memory=memory):
                self.assertEqual(plan_ocr_workers(pages, cpu_count=cpus,
                                                 available_memory_mb=memory).worker_count, expected)
