"""Demo module: an order total with a discount rule that must never silently change."""

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    if quantity < 0:
        raise ValueError("quantity can't be negative")
    return (unit_price * quantity).quantize(CENT, ROUND_HALF_UP)


# SHIELD: discount-rule
def discount(subtotal: Decimal, loyalty_years: int) -> Decimal:
    """1% per loyalty year, capped at 10%, never applied to orders under 50."""
    if subtotal < Decimal("50"):
        return Decimal("0.00")
    rate = min(loyalty_years, 10) * Decimal("0.01")
    return (subtotal * rate).quantize(CENT, ROUND_HALF_UP)
# SHIELD-END: discount-rule


def order_total(lines: list[tuple[Decimal, int]], loyalty_years: int = 0) -> Decimal:
    subtotal = sum((line_total(p, q) for p, q in lines), Decimal("0.00"))
    return subtotal - discount(subtotal, loyalty_years)
