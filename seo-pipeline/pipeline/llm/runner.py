"""Chạy một prompt từ đầu đến cuối: chọn provider/model → render → gọi → validate → repair → ghi prompt_runs.

- Cache theo idempotency_key: bước chạy lại (sau crash, sau khi chờ external) không gọi model lần nữa.
- Repair tối đa `repair_attempts` lần bằng repair_v1 (§4.3); Truncated thì chạy lại 1 lần với max tokens ×1,5.
- AwaitingExternal / Refused / TransientError được ném lên để executor xử lý.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import UUID

import httpx
import psycopg
from psycopg.types.json import Jsonb

from ..artifacts import BlobStore
from ..idempotency import idempotency_key
from ..prompts import GROUNDED, PromptSpec, render, render_spec
from .base import FAMILIES, LLMError, LLMRequest, LLMResult, Provider, Refused, Truncated
from .validators import validate_output

log = logging.getLogger(__name__)


class InvalidOutput(LLMError):
    """Hết lượt repair mà output vẫn sai."""


@dataclass
class RunOutput:
    parsed: Any
    text: str
    run_id: UUID
    provider: str
    model: str
    cached: bool = False


@dataclass
class ModelChoice:
    provider: str
    model: str
    effort: str | None


def choose_model(config: dict, model_key: str, avoid_families: set[str] = frozenset()) -> ModelChoice:
    """Lấy model theo config.models; trùng họ cần tránh (ADR-5) thì đổi sang family_defaults của họ khác."""
    m = config["models"][model_key]
    effort = m.get("effort") or m.get("thinking_level")
    if m["provider"] not in avoid_families:
        return ModelChoice(m["provider"], m["model"], effort)
    defaults = config["family_defaults"]
    # Ưu tiên họ chạy qua extension web (không tốn tiền API), rồi mới tới họ gọi API
    providers = config.get("providers", {})
    others = sorted((f for f in FAMILIES if f != m["provider"]),
                    key=lambda f: providers.get(f, {}).get("mode") not in ("external", "proxy"))
    for fam in others:
        if fam not in avoid_families and fam in defaults:
            return ModelChoice(fam, defaults[fam], effort)
    raise ValueError(f"không còn họ model nào ngoài {sorted(avoid_families)} cho {model_key}")


ProviderFactory = Callable[[str, UUID | None, str | None], Provider]


class PromptRunner:
    def __init__(self, conn: psycopg.Connection, config: dict, registry: dict[str, PromptSpec], schemas: dict[str, dict],
                 shared: dict[str, str], blobs: BlobStore, provider_factory: ProviderFactory):
        self.conn = conn
        self.config = config
        self.registry = registry
        self.schemas = schemas
        self.shared = shared
        self.blobs = blobs
        self.provider_factory = provider_factory

    def run(self, *, job_id: UUID, step: str, prompt_id: str, variables: dict[str, Any],
            input_hashes: list[str], ctx: dict | None = None, avoid_families: set[str] = frozenset(),
            model_key: str | None = None, feedback: str | None = None) -> RunOutput:
        spec = self.registry[prompt_id]
        choice = choose_model(self.config, model_key or spec.model_key, avoid_families)
        schema = self.schemas[spec.output] if spec.is_json else None
        system, user = render_spec(spec, variables, self.shared)
        if feedback:
            user += f"\n\n<phan_hoi_duyet>{feedback}</phan_hoi_duyet>\nSửa đúng các điểm trong phan_hoi_duyet."
        ctx = ctx or {}
        params = {"effort": choice.effort, "prompt_sha": hashlib.sha256(f"{system}\x1f{user}".encode()).hexdigest()}
        key = idempotency_key(f"{step}:{prompt_id}", input_hashes, spec.version, choice.provider, choice.model,
                              params, self.config["thresholds"])

        cached = self.conn.execute(
            "SELECT id, response_raw_uri, (params->'raw' ?| array['external_task_id', 'web_proxy']) AS provider_mode_external "
            "FROM prompt_runs WHERE idempotency_key = %s AND status = 'ok'", (key,)
        ).fetchone()
        if cached:
            text = self.blobs.get(cached["response_raw_uri"]).decode("utf-8")
            rep = validate_output(text, schema, spec.validators, ctx, lenient_json=cached["provider_mode_external"])
            if rep.ok:
                return RunOutput(rep.parsed, text, cached["id"], choice.provider, choice.model, cached=True)
            # Output từng đạt nay không qua validator (luật đã siết): hạ thành invalid, nếu không lượt gọi mới đạt
            # sẽ đụng unique index prompt_runs_idem_ok (mỗi key chỉ một dòng ok) và bước lỗi lặp mãi
            self.conn.execute("UPDATE prompt_runs SET status = 'invalid', validation_errors = %s WHERE id = %s",
                              (Jsonb(rep.errors[:50]), cached["id"]))
        # Output bị loại trước đây có thể hợp lệ với validator mới (đã sửa luật): dùng lại, khỏi trả tiền gọi lại
        for old in [] if cached else self.conn.execute(
                "SELECT id, response_raw_uri, (params->'raw' ?| array['external_task_id', 'web_proxy']) AS ext FROM prompt_runs "
                "WHERE idempotency_key = %s AND status = 'invalid' ORDER BY created_at DESC LIMIT 3", (key,)).fetchall():
            text = self.blobs.get(old["response_raw_uri"]).decode("utf-8")
            rep = validate_output(text, schema, spec.validators, ctx, lenient_json=old["ext"])
            if rep.ok:
                self.conn.execute("UPDATE prompt_runs SET status = 'ok', validation_errors = NULL WHERE id = %s", (old["id"],))
                log.info("%s: output cũ %s hợp lệ với validator hiện tại, dùng lại", prompt_id, old["id"])
                return RunOutput(rep.parsed, text, old["id"], choice.provider, choice.model, cached=True)

        provider = self.provider_factory(choice.provider, job_id, step)
        max_tokens = spec.max_output(choice.provider)
        repairs = self.config["thresholds"].get("repair_attempts", 2)
        rnd = self._round(key, repairs)
        salt = f":r{rnd}" if rnd else ""  # vòng 0 giữ key cũ (tương thích task external đã có)
        current_user, bumped = user, False
        attempt = 0
        while attempt <= repairs:
            req = LLMRequest(
                prompt_id=prompt_id, provider=choice.provider, model=choice.model, system=system, user=current_user,
                schema=schema, schema_name=spec.output, max_output_tokens=max_tokens, effort=choice.effort,
                grounding=prompt_id in GROUNDED, cache_system=prompt_id in ("P6-S", "P6-H", "P6-A", "P8a", "P8b", "P8c", "P8d"),
                cache_ttl="1h" if prompt_id == "P6-H" else "5m",
            )
            self._mark_call(job_id, prompt_id, choice, attempt, provider)
            try:
                result = provider.complete(req, f"{key}{salt}:{attempt}:{max_tokens}")
            except (Truncated, Refused) as e:
                # Lượt gọi hỏng vẫn bị hãng tính tiền: ghi prompt_runs + cộng chi phí job, nếu không trần chi phí
                # không bao giờ chặn được bước cứ bị cắt/từ chối rồi chạy lại
                if e.billed is not None:
                    self._record(job_id, step, spec, choice, key, attempt, req, e.billed, [str(e)], input_hashes, rnd,
                                 status="refused" if isinstance(e, Refused) else "truncated")
                if isinstance(e, Refused) or bumped:
                    raise
                bumped, max_tokens = True, int(max_tokens * 1.5)
                continue
            finally:
                self._mark_call(job_id, None)
            rep = validate_output(result.text, schema, spec.validators, ctx, lenient_json=result.stop_reason == "external")
            if rep.skipped:
                log.info("%s bỏ qua validator: %s", prompt_id, rep.skipped)
            run_id = self._record(job_id, step, spec, choice, key, attempt, req, result, rep.errors, input_hashes, rnd)
            if rep.ok:
                return RunOutput(rep.parsed, result.text, run_id, choice.provider, choice.model)
            attempt += 1
            current_user = user + "\n\n" + render(self.shared["repair_v1"], {
                "PREVIOUS_OUTPUT": result.text, "VALIDATOR_ERRORS": "\n".join(rep.errors[:50])}, self.shared)
        raise InvalidOutput(f"{prompt_id}: output sai sau {repairs} lần repair")

    def _mark_call(self, job_id, prompt_id: str | None, choice: ModelChoice | None = None, attempt: int = 0,
                   provider=None) -> None:
        """Ghi lượt gọi API đang chạy vào jobs.current_call cho UI (task extension đã có bảng riêng nên bỏ qua)."""
        if job_id is None or (prompt_id and type(provider).__name__ == "ExternalProvider"):
            return
        call = None if prompt_id is None else Jsonb({
            "prompt_id": prompt_id, "provider": choice.provider, "model": choice.model, "attempt": attempt,
            "started_at": datetime.now(UTC).isoformat()})
        try:
            self.conn.execute("UPDATE jobs SET current_call = %s WHERE id = %s", (call, job_id))
        except psycopg.errors.UndefinedColumn:  # DB chưa chạy migration 004
            pass

    def _round(self, key: str, repairs: int) -> int:
        """Số lần bước này đã hỏng hẳn (hết repair). Autopilot chạy lại bước → vòng mới hỏi model lượt mới,
        không đọc lại task external cũ đã trả output sai."""
        return self.conn.execute(
            "SELECT count(*) AS n FROM prompt_runs WHERE idempotency_key = %s AND status = 'invalid' AND repair_attempt = %s",
            (key, repairs)).fetchone()["n"]

    def _record(self, job_id, step, spec: PromptSpec, choice: ModelChoice, key: str, attempt: int, req: LLMRequest,
                result: LLMResult, errors: list[str], input_hashes: list[str], rnd: int = 0,
                status: str | None = None) -> UUID:
        """status: 'truncated' / 'refused' cho lượt gọi hỏng nhưng vẫn tốn tiền (không lưu blob output)."""
        if errors and status is None:
            # External: mỗi lần job thức dậy lại đọc cùng task cũ — không ghi trùng lượt invalid (trong cùng vòng)
            dup = self.conn.execute(
                "SELECT id FROM prompt_runs WHERE idempotency_key = %s AND repair_attempt = %s AND status = 'invalid' "
                "AND coalesce((params->>'round')::int, 0) = %s",
                (key, attempt, rnd)).fetchone()
            if dup:
                if result.cost_usd:  # lượt gọi API lặp lại (bước retry giữa chừng repair) vẫn tốn tiền
                    self.conn.execute("UPDATE jobs SET cost_usd = cost_usd + %s WHERE id = %s", (result.cost_usd, job_id))
                return dup["id"]
        uri = None
        if status is None:
            data = result.text.encode("utf-8")
            uri = self.blobs.put(data, hashlib.sha256(data).hexdigest())
        # Lưu toàn văn prompt của lượt này (kể cả lượt repair) để debug; tắt bằng debug.save_prompts: false
        prompt_uri = None
        if self.config.get("debug", {}).get("save_prompts", True):
            pdata = f"=== SYSTEM ===\n{req.system}\n\n=== USER ===\n{req.user}".encode("utf-8")
            prompt_uri = self.blobs.put(pdata, hashlib.sha256(pdata).hexdigest())
        with self.conn.transaction():
            row = self.conn.execute(
                """INSERT INTO prompt_runs (job_id, step, prompt_id, prompt_version, provider, model, params, idempotency_key,
                       request_redacted, response_raw_uri, validation_errors, repair_attempt, tokens_in, tokens_cached_read,
                       tokens_cache_write, tokens_out, cost_usd, latency_ms, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (job_id, step, spec.id, spec.version, choice.provider, result.model,
                 Jsonb({"effort": req.effort, "max_output_tokens": req.max_output_tokens, "grounding": req.grounding, "round": rnd,
                        "input_hashes": input_hashes, "raw": result.raw}),
                 key, Jsonb({"system_chars": len(req.system), "user_chars": len(req.user), "prompt_uri": prompt_uri}), uri,
                 Jsonb(errors) if errors else None, attempt, result.tokens_in, result.tokens_cached_read,
                 result.tokens_cache_write, result.tokens_out, result.cost_usd, result.latency_ms,
                 status or ("invalid" if errors else "ok")),
            ).fetchone()
            if result.cost_usd:
                self.conn.execute("UPDATE jobs SET cost_usd = cost_usd + %s WHERE id = %s", (result.cost_usd, job_id))
        return row["id"]


