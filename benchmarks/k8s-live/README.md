# LIVE Kubernetes Benchmark Cases

Cases in this directory are declarative inputs for `scripts/run_live_benchmark.py`.
They require a real Kubernetes Environment with `ops.mode=real` and
`ops.kubernetes.fallback_policy=fail_closed`. The runner rejects mock, replay,
or unavailable observations and writes only redacted artifacts.
