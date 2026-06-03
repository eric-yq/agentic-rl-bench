"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv_int(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def _csv_str(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


@dataclass
class Config:
    # Storage
    s3_bucket: str = field(default_factory=lambda: os.getenv("S3_BUCKET", ""))
    s3_prefix: str = field(default_factory=lambda: os.getenv("S3_PREFIX", "agentic-rl-bench"))
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "us-east-1"))

    # Test parameters
    duration_sec: int = field(default_factory=lambda: int(os.getenv("DURATION_SEC", "300")))
    warmup_sec: int = field(default_factory=lambda: int(os.getenv("WARMUP_SEC", "30")))
    cooldown_sec: int = field(default_factory=lambda: int(os.getenv("COOLDOWN_SEC", "60")))
    concurrencies: list[int] = field(
        default_factory=lambda: _csv_int(os.getenv("CONCURRENCIES", "1,8,32,128"))
    )

    # Pricing (USD/h, 4xlarge baseline)
    price_c7i_4xl: float = field(default_factory=lambda: float(os.getenv("PRICE_C7I_4XL", "0.7140")))
    price_c8g_4xl: float = field(default_factory=lambda: float(os.getenv("PRICE_C8G_4XL", "0.5808")))

    # Service endpoints
    b1_worker_url: str = field(default_factory=lambda: os.getenv("B1_WORKER_URL", "http://b1-codeexec-worker:8001"))
    b3_api_url: str = field(default_factory=lambda: os.getenv("B3_API_URL", "http://b3-mock-api:8003"))
    b4_worker_url: str = field(default_factory=lambda: os.getenv("B4_WORKER_URL", "http://b4-playwright-worker:8004"))
    b4_target_url: str = field(default_factory=lambda: os.getenv("B4_TARGET_URL", "http://b4-webarena-static:80"))
    b5_worker_url: str = field(default_factory=lambda: os.getenv("B5_WORKER_URL", "http://b5-sql-runner:8005"))

    # B8 cold-start
    b8_trials: int = field(default_factory=lambda: int(os.getenv("B8_TRIALS", "1000")))
    b8_image: str = field(default_factory=lambda: os.getenv("B8_IMAGE", "python:3.11-slim"))

    # Run control
    skip: list[str] = field(default_factory=lambda: _csv_str(os.getenv("SKIP", "")))

    # Output
    results_dir: str = field(default_factory=lambda: os.getenv("RESULTS_DIR", "/results"))


CFG = Config()
