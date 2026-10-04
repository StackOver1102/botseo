"""Dựng và kiểm tra JSON-LD Article + BreadcrumbList (§5.9). Trường null bị bỏ hẳn key."""
from __future__ import annotations

import json
from typing import Any

REQUIRED_ARTICLE = ["@context", "@type", "headline", "author"]


def _drop_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None and v != [] and v != {}}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj if v is not None]
    return obj


def build(jsonld_data: dict, url: str | None = None, publisher: dict | None = None) -> list[dict]:
    author = None
    if jsonld_data.get("author_name"):
        author = {"@type": jsonld_data.get("author_type") or "Person", "name": jsonld_data["author_name"],
                  "url": jsonld_data.get("author_url")}
    article = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": jsonld_data["headline"],
        "author": author,
        "datePublished": jsonld_data.get("date_published"),
        "dateModified": jsonld_data.get("date_modified"),
        "mainEntityOfPage": url,
        "publisher": publisher,
    }
    out = [_drop_none(article)]
    crumbs = jsonld_data.get("breadcrumb") or []
    if crumbs:
        out.append({
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "name": c["name"], "item": c["url"]}
                for i, c in enumerate(crumbs)
            ],
        })
    return out


def validate(blocks: list[dict]) -> list[str]:
    """Trả danh sách lỗi; rỗng là hợp lệ. Rich Results Test vẫn kiểm tay (không có API công khai [CXM])."""
    errors = []
    try:
        blocks = json.loads(json.dumps(blocks, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        return [f"json: {e}"]
    articles = [b for b in blocks if b.get("@type") == "Article"]
    if len(articles) != 1:
        errors.append("cần đúng 1 Article")
    for a in articles:
        errors += [f"Article thiếu {k}" for k in REQUIRED_ARTICLE if k not in a]
        if len(a.get("headline", "")) > 110:
            errors.append("headline > 110 ký tự")
    return errors
