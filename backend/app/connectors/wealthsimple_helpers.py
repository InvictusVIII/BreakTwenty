from __future__ import annotations


WS_ACTIVITY_TYPE_MAP = {
    "dividend": "dividend",
    "buy": "buy",
    "sell": "sell",
    "deposit": "deposit",
    "withdrawal": "withdrawal",
    "fee": "fee",
    "interest": "interest",
    "redistribution": "distribution",
    "refund": "deposit",
    "institutional_transfer": "transfer",
}


def classify_wealthsimple_account(unified_type: str) -> tuple[str, bool]:
    mapping = {
        "SELF_DIRECTED_CRYPTO": ("crypto", False),
        "SELF_DIRECTED_TFSA": ("tfsa", False),
        "SELF_DIRECTED_RRSP": ("rrsp", False),
        "SELF_DIRECTED_FHSA": ("fhsa", False),
        "SELF_DIRECTED_RESP": ("resp", False),
        "SELF_DIRECTED_LIRA": ("lira", False),
        "SELF_DIRECTED_RRIF": ("rrif", False),
        "SELF_DIRECTED_NON_REGISTERED": ("margin", False),
        "CASH": ("chequing", False),
        "SELF_DIRECTED_MARGIN": ("margin", False),
    }
    return mapping.get(unified_type, ("other", False))
