"""测试用的临时竞赛工作区。"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import numpy as np
import yaml
from django.test import override_settings
from PIL import Image

from competition.config import load_config

CONFIG_TEMPLATE = {
    "competition": {
        "title": "测试竞赛",
        "subtitle": "单元测试",
        "timezone": "Asia/Shanghai",
        "contest_code_prefix": "S",
        "intro": "test",
        "rules": ["rule"],
    },
    "schedule": {
        "submission_open": "2000-01-01T00:00:00",
        "submission_close": "2099-01-01T00:00:00",
        "review_open": "2000-01-02T00:00:00",
        "review_close": "2099-01-02T00:00:00",
    },
    "tasks": [
        {
            "slug": "denoise",
            "name": "图像去噪",
            "short_name": "去噪",
            "channels": 3,
            "color_mode": "RGB",
            "max_submissions": 2,
            "ranking_metric": "psnr",
            "tiebreak_metric": "ssim",
            "images": [{"key": "D01", "label": "图 1", "note": "test"}],
            "metrics": [
                {"key": "psnr", "name": "PSNR", "digits": 2},
                {"key": "ssim", "name": "SSIM", "digits": 4},
            ],
        },
        {
            "slug": "decolor",
            "name": "图像去色",
            "short_name": "去色",
            "channels": 1,
            "color_mode": "L",
            "max_submissions": 2,
            "ranking_metric": "escore",
            "tiebreak_metric": "ccpr",
            "images": [{"key": "C01", "label": "图 1", "note": "test"}],
            "metrics": [
                {"key": "ccpr", "name": "CCPR", "digits": 4},
                {"key": "ccfr", "name": "CCFR", "digits": 4},
                {"key": "escore", "name": "E-score", "digits": 4},
            ],
        },
    ],
    "metrics_options": {
        "data_range": 255,
        "ssim_win_size": 7,
        "decolor_tau": 5,
        "decolor_pairs_per_pixel": 4,
        "decolor_seed": 7,
    },
    "peer_review": {
        "enabled": True,
        "task": "decolor",
        "comparisons_per_student": 2,
        "allow_tie": True,
        "regularization": 1.0,
    },
    "scoring": {"weight_objective": 0.4, "weight_subjective": 0.6},
}


def _color_image(size=64, seed=0):
    rng = np.random.default_rng(seed)
    img = np.zeros((size, size, 3), dtype=np.uint8)
    half = size // 2
    img[:half, :half] = (220, 40, 40)
    img[:half, half:] = (40, 200, 60)
    img[half:, :half] = (50, 60, 220)
    img[half:, half:] = (230, 210, 60)
    noise = rng.integers(-6, 7, img.shape)
    return np.clip(img.astype(int) + noise, 0, 255).astype(np.uint8)


class Workspace:
    def __init__(self, max_submissions: int = 2):
        self.root = Path(tempfile.mkdtemp(prefix="dip-test-"))
        for rel in ("data/public/denoise", "data/public/decolor",
                    "data/private/ground_truth", "storage", "config"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)

        clean = _color_image()
        noisy = np.clip(clean.astype(int) + np.random.default_rng(1).normal(0, 8, clean.shape),
                        0, 255).astype(np.uint8)
        Image.fromarray(noisy, "RGB").save(self.root / "data/public/denoise/D01.png")
        Image.fromarray(clean, "RGB").save(self.root / "data/private/ground_truth/D01.png")
        Image.fromarray(clean, "RGB").save(self.root / "data/public/decolor/C01.png")

        config = {
            **CONFIG_TEMPLATE,
            "tasks": [
                {**task, "max_submissions": max_submissions}
                for task in CONFIG_TEMPLATE["tasks"]
            ],
        }
        self.config_path = self.root / "config/competition.yaml"
        self.config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
        )
        self.override = override_settings(
            BASE_DIR=self.root,
            COMPETITION_CONFIG=self.config_path,
            MEDIA_ROOT=self.root / "storage",
        )
        self.override.enable()
        load_config(force=True)

    def cleanup(self):
        self.override.disable()
        load_config(force=True)
        shutil.rmtree(self.root, ignore_errors=True)
