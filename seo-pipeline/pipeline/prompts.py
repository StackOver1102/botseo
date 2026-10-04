"""Prompt registry (§4): nạp prompts/*.yaml, gộp `extends`, render {{VAR}} và {{> hard_rules}}."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
SCHEMA_PATH = ROOT / "schemas" / "v5.schema.json"

# max_output mặc định theo bảng §4.4 (khi YAML không ghi provider_params)
DEFAULT_MAX_OUTPUT = {
    "B0-H": 1500, "PF": 3000, "P1": 16000, "P2a": 32000, "P2b": 12000, "P3": 8000, "P4-D": 12000, "P4-G": 12000,
    "P5": 3000, "G": 3000, "P6-S": 24000, "P6-H": 6000, "P7a": 3000, "P7b": 16000, "P8a": 8000, "P8b": 8000,
    "P8c": 8000, "P8d": 8000, "P9": 16000, "P10": 16000, "P10-S": 8000, "P11": 2000, "RW": 16000, "RWA": 1500, "P6-A": 8000,
}
GROUNDED = {"P4-G", "P10"}

_VAR = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")
_INCLUDE = re.compile(r"\{\{>\s*(\w+)\s*\}\}")


class TemplateError(KeyError):
    pass


@dataclass
class PromptSpec:
    id: str
    version: str
    model_key: str
    output: str  # tên $defs trong schema, hoặc "markdown"
    system_template: str
    user_template: str
    validators: list[str] = field(default_factory=list)
    provider_params: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def is_json(self) -> bool:
        return self.output != "markdown"

    def max_output(self, provider: str) -> int:
        p = self.provider_params.get(provider) or {}
        return p.get("max_tokens") or p.get("max_output_tokens") or DEFAULT_MAX_OUTPUT.get(self.id, 8000)


def load_registry(directory: Path = PROMPTS_DIR) -> dict[str, PromptSpec]:
    raw = {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in directory.glob("*.yaml")}
    out = {}
    for pid, data in raw.items():
        if "extends" in data:
            base = dict(raw[data["extends"]])
            base.update({k: v for k, v in data.items() if k not in ("user_template_append", "user_template_turn")})
            if "user_template_append" in data:
                base["user_template"] = base.get("user_template", "") + data["user_template_append"]
            if "user_template_turn" in data:
                base["user_template"] = data["user_template_turn"]
            data = base
        out[pid] = PromptSpec(
            id=data["id"], version=str(data["version"]), model_key=data["model_key"], output=data["output"],
            system_template=data.get("system_template", ""), user_template=data.get("user_template", ""),
            validators=list(data.get("validators", [])), provider_params=data.get("provider_params") or {}, raw=data,
        )
    return out


def load_schemas(path: Path = SCHEMA_PATH) -> dict[str, dict]:
    defs = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **d} for name, d in defs.items()}


def load_shared(directory: Path = PROMPTS_DIR / "_shared") -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8").strip() for p in directory.glob("*.md")}


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render(template: str, variables: dict[str, Any], shared: dict[str, str]) -> str:
    text = _INCLUDE.sub(lambda m: shared[m.group(1)], template)
    missing = sorted({m.group(1) for m in _VAR.finditer(text)} - variables.keys())
    if missing:
        raise TemplateError(f"thiếu biến: {missing}")
    return _VAR.sub(lambda m: _fmt(variables[m.group(1)]), text).strip()


def render_spec(spec: PromptSpec, variables: dict[str, Any], shared: dict[str, str]) -> tuple[str, str]:
    return render(spec.system_template, variables, shared), render(spec.user_template, variables, shared)
