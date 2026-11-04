---
name: http-and-retries
description: Call other services with timeouts, bounded retries with backoff, and only for idempotent requests
when: retry, retries, backoff, timeout, http request, http client, requests.get, urllib, httpx, rate limit, 429, 503, flaky network, webhook
---
# HTTP calls and retries

Every network call can hang, fail or succeed twice. Plan for all three.

- **Always set a timeout.** `requests.get(url, timeout=(3, 10))`. The default in many libraries is to wait forever.
- **Retry only what is safe to repeat.** GET, PUT and DELETE are idempotent. Retry a POST only with an idempotency key the server
  honours, or you may charge a customer twice.
- **Retry only transient failures**: connection errors, timeouts, 429, 502, 503, 504. Never retry 400, 401, 403, 404 or 422;
  they will fail the same way again.
- **Back off exponentially with jitter and a cap**: wait about 0.5s, 1s, 2s, 4s plus a random fraction, at most 3 to 5 attempts and
  a total time limit. Honour a `Retry-After` header when present.
- **Check the status code.** Call `raise_for_status()` or test `status_code`; a 500 response is not an exception by default.
- **Reuse a session or client** for connection pooling instead of a new connection per call.
- **Log each retry** with the URL (no secrets in query strings), attempt number and status.
- **Verify TLS.** Never set `verify=False` to make an error go away.

In tests, do not hit the real network. Replace the client with a fake that returns the failures you want to exercise.
