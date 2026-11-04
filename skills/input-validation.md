---
name: input-validation
description: Validate untrusted input once at the boundary, then trust it inside
when: validate, validation, user input, sanitize, form input, request body, query parameter, untrusted input, invalid input, bad input
---
# Validating input

Check data where it enters the program, once, and then trust it.

- **Find the boundary.** Input arrives from a request, a form, a CLI argument, a file, an environment variable or another service.
  Validate there. Inner functions should not re-check the same thing; that is noise and drifts out of sync.
- **Allow-list, do not block-list.** State what is valid (type, range, length, allowed characters, allowed values) and reject
  everything else. Trying to list every bad value always misses one.
- **Parse into a real type.** Turn the string into an `int`, a `date`, an enum or a dataclass at the boundary. Code inside then works
  with values that cannot be malformed.
- **Check limits.** Maximum length of strings, size of uploads, number of items in a list. Missing limits are how a single request
  exhausts memory.
- **Reject with a clear message**: which field, what was wrong, what is allowed. Return 400-style errors, not a crash.
- **Validation is not escaping.** Still use parameterised queries for SQL, argument lists for subprocess, and template escaping for
  HTML, even for input you validated.
- **Paths:** resolve the path and confirm it is inside the allowed folder before opening it.

Test at least: a valid value, an empty value, a too-long value, a wrong type.
