---
name: migrate
description: Move every use of an old API onto the new one, with the tests green
flow: migrate
args: "replace `old_name` with new_name", for example
---
Migrate: {args}

Find every use of the old API and move it to the new one, call site by call site. Keep behaviour identical: where the new API differs in
a default, an error type or an ordering, preserve the old behaviour and leave a one-line comment. Update the imports. Every existing test
must still pass, and nothing unrelated may change.
