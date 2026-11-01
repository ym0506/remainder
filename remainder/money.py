"""Money is held as integer cents everywhere. Strings only at the edges (PayPal, screen)."""

from decimal import Decimal, InvalidOperation


def to_cents(value):
    """Parse "1,000.00", "$15", "15.5" into cents. Rejects fractions of a cent."""
    text = str(value).strip().replace(",", "").replace("$", "")
    try:
        cents = Decimal(text) * 100
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {value!r}") from exc
    if not cents.is_finite():
        raise ValueError(f"not an amount: {value!r}")
    if cents != cents.to_integral_value():
        raise ValueError(f"more than two decimals: {value!r}")
    return int(cents)


def to_str(cents):
    """Cents to the two-decimal string PayPal expects for USD."""
    if cents < 0:
        raise ValueError(f"negative amount: {cents}")
    return f"{cents // 100}.{cents % 100:02d}"


def display(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"
