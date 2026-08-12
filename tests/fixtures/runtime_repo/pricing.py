"""Small deterministic repository used by the Runtime diagnosis tests."""


def discounted_price_cents(original_price_cents: int, discount_percent: int) -> int:
    """Return the price after applying a percentage discount.

    Both arguments are integers. The return value must remain an integer number
    of cents.
    """
    return original_price_cents - discount_percent
