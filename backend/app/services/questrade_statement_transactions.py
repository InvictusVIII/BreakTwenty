"""Parse the itemized transaction ledger from Questrade monthly statement PDFs.

Imported (defunct) accounts only — active accounts get individual transactions from the
Questrade API. The ledger (statement section 04, "Activity Details / Transactions") is a
multi-column dual-currency table::

    Trans Date | Settle | Activity type | Symbol | Description | CAD[Qty Price Gross Com Net] | USD[Qty Price Gross Com Net]

Each transaction is anchored by a Trans Date in the first column (x<80); its description
wraps across the following rows until the next dated row; its cash impact is the Net of
whichever currency block is populated (right-aligned: CAD Net x≈546-557, USD Net x≈714-725).
Net is signed — parenthesised = negative (buys/transfers-out), positive = inflow
(dividends/contributions) — mapping directly to BreakTwenty's ``+inflow / -outflow``
``Transaction.amount``.

Activity-type labels map to the same BreakTwenty transaction types the live connector emits
(mirroring ``connectors.questrade_helpers.QUESTRADE_ACTIVITY_TYPE_MAP``), so the imported
transactions categorize identically to API ones. Some grouped distribution rows omit the
type word — it is inferred from the description. The statement records dividends at their
NET amount with a "NON-RES TAX WITHHELD" note but no separate withheld figure, so
withholding is not split into its own transaction here.

Uses pdfplumber for word-position extraction (the table has no full ruling grid). Pure
parsing — no DB; callers turn the points into transactions for an ``is_imported`` account.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from datetime import date

import pdfplumber

_DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_AMOUNT_RE = re.compile(r"^\(?-?[\d,]+\.\d{2}\)?$")
_ACCOUNT_RE = re.compile(r"Account #:\s*(\d+)")
# Page-footer / table-end markers — stop accumulating a description when one appears, so the
# last transaction on a page doesn't swallow the footer (FX-rate line, disclaimer, page no.,
# or the Closing-balance row).
_FOOTER_RE = re.compile(
    r"FX rate:|QUESTRADE|Investor Protection Fund|Member of|Page \d+ of|Closing balance",
    re.IGNORECASE,
)
# Table column-header row — skip so it isn't mistaken for a security name.
_HEADER_RE = re.compile(r"Description|Activity type|Trans Date|Opening balance")

# Column x-boundaries (column END positions), from the statement header layout
# (validated against 2020-2024 individual-TFSA statements).
_X_DATE_END = 80
_X_SETTLE_END = 130
_X_TYPE_END = 185
_X_SYMBOL_END = 230
_X_DESC_END = 405
_CAD_NET = (515, 580)   # right-aligned value; header "Net¹" x≈546-557
_USD_NET = (688, 760)   # right-aligned value; header "Net¹" x≈714-725

# Holdings ("Stocks/ETFs owned") tables — the canonical Symbol↔name source. The symbol sits
# in the first column (x<56); the security name follows (x≈60-186, before the cost columns).
_X_HOLD_SYM_END = 56
_X_HOLD_NAME_END = 186
_HOLD_PAGE_RE = re.compile(r"Cost/share|Mkt\. price|Pos\. cost")   # holdings-table column headers
_HOLD_SYM_RE = re.compile(r"^[.\-]?[A-Z][A-Z0-9]{0,5}(\.[A-Z]{1,3})?$")
# Boilerplate dropped before matching a ledger description to a holding name. Distinctive
# security words AND geography (US/CDN) are KEPT so look-alikes (Evolve US vs CDN banks,
# Hamilton US-covered-call vs multi-sector, YieldMax COIN vs TSLA) don't merge.
_GENERIC_TOKENS = frozenset({
    "THE", "AND", "ON", "OF", "FD", "FUND", "FUNDS", "ETF", "ETFS", "UNIT", "UNITS", "TRUST",
    "II", "INC", "CORP", "CO", "COM", "COMMON", "STOCK", "SHARES", "SHS", "SHARE", "CLASS",
    "CL", "EL", "LP", "IDX", "USD", "CAD", "HEDGED", "UNHEDGED", "DIST", "DIVIDEND", "DIV",
    "CASH", "REC", "PAY", "NON", "RES", "TAX", "WITHHELD", "WE", "ACTED", "AS", "AGENT",
    "CONT", "REINVEST", "DRIP", "OPTION", "INCOME", "STRATEGY", "ENHANCED", "INDEX",
})

# Statement activity-type label -> BreakTwenty transaction type (matches the live connector's
# QUESTRADE_ACTIVITY_TYPE_MAP; "distribution" routes to the Dividend category downstream).
_TYPE_MAP = {
    "DIV": "dividend", "DIS": "distribution",
    "Buy": "buy", "Sell": "sell",
    "Contribution": "deposit", "Transfer": "transfer",
    "Fee": "fee", "ADR": "fee", "Exchange": "transfer",
    "REV": "other", "BRW": "other", "CIL": "deposit",
}


@dataclass
class StatementTxn:
    date: str            # ISO YYYY-MM-DD
    type: str            # BreakTwenty transaction type
    amount: float        # +inflow / -outflow (statement Net, signed)
    currency: str        # "CAD" | "USD"
    symbol: str | None
    description: str


@dataclass
class TxnParse:
    account_number: str | None
    transactions: list  # list[StatementTxn]
    holdings: dict = field(default_factory=dict)  # {symbol: name} from the owned tables
    warnings: list = field(default_factory=list)
    error: str | None = None


def _parse_amount(text: str | None) -> float | None:
    if not text:
        return None
    negative = text.strip().startswith("(")
    cleaned = text.strip().strip("()").replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _iso_date(mmddyyyy: str | None) -> str | None:
    """``08-01-2023`` (MM-DD-YYYY) -> ``2023-08-01``."""
    if not mmddyyyy:
        return None
    try:
        month, day, year = mmddyyyy.split("-")
        return date(int(year), int(month), int(day)).isoformat()
    except (ValueError, TypeError):
        return None


def _resolve_type(type_word: str, description: str, amount: float | None) -> str:
    """Map the statement's activity-type label to a BreakTwenty type; infer from the
    description/sign when the label is blank (grouped distribution rows omit it)."""
    mapped = _TYPE_MAP.get(type_word)
    # An explicit trade / cash-movement label is authoritative — return it before the
    # withholding inference, so a Buy/Sell whose (sometimes mis-joined) description happens to
    # mention "WITHHELD" or carry REC/PAY dates is never reclassified as withholding tax.
    if mapped in ("buy", "sell", "deposit", "transfer", "fee"):
        return mapped
    desc = (description or "").upper()
    # Dividend/distribution context: an explicit DIV/DIS label, the DIST/CASH DIV text, or the
    # REC (record) + PAY (payment) date pair that only dividend rows carry (trades never do).
    is_dividend = (
        mapped in ("dividend", "distribution")
        or any(k in desc for k in ("DIST", "CASH DIV", "DIVIDEND"))
        or (" REC " in desc and " PAY " in desc)
    )
    # A NEGATIVE amount in a dividend context is a deduction, not income — the explicit
    # "NON-RES TAX WITHHELD" row, or a foreign-tax withholding shown only as a negative on the
    # distribution (e.g. BEP.UN), even when the row is typed DIV. Positive → the dividend.
    if amount is not None and amount < 0 and (is_dividend or "WITHHELD" in desc or "WITHHOLDING" in desc):
        return "withholding_tax"
    if mapped:
        return mapped
    if "WE ACTED AS AGENT" in desc:  # a trade execution line
        return "sell" if (amount or 0) > 0 else "buy"
    if is_dividend:
        return "dividend"
    if "CONT" in desc:
        return "deposit"
    if amount is not None and amount < 0:
        return "buy"
    return "other"


def _extract_holdings(page, holdings: dict) -> None:
    """Accumulate ``{symbol: [name words]}`` from one holdings ("owned") table page. The
    symbol is the ticker-shaped word in the first column; its (multi-line) name follows."""
    rows: dict[int, list] = {}
    for word in page.extract_words():
        rows.setdefault(round(word["top"] / 3) * 3, []).append(word)
    current: str | None = None
    for key in sorted(rows):
        cells = sorted(rows[key], key=lambda w: w["x0"])
        if _FOOTER_RE.search(" ".join(w["text"] for w in cells)):
            current = None   # page footer/disclaimer — stop extending the last holding's name
            continue
        lead = next((w["text"] for w in cells if w["x0"] < _X_HOLD_SYM_END), "")
        name_words = [
            w["text"] for w in cells
            if _X_HOLD_SYM_END <= w["x0"] < _X_HOLD_NAME_END and w["text"] not in ("BK", "NB")
        ]
        if _HOLD_SYM_RE.match(lead) and lead not in ("BK", "NB"):
            current = lead
            holdings.setdefault(current, [])
            holdings[current] += name_words
        elif current and name_words and not any(ch.isdigit() for ch in " ".join(name_words)):
            holdings[current] += name_words   # multi-line name continuation


def _name_tokens(text: str) -> frozenset:
    cleaned = re.sub(r"[^A-Z0-9 ]", " ", (text or "").upper())
    return frozenset(t for t in cleaned.split() if len(t) >= 2 and t not in _GENERIC_TOKENS)


def build_symbol_index(holdings: dict) -> list:
    """``[(symbol, name_tokens)]`` for matching; holdings whose name has <2 distinctive
    tokens are dropped (too generic to match a description safely)."""
    index = []
    for symbol, name in holdings.items():
        tokens = _name_tokens(name)
        if len(tokens) >= 2:
            index.append((symbol, tokens))
    return index


def resolve_symbol(description: str, index: list, *, threshold: float = 0.8) -> str | None:
    """The canonical holdings symbol whose name is (almost) fully contained in *description*
    (precision = |name ∩ desc| / |name|). Returns None below threshold. On a tie — usually the
    same fund's dual-currency listings sharing one name — picks deterministically (shortest
    symbol = the base listing) so a security always resolves to the SAME symbol, never split."""
    desc_tokens = _name_tokens(description)
    if not desc_tokens:
        return None
    best_score, candidates = 0.0, []
    for symbol, name_tokens in index:
        score = len(name_tokens & desc_tokens) / len(name_tokens)
        if score > best_score:
            best_score, candidates = score, [symbol]
        elif score == best_score:
            candidates.append(symbol)
    if best_score < threshold:
        return None
    return min(candidates, key=lambda s: (len(s), s))


def canonical_symbol_map(holdings: dict) -> dict:
    """Map a base ticker to its suffixed listing when both appear in the holdings (Questrade
    sometimes lists the same fund as "BTCY" in older statements and "BTCY.B" later). The
    suffixed form is the canonical ticker — its class/currency suffix is meaningful (.B/.U/.UN)
    — so a resolved or parsed "BTCY" collapses onto "BTCY.B" instead of splitting the position."""
    suffixed_by_base: dict[str, str] = {}
    for symbol in holdings:
        if "." in symbol and not symbol.startswith("."):
            suffixed_by_base.setdefault(symbol.rsplit(".", 1)[0], symbol)
    return suffixed_by_base


def canonicalize_symbol(symbol: str | None, suffixed_by_base: dict) -> str | None:
    """Collapse a suffix-less ticker onto its suffixed sibling (BTCY -> BTCY.B); already-
    suffixed or unknown symbols are returned unchanged."""
    if not symbol or "." in symbol:
        return symbol
    return suffixed_by_base.get(symbol, symbol)


def parse_statement_transactions(source, *, filename: str | None = None) -> TxnParse:
    """Parse a single Questrade statement PDF's transaction ledger.

    *source* may be a path, raw ``bytes``, or a binary file-like object. Never raises for a
    malformed PDF — returns a :class:`TxnParse` with ``error`` set.
    """
    try:
        if isinstance(source, bytes):
            source = io.BytesIO(source)
        elif isinstance(source, (str, os.PathLike)):
            source = os.fspath(source)
        pdf = pdfplumber.open(source)
    except Exception as exc:  # unreadable / corrupt / not a PDF
        return TxnParse(None, [], error=f"Could not read PDF: {exc}")

    raw: list[dict] = []
    account_number: str | None = None
    holdings_words: dict[str, list] = {}
    try:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if account_number is None:
                match = _ACCOUNT_RE.search(text)
                if match:
                    account_number = match.group(1)
            if "Activity type" not in text:
                if _HOLD_PAGE_RE.search(text):  # a holdings ("owned") table — canonical symbols
                    _extract_holdings(page, holdings_words)
                continue
            # Cluster words into visual rows by vertical position (~3pt tolerance).
            rows: dict[int, list] = {}
            for word in page.extract_words():
                rows.setdefault(round(word["top"] / 3) * 3, []).append(word)
            current: dict | None = None
            pending_name: list[str] = []   # security-name rows printed ABOVE the next dated row
            pending_symbol = ""             # a ticker on such a name row, for the next transaction
            for key in sorted(rows):
                row = sorted(rows[key], key=lambda w: w["x0"])
                row_text = " ".join(w["text"] for w in row)
                if _FOOTER_RE.search(row_text):
                    if current:  # table ended on this page — don't swallow the footer
                        raw.append(current)
                        current = None
                    pending_name, pending_symbol = [], ""
                    continue
                if _HEADER_RE.search(row_text):
                    pending_name, pending_symbol = [], ""  # table starts here — drop legend notes above
                    continue
                first = row[0]
                if _DATE_RE.match(first["text"]) and first["x0"] < _X_DATE_END:
                    if current:
                        raw.append(current)
                    cad = next((w["text"] for w in row if _CAD_NET[0] <= w["x0"] < _CAD_NET[1] and _AMOUNT_RE.match(w["text"])), None)
                    usd = next((w["text"] for w in row if _USD_NET[0] <= w["x0"] < _USD_NET[1] and _AMOUNT_RE.match(w["text"])), None)
                    settle = next((w["text"] for w in row if _X_DATE_END <= w["x0"] < _X_SETTLE_END and _DATE_RE.match(w["text"])), None)
                    dated_symbol = " ".join(w["text"] for w in row if _X_TYPE_END <= w["x0"] < _X_SYMBOL_END).strip()
                    current = {
                        "date": first["text"],
                        "settle": settle,
                        "type_word": " ".join(w["text"] for w in row if _X_SETTLE_END <= w["x0"] < _X_TYPE_END).strip(),
                        # The security name (printed on the rows above) belongs to THIS row — prepend it.
                        "symbol": dated_symbol or pending_symbol,
                        "desc": pending_name + [w["text"] for w in row if _X_SYMBOL_END <= w["x0"] < _X_DESC_END],
                        "cad": cad, "usd": usd,
                    }
                    pending_name, pending_symbol = [], ""
                else:  # a description-only row — current's continuation OR the next txn's name
                    desc_words = [w["text"] for w in row if _X_SYMBOL_END <= w["x0"] < _X_DESC_END]
                    sym_words = " ".join(w["text"] for w in row if _X_TYPE_END <= w["x0"] < _X_SYMBOL_END).strip()
                    text = " ".join(desc_words).upper()
                    # Continuation rows carry the mechanics (REC/PAY dates, SHS, the trade-
                    # execution note, withholding, contribution id); a row with none of those is
                    # the *next* transaction's security name (Questrade prints it above the date).
                    if current is not None and any(
                        k in text for k in ("SHS", "REC ", "PAY ", "WE ACTED", "AGENT", "WITHHELD", "CONT ", "REINVEST")
                    ):
                        current["desc"] += desc_words
                        if not current["symbol"] and sym_words:
                            current["symbol"] = sym_words
                    else:
                        pending_name += desc_words
                        if sym_words:
                            pending_symbol = sym_words
            if current:
                raw.append(current)
    finally:
        pdf.close()

    transactions: list[StatementTxn] = []
    for item in raw:
        description = " ".join(item["desc"]).strip()
        if description.startswith(("Opening balance", "Closing balance")):
            continue
        amount = _parse_amount(item["cad"]) if item["cad"] else _parse_amount(item["usd"])
        iso = _iso_date(item["date"])
        if amount is None or iso is None:
            continue  # a sub-total / header row with no usable Net value
        transactions.append(StatementTxn(
            date=iso,
            type=_resolve_type(item["type_word"], description, amount),
            amount=amount,
            currency="CAD" if item["cad"] else "USD",
            symbol=(item["symbol"] or None),
            description=description,
        ))

    holdings = {symbol: " ".join(words).strip() for symbol, words in holdings_words.items() if words}
    return TxnParse(account_number=account_number, transactions=transactions, holdings=holdings)
