import httpx
import time
import logging

logger = logging.getLogger("breaktwenty.currency")

# In-memory cache: { base_currency: (timestamp, rates_dict) }
_rates_cache: dict[str, tuple[float, dict[str, float]]] = {}
_CACHE_TTL = 3600  # 1 hour


def get_cached_rates_entry(base: str) -> tuple[float, dict[str, float]] | None:
    """Public read-only view of the FX rate cache for ``base``: the cached
    ``(timestamp, rates_dict)`` tuple, or ``None`` if nothing is cached for it.
    Lets callers report cache freshness without reaching into ``_rates_cache``."""
    return _rates_cache.get(base.upper())

# Crypto held as manual-account currencies; priced in USD via Coinbase's public
# spot API (no key) and folded into the base-relative rate map so the existing
# cross-rate converter handles them. USD prices are base-independent → cached once.
_CRYPTO_USD_SYMBOLS = ("BTC", "ETH")
_crypto_usd_cache: tuple[float, dict[str, float]] | None = None


async def _get_crypto_usd_prices() -> dict[str, float]:
    """Current USD price per 1 unit of each supported crypto. {} on failure."""
    global _crypto_usd_cache
    now = time.time()
    if _crypto_usd_cache and (now - _crypto_usd_cache[0]) < _CACHE_TTL:
        return _crypto_usd_cache[1]
    prices: dict[str, float] = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for symbol in _CRYPTO_USD_SYMBOLS:
                resp = await client.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot")
                resp.raise_for_status()
                amount = ((resp.json() or {}).get("data") or {}).get("amount")
                if amount:
                    prices[symbol] = float(amount)
    except Exception as e:
        logger.warning("Failed to fetch crypto prices: %s", e)
        return _crypto_usd_cache[1] if _crypto_usd_cache else prices
    if prices:
        _crypto_usd_cache = (now, prices)
    return prices


async def _augment_with_crypto(rates: dict[str, float]) -> None:
    """Add BTC/ETH to a base-relative rate map: rates[CRYPTO] = CRYPTO units per 1
    base = (USD per 1 base) / (USD per 1 CRYPTO). Needs the base's USD rate; a USD
    base already has rates['USD'] == 1.0."""
    usd_per_base = rates.get("USD")
    if not usd_per_base or usd_per_base <= 0:
        return
    for code, usd_price in (await _get_crypto_usd_prices()).items():
        if usd_price and usd_price > 0:
            rates[code] = float(usd_per_base) / float(usd_price)


async def _get_cad_anchored_rates() -> dict[str, float]:
    """CAD-anchored current rates ``{X: units of X per 1 CAD}``, BTC/ETH folded in.

    CAD is the canonical base: exchangerate-api.com always supports it, and the crypto
    augmentation needs the USD-per-base rate it returns. Cached under ``'CAD'``.
    """
    now = time.time()
    cached = _rates_cache.get("CAD")
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.exchangerate-api.com/v4/latest/CAD")
            resp.raise_for_status()
            rates = (resp.json() or {}).get("rates", {}) or {}
            rates["CAD"] = 1.0
            await _augment_with_crypto(rates)
            _rates_cache["CAD"] = (now, rates)
            return rates
    except Exception as e:
        logger.warning("Failed to fetch CAD FX rates: %s", e)
        if cached:
            return cached[1]
        return {"CAD": 1.0}


async def get_fx_rates(base_currency: str = "CAD") -> dict[str, float]:
    """Return exchange rates relative to ``base_currency``: ``rates[X]`` = units of X per
    1 base (so ``rates[base] == 1.0``).

    Always anchored on CAD (the only base exchangerate-api is guaranteed to support, and
    the one crypto pricing needs) then re-based to the requested base, so ANY selectable
    currency — fiat OR crypto (BTC/ETH) — works as the primary. Without this re-basing a
    crypto base hit exchangerate-api's missing-base path, returned a bare ``{BTC: 1.0}``,
    and every conversion silently passed through at 1:1 (200k CAD shown as 200k BTC).
    Cached per base for 1 hour.
    """
    base = base_currency.upper()
    now = time.time()

    cached = _rates_cache.get(base)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    cad_rates = await _get_cad_anchored_rates()
    if base == "CAD":
        return cad_rates  # already cached under 'CAD' by the anchor fetch

    base_per_cad = cad_rates.get(base)
    if not base_per_cad or base_per_cad <= 0:
        # Unknown/unpriced base — degrade to identity so callers never divide by zero.
        rates = {base: 1.0}
    else:
        # Re-base: (X per 1 CAD) / (base per 1 CAD) = X per 1 base.
        rates = {cur: val / base_per_cad for cur, val in cad_rates.items()}
        rates[base] = 1.0
    _rates_cache[base] = (now, rates)
    return rates
