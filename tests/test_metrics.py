"""评价指标的正确性与稳定性测试。"""
from __future__ import annotations

import unittest

import numpy as np

from competition.metrics import decolor, denoise


def _clean(size=64, seed=0):
    rng = np.random.default_rng(seed)
    base = np.zeros((size, size, 3), dtype=np.uint8)
    half = size // 2
    base[:half, :half] = (220, 40, 40)
    base[:half, half:] = (40, 200, 60)
    base[half:, :half] = (50, 60, 220)
    base[half:, half:] = (230, 210, 60)
    return np.clip(base.astype(int) + rng.integers(0, 6, base.shape), 0, 255).astype(np.uint8)


class DenoiseMetricTests(unittest.TestCase):
    def test_psnr_matches_definition(self):
        gt = np.full((16, 16, 3), 100, dtype=np.uint8)
        pred = gt.copy()
        pred[0, 0, 0] = 101  # 单像素误差 1
        result = denoise.psnr_ssim(gt, pred, data_range=255)
        n = gt.size
        expected = 10 * np.log10(255.0 ** 2 / (1.0 / n))
        self.assertAlmostEqual(result["psnr"], expected, places=2)

    def test_ssim_of_identical_is_one(self):
        gt = _clean()
        result = denoise.psnr_ssim(gt, gt.copy(), data_range=255)
        self.assertAlmostEqual(result["ssim"], 1.0, places=6)
        self.assertEqual(result["psnr"], 99.0)  # 完全一致时截断为 99 dB

    def test_ssim_penalizes_blur(self):
        gt = _clean()
        blurred = gt.copy()
        blurred[1:-1, 1:-1] = (
            gt[:-2, 1:-1].astype(int) + gt[2:, 1:-1] + gt[1:-1, :-2] + gt[1:-1, 2:]
        ) // 4
        sharp = denoise.psnr_ssim(gt, gt.copy())
        soft = denoise.psnr_ssim(gt, blurred)
        self.assertGreater(sharp["ssim"], soft["ssim"])

    def test_shape_mismatch_raises(self):
        with self.assertRaises(ValueError):
            denoise.psnr_ssim(np.zeros((8, 8, 3), np.uint8), np.zeros((9, 8, 3), np.uint8))


class DecolorMetricTests(unittest.TestCase):
    def test_constant_gray_scores_zero(self):
        rgb = _clean()
        flat = np.full(rgb.shape[:2], 128, dtype=np.uint8)
        result = decolor.evaluate_arrays(rgb, flat, rounds=4, seed=1)
        self.assertEqual(result["ccpr"], 0.0)
        self.assertEqual(result["ccfr"], 1.0)
        self.assertEqual(result["escore"], 0.0)

    def test_luminance_gray_keeps_some_contrast(self):
        rgb = _clean()
        gray = decolor.luminance_grayscale(rgb)
        result = decolor.evaluate_arrays(rgb, gray, rounds=8, seed=1)
        self.assertGreater(result["ccpr"], 0.2)
        self.assertGreater(result["ccfr"], 0.5)
        self.assertGreater(result["escore"], 0.2)

    def test_metrics_are_deterministic(self):
        rgb = _clean()
        gray = decolor.luminance_grayscale(rgb)
        a = decolor.evaluate_arrays(rgb, gray, rounds=6, seed=3)
        b = decolor.evaluate_arrays(rgb, gray, rounds=6, seed=3)
        self.assertEqual(a, b)

    def test_size_mismatch_raises(self):
        rgb = _clean()
        with self.assertRaises(ValueError):
            decolor.evaluate_arrays(rgb, np.zeros((8, 8), np.uint8), rounds=2)

    def test_invented_contrast_hurts_content_fidelity(self):
        """在原本平坦的同色区域制造噪声，会产生原图不存在的对比度，CCFR 应下降。"""
        rgb = _clean()
        gray = decolor.luminance_grayscale(rgb)
        bad = gray.copy()
        rng = np.random.default_rng(3)
        h, w = gray.shape
        patch = bad[: h // 2, : w // 2].astype(int)
        bad[: h // 2, : w // 2] = np.clip(patch + rng.integers(-70, 70, patch.shape), 0, 255)
        good_res = decolor.evaluate_arrays(rgb, gray, rounds=8, seed=5)
        bad_res = decolor.evaluate_arrays(rgb, bad, rounds=8, seed=5)
        self.assertLess(bad_res["ccfr"], good_res["ccfr"])
