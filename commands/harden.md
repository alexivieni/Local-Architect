---
name: harden
description: Add error handling and input validation without changing behaviour
flow: harden
args: a file, module or entry point
---
Harden {args}.

Find where untrusted input enters and validate it there: type, range, length, allowed values, with a clear error. Replace bare or broad
except blocks with the narrowest exception, and remove any that swallow errors silently. Add timeouts to network calls. Behaviour for
valid input must not change: the existing tests must pass unchanged, and add a test for each new rejection. Do not rename, restructure or
reformat code that does not need it.
