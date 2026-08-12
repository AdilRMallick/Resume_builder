# LLM Gateway

Source: [AdilRMallick/llm_gateway](https://github.com/AdilRMallick/llm_gateway),
including its committed benchmark artifacts and reproduction instructions.

## Architecture and reliability

- Built one asynchronous FastAPI chat interface across Anthropic, OpenAI, and Google with normalized request and response shapes.
- Implemented cheapest, fastest, and pinned routing policies using estimated request cost and rolling provider health.
- Added full-jitter retry and automatic provider failover, Redis response caching with single-flight locks, and batched PostgreSQL cost accounting.
- Used Envoy for TLS termination, cross-replica edge rate limiting, and structured access logs; Docker Compose provides reproducible local orchestration.
- Built a fault-injecting mock service for all three provider wire formats so CI and benchmarks run deterministically without API keys or spend.

## Reproducible measurements

- Reduced cached-response latency from 127.64 milliseconds to 3.48 milliseconds at p50, a 37-times improvement, and from 129.61 milliseconds to 4.92 milliseconds at p95, a 26-times improvement.
- Demonstrated automatic recovery from injected OpenAI 503 responses through Google, then from concurrent OpenAI 503 and Google 429 responses through Anthropic; service recovered without operator action after faults were cleared.
- Reduced modeled cost by 94.1 percent versus the pinned Anthropic baseline on the same seeded 60-request workload by combining cheapest-provider routing with a 50-percent-repeat cache workload.
- Recorded benchmark conditions, cold-versus-warm cache state, input workloads, request counts, and full result artifacts so each metric can be reproduced and audited.

## Verification

- Tested provider adapters, routing, cache keys, Redis-outage degradation, concurrent cache misses, retries, failover, accounting, Envoy TLS, and rate limiting with pytest, Testcontainers, and GitHub Actions.
- Kept liveness dependency-free, required PostgreSQL for readiness, and allowed Redis failures to degrade to uncached operation rather than failing served requests.
