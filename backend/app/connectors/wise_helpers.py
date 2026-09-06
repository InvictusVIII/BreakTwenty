from __future__ import annotations


CURRENCY_NAMES = {
    "CAD": "Canadian Dollar",
    "USD": "United States Dollar",
    "EUR": "Euro",
    "GBP": "British Pound",
    "AUD": "Australian Dollar",
    "NZD": "New Zealand Dollar",
    "JPY": "Japanese Yen",
    "CHF": "Swiss Franc",
    "SEK": "Swedish Krona",
    "NOK": "Norwegian Krone",
    "DKK": "Danish Krone",
    "PLN": "Polish Zloty",
    "CZK": "Czech Koruna",
    "HUF": "Hungarian Forint",
    "RON": "Romanian Leu",
    "BGN": "Bulgarian Lev",
    "HRK": "Croatian Kuna",
    "TRY": "Turkish Lira",
    "INR": "Indian Rupee",
    "BRL": "Brazilian Real",
    "MXN": "Mexican Peso",
    "SGD": "Singapore Dollar",
    "HKD": "Hong Kong Dollar",
    "MYR": "Malaysian Ringgit",
    "THB": "Thai Baht",
    "IDR": "Indonesian Rupiah",
    "PHP": "Philippine Peso",
    "ZAR": "South African Rand",
    "AED": "UAE Dirham",
    "ILS": "Israeli Shekel",
    "KRW": "South Korean Won",
    "CNY": "Chinese Yuan",
    "TWD": "Taiwan Dollar",
    "PKR": "Pakistani Rupee",
    "BDT": "Bangladeshi Taka",
    "LKR": "Sri Lankan Rupee",
    "NPR": "Nepalese Rupee",
    "EGP": "Egyptian Pound",
    "KES": "Kenyan Shilling",
    "NGN": "Nigerian Naira",
    "GHS": "Ghanaian Cedi",
    "UAH": "Ukrainian Hryvnia",
    "GEL": "Georgian Lari",
    "CLP": "Chilean Peso",
    "COP": "Colombian Peso",
    "PEN": "Peruvian Sol",
    "ARS": "Argentine Peso",
    "UYU": "Uruguayan Peso",
    "CRC": "Costa Rican Colon",
}

WISE_TX_TYPE_MAP = {
    "CARD": "withdrawal",
    "CONVERSION": "transfer",
    "DEPOSIT": "deposit",
    "MONEY_ADDED": "deposit",
    "TRANSFER": None,
    "DIRECT_DEBIT": "withdrawal",
    "BALANCE_INTEREST": "interest",
    "BALANCE_ADJUSTMENT": None,
}


def map_wise_type(details_type: str, tx_type: str, amount_value: float) -> str | None:
    mapped = WISE_TX_TYPE_MAP.get(details_type)
    if mapped is not None:
        return mapped
    if details_type == "TRANSFER":
        return "withdrawal" if tx_type == "DEBIT" else "deposit"
    if details_type == "BALANCE_ADJUSTMENT":
        return "withdrawal" if amount_value < 0 else "deposit"
    return None
