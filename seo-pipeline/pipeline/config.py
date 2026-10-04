"""Nạp config.yaml (§8). Mọi ngưỡng là heuristic."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config" / "config.yaml"


def load_dotenv(path: str | Path = ROOT / ".env") -> None:
    """Nạp KEY=VALUE từ .env vào môi trường; biến đã export sẵn trong shell được ưu tiên."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key, value)


def load_config(path: str | Path = DEFAULT_PATH, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    load_dotenv()
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if overrides:
        _merge(cfg, overrides)
    return cfg


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
