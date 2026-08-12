from pricing import discounted_price_cents


def test_percentage_discount_is_applied_to_the_price() -> None:
    assert discounted_price_cents(10_000, 20) == 8_000
