---
name: writing-docs
description: Write docstrings, READMEs and comments that answer what a reader actually needs
when: documentation, docstring, docstrings, readme, write docs, document this, comments, usage example, changelog
files: *.md, *.rst, docs/*
---
# Writing docs

Docs are for someone who has not read the code and has five minutes.

- **A README answers, in order:** what this is in one sentence, how to install it, how to run it, one copy-paste example, how to
  run the tests, where to get help. Commands must be exact and must work.
- **A docstring says what, not how.** What the function does, what each argument means, what it returns, what it raises, and any
  side effect (writes a file, sends a request). One line is enough for a simple function.
- **Comment the why.** `# retry because the API returns 503 during deploys` is useful. `# increment i` is not.
- **Examples beat prose.** One real call with real output teaches more than a paragraph.
- **Match the code exactly.** Read the function before you document it. A wrong doc is worse than none; check argument names
  and defaults against the signature.
- **Use the project's existing style**: Google, NumPy or reST docstrings, whatever is already there.
- **Do not change code while writing docs.** If you find a bug, report it separately.
- **Date or version anything that will go stale**, such as supported versions or limits.
