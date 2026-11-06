"""Money in integer minor units, PayPal currency rules and indicative FX."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Currencies PayPal accepts for payments and payouts (developer.paypal.com currency codes).
PAYPAL_CURRENCIES = {
    "AUD", "BRL", "CAD", "CNY", "CZK", "DKK", "EUR", "HKD", "HUF", "ILS", "JPY", "MYR",
    "MXN", "TWD", "NZD", "NOK", "PHP", "PLN", "GBP", "SGD", "SEK", "CHF", "THB", "USD",
}
# PayPal requires whole numbers for these.
ZERO_DECIMAL = {"JPY", "HUF", "TWD"}

# Indicative rates: units of currency per 1 USD. Shown to users as indicative only;
# the amounts charged are whatever PayPal reports on the order and capture.
FX_AS_OF = "1 Nov 2026"
USD_RATES = {
    "USD": Decimal("1"), "EUR": Decimal("0.92"), "GBP": Decimal("0.78"), "CAD": Decimal("1.38"),
    "AUD": Decimal("1.52"), "JPY": Decimal("152"), "MXN": Decimal("19.6"), "PHP": Decimal("57.5"),
    "SGD": Decimal("1.33"), "INR": Decimal("84.5"), "KES": Decimal("129"), "NGN": Decimal("1650"),
    "ZAR": Decimal("18.1"), "IDR": Decimal("15800"), "PKR": Decimal("278"), "BDT": Decimal("120"),
    "GHS": Decimal("15.6"), "LKR": Decimal("298"), "NPR": Decimal("135"), "COP": Decimal("4300"),
    "PLN": Decimal("4.0"), "THB": Decimal("34"), "MYR": Decimal("4.4"), "BRL": Decimal("5.7"),
}

SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥", "PHP": "₱",
    "NGN": "₦", "KES": "KSh ", "MXN": "MX$", "CAD": "CA$", "AUD": "A$", "SGD": "S$", "ZAR": "R ",
}


def exponent(currency: str) -> int:
    return 0 if currency.upper() in ZERO_DECIMAL else 2


def to_minor(value, currency: str) -> int:
    """Parse '84.20', 84.2 or Decimal into minor units. Raises ValueError."""
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {value!r}") from exc
    if d.is_nan() or d.is_infinite():
        raise ValueError(f"not an amount: {value!r}")
    q = Decimal(1).scaleb(-exponent(currency))
    return int((d.quantize(q, rounding=ROUND_HALF_UP) * (10 ** exponent(currency))).to_integral_value())


def to_value(minor: int, currency: str) -> str:
    """Minor units to PayPal's string value, e.g. 8420 USD -> '84.20', 500 JPY -> '500'."""
    e = exponent(currency)
    if e == 0:
        return str(int(minor))
    sign = "-" if minor < 0 else ""
    minor = abs(int(minor))
    return f"{sign}{minor // 100}.{minor % 100:02d}"


def fmt(minor: int, currency: str) -> str:
    value = Decimal(to_value(minor, currency))
    e = exponent(currency)
    text = f"{value:,.{e}f}"
    sym = SYMBOLS.get(currency)
    return f"{sym}{text}" if sym else f"{text} {currency}"


def supported(currency: str) -> bool:
    return currency.upper() in PAYPAL_CURRENCIES


def convert(minor: int, src: str, dst: str) -> int:
    """Indicative conversion between currencies, in minor units of dst."""
    src, dst = src.upper(), dst.upper()
    if src == dst:
        return int(minor)
    if src not in USD_RATES or dst not in USD_RATES:
        raise ValueError(f"no indicative rate for {src} or {dst}")
    value = Decimal(to_value(minor, src))
    usd = value / USD_RATES[src]
    return to_minor(usd * USD_RATES[dst], dst)


def settlement_for(currency: str) -> str:
    """Bills in a PayPal currency settle in it; others settle in USD."""
    return currency.upper() if supported(currency) else "USD"
