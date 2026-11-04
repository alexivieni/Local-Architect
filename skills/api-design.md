---
name: api-design
description: Design an HTTP or library interface that callers can use without reading its source
when: api design, endpoint, rest api, route, http status, status code, json response, api versioning, pagination, public interface
---
# Designing an interface

Callers will depend on whatever you ship, mistakes included. Get the shape right first.

- **Write the contract before the code**: the URL or function signature, the inputs, the success response, every error response.
  Put one example request and response in the docs or a test.
- **Nouns for resources, verbs from HTTP.** `GET /orders/42`, `POST /orders`, `DELETE /orders/42`. Not `/getOrder?id=42`.
- **Status codes mean something.** 200 ok, 201 created, 204 no content, 400 bad input, 401 not logged in, 403 not allowed,
  404 not found, 409 conflict, 422 failed validation, 500 our bug. Never return 200 with an error inside.
- **One error shape everywhere**, for example `{"error": {"code": "...", "message": "..."}}`.
- **Paginate any list** that can grow: a limit with a maximum, and a cursor or offset. Return the next cursor.
- **Make writes safe to retry.** PUT and DELETE are idempotent; for POST accept an idempotency key.
- **Do not break callers.** Adding an optional field is fine. Removing or renaming a field, or changing its type, needs a new version.
- **Accept little, return predictably.** Same field names and date format (ISO 8601, UTC) in every response.
