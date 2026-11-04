Make this code fail SAFELY and LOUDLY. These findings come from reading the code's syntax tree, so the file and line are exact.
Fix each one with the smallest change:
- a bare `except:` -> catch the specific exception that can really happen there;
- an error caught and ignored -> handle it, log it, or let it propagate; never silence it;
- a network call with no timeout -> add a timeout;
- `shell=True` -> pass the command as a list;
- `assert` used to check input -> raise ValueError/TypeError with a clear message;
- a file opened outside `with` -> use `with`.
Behaviour for valid input must NOT change and every existing test must still pass. If a finding is deliberate (say, an intentional
ignore), leave it and add a one-line comment explaining why.

FINDINGS:
{findings}

CODE:
{context}

{feedback}
