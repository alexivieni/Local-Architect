---
name: error-handling
description: Catch the narrowest exception, never swallow one, and fail with a message that says what to do
when: error handling, exception, try/except, try except, except Exception, bare except, raise, traceback, swallowed error, handle errors
---
# Handling errors

An exception is information. Throwing it away is how a bug becomes a mystery.

- **Catch the narrowest exception you can name.** `except FileNotFoundError:` not `except Exception:`. Never write a bare `except:`;
  it also catches Ctrl+C and `SystemExit`.
- **Never swallow silently.** `except X: pass` is only acceptable with a comment saying why the failure is expected and harmless.
  Otherwise log it, re-raise it, or return a value the caller must check.
- **Keep the try block small.** Wrap the one call that can fail, not the whole function, so you know which line raised.
- **Re-raise with context.** `raise ConfigError(f"cannot read {path}") from ex` keeps the original traceback.
- **Handle it where you can do something about it.** Low-level code raises; the top level (CLI, request handler) decides what the
  user sees. Do not catch an error only to print it and carry on with bad data.
- **Clean up with `with` or `finally`**, not by copying close calls into every branch.
- **The message says what failed and what to do**: the file, the value, the limit. "Something went wrong" helps nobody.

Write a test that triggers the error path. Untested except blocks are often broken.