def make_provider_factory(conn: psycopg.Connection, config: dict) -> ProviderFactory:
    """mode api → gọi thẳng API; mode external → task cho extension; mode proxy (gemini) → proxy cục bộ
    gemini-web-to-api. Client API được tạo một lần."""
    from .anthropic_provider import AnthropicProvider
    from .external import ExternalProvider
    from .gemini_provider import GeminiProvider
    from .openai_provider import OpenAIProvider

    api_classes = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "gemini": GeminiProvider}
    clients: dict[str, Provider] = {}

    def factory(name: str, job_id: UUID | None, step: str | None) -> Provider:
        pcfg = config["providers"][name]
        if pcfg.get("mode", "api") == "external":
            if name == "anthropic":
                raise ValueError("anthropic chỉ hỗ trợ mode api")
            return ExternalProvider(name, pcfg, conn, job_id, step)
        if pcfg.get("mode") == "proxy":
            if name != "gemini":
                raise ValueError(f"{name} không hỗ trợ mode proxy")
            from .gemini_proxy_provider import GeminiProxyProvider
            if "gemini-proxy-http" not in clients:  # dùng chung một httpx.Client; provider tạo theo từng lượt (cần job_id/step)
                clients["gemini-proxy-http"] = httpx.Client(timeout=pcfg.get("timeout_seconds", 600))
            return GeminiProxyProvider(pcfg, conn, job_id, step, http=clients["gemini-proxy-http"])
        if name not in clients:
            clients[name] = api_classes[name](pcfg, config.get("pricing", {}))
        return clients[name]

    return factory
