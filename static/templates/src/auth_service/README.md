# __NAME__

A small, careful authentication service. Standard library only.

    AUTH_SECRET=change-me python -m __PKG__ --port 8085 --db auth.db
    python -m pytest -q

## Endpoints (JSON)

| method | path | body | result |
|--------|------|------|--------|
| POST | /register | `{"email","password"}` | 201 `{"id"}` |
| POST | /login | `{"email","password"}` | 200 `{"session","access_token","expires_in"}` |
| GET | /me | header `Authorization: Bearer <access_token>` | 200 `{"id","email"}` |
| POST | /logout | header `Authorization: Session <session>` | 204 |

## What it does right

- Passwords: PBKDF2-HMAC-SHA256, 200k iterations, 16-byte random salt, constant-time compare.
- Sessions: random 32-byte tokens; the database keeps only their SHA-256, so a leaked DB cannot log anyone in.
- Throttling: 5 failed logins for an email lock it for 5 minutes (counts reset on success).
- Access tokens: `base64(payload).base64(hmac)` with an expiry; the secret comes from `AUTH_SECRET`.
- Errors never say whether the email exists.
