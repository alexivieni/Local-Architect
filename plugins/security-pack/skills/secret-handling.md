---
name: secret-handling
description: Keep secrets out of code, logs and URLs
when: secret, password, token, api key, credential, leak, env
---
# Handling secrets

- Read secrets from the environment or a config file outside the repository; never commit them.
- Never log a secret, and never put one in a URL query string.
- Compare tokens with `hmac.compare_digest`, not `==`.
- Build subprocess arguments as a list; never pass user input through a shell.
