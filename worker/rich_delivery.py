"""Bounded, privacy-safe delivery receipt evidence."""
from typing import Any


def bounded(text: str, limit: int) -> str:
    if len(text.encode()) <= limit:
        return text
    return text.encode()[:max(0, limit - 3)].decode("utf-8", errors="ignore").rstrip() + "…"


def evidence_for(results: list[dict[str, Any]], indices: list[int], media_count: int) -> dict[str, Any]:
    items = [results[i] for i in dict.fromkeys(indices) if "error" not in results[i]
             and results[i].get("access") == "public"]
    full = "\n\n".join(str(item.get("plainContent") or item.get("content") or "") for item in items)
    return {"platforms": [item["platform"] for item in items],
            "sources": [item["canonicalUrl"] for item in items],
            "titles": [str(item.get("title") or "") for item in items],
            "content": bounded(full, 8000), "truncated": len(full.encode()) > 8000,
            "mediaCount": media_count,
            "trust": "untrusted_external_data"}
