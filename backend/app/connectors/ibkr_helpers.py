from __future__ import annotations


def map_ibkr_account_type(customer_type: str, account_type: str = "") -> str:
    combined = f"{customer_type} {account_type}".upper()
    if "TAX FREE" in combined or "TFSA" in combined:
        return "tfsa"
    if "RRSP" in combined:
        return "rrsp"
    if "FHSA" in combined:
        return "fhsa"
    if "RESP" in combined:
        return "resp"
    return "margin"
