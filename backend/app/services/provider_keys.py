from __future__ import annotations

def provider_status_key(provider: str) -> str:
    try:
        from app.provider_catalog import get_status_provider_for_result

        return get_status_provider_for_result(provider)
    except Exception:
        return provider
