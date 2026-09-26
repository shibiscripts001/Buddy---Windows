#!/usr/bin/env python3
"""
Currency list + formatting shared by the project-rate UI and Reports'
earnings display/exports.
"""

# (code, symbol, display name). Symbols aren't unique (several currencies
# use "$") - format_money() always appends the code too so amounts are
# never ambiguous about which currency they're actually in.
CURRENCIES = [
    ("USD", "$", "US Dollar"),
    ("EUR", "€", "Euro"),
    ("GBP", "£", "British Pound"),
    ("JPY", "¥", "Japanese Yen"),
    ("CAD", "$", "Canadian Dollar"),
    ("AUD", "$", "Australian Dollar"),
    ("CHF", "Fr", "Swiss Franc"),
    ("CNY", "¥", "Chinese Yuan"),
    ("INR", "₹", "Indian Rupee"),
    ("BRL", "R$", "Brazilian Real"),
    ("MXN", "$", "Mexican Peso"),
    ("KRW", "₩", "South Korean Won"),
    ("SEK", "kr", "Swedish Krona"),
    ("NOK", "kr", "Norwegian Krone"),
    ("NZD", "$", "New Zealand Dollar"),
    ("SGD", "$", "Singapore Dollar"),
    ("HKD", "$", "Hong Kong Dollar"),
    ("ZAR", "R", "South African Rand"),
    ("RUB", "₽", "Russian Ruble"),
    ("AED", "د.إ", "UAE Dirham"),
]

DEFAULT_CURRENCY = "USD"
CURRENCY_CODES = [code for code, _, _ in CURRENCIES]
CURRENCY_SYMBOLS = {code: symbol for code, symbol, _ in CURRENCIES}


def format_money(amount, currency_code, short=False):
    """short=True omits the trailing currency code (tighter spaces, e.g.
    inline bar rows) - full form (default) always includes it, since
    several currencies share the same symbol."""
    symbol = CURRENCY_SYMBOLS.get(currency_code, "")
    text = f"{symbol}{amount:,.2f}"
    return text if short else f"{text} {currency_code}"
