# Runtime diagnosis fixture

This repository intentionally contains a defect in `pricing.py`.

`discounted_price_cents(10_000, 20)` should return `8_000`, but the current
implementation subtracts the percentage value as though it were an amount in
cents. The `check_*.py` files are discovered only when pytest runs from this
fixture repository using its local `pytest.ini`.
