# DRISHTI Load Tests (k6)

Run against STAGING only (try-ons cost GPU money; never load-test prod).

## Smoke / pre-launch gate

```bash
BASE_URL=https://staging-api.drishti.example \
TEST_KEY=$STAGING_TEST_KEY TEST_OTP=000000 \
k6 run scenarios/mixed_smoke.js
```

## Isolate the GPU path (capacity planning)

```bash
k6 run --vus 10 --duration 9m scenarios/tryon.js
```

## What the thresholds assert

- reco p95 < 800ms (cached read path)
- try-on submit returns 202 fast; full e2e p50 < 15s, p95 < 30s (realistic T4/L40S)
- <2% error rate, >85% quality-gate pass

## Interpreting results

- If `tryon_e2e` p95 blows past 30s → not enough GPU workers; check KEDA max replicas
  and the p0/p1 queue depth in Grafana (drishti-vtoe dashboard) during the run.
- If reco p95 > 800ms → cache miss rate too high or Qdrant under-provisioned.
- This validates capacity & SLOs; it does NOT validate render quality (use the
  visual-regression suite for that).
