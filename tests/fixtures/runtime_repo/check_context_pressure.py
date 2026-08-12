from pricing import discounted_price_cents


def test_large_test_artifact_still_reports_the_real_failure() -> None:
    # The Runtime must persist this as an Artifact rather than insert the whole
    # output into every model request. The counter makes the output deterministic
    # while avoiding a large fixture file in the repository.
    for index in range(4_000):
        print(f"irrelevant diagnostic trace {index:04d}: cache_key=pricing-{index * 17}")

    assert discounted_price_cents(10_000, 20) == 8_000
