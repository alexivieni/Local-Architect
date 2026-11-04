---
name: fix
description: Reproduce a bug with a failing test, then fix it
flow: bugfix
args: what goes wrong, in your own words
---
Fix this: {args}

Start by reproducing it in a test that fails for the right reason. Then make the smallest change that makes it pass. Then run the rest
of the suite to be sure nothing else broke. If you cannot reproduce it, do not change anything: say what you tried and what you would
need to know.
