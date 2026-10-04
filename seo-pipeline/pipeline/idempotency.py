"""Khoá idempotent cho mỗi lượt gọi bước (§2, xử lý lỗi): đổi input, prompt, model, params hoặc ngưỡng → chạy lại."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def _sha(obj: Any) -> str:
    data = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def idempotency_key(step_id: str, input_artifact_hashes: Iterable[str], prompt_version: str, provider: str,
                    model: str, params: dict[str, Any], config_thresholds: dict[str, Any]) -> str:
    parts = [step_id, *sorted(input_artifact_hashes), prompt_version, provider, model, _sha(params), _sha(config_thresholds)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
