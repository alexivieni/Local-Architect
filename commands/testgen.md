---
name: testgen
description: Write the tests that are missing, and prove they catch something
flow: test
args: a file or module to cover
---
Write the tests that are missing for {args}.

Cover what the code promises, not what it happens to do: the ordinary case, the empty case, the boundary, and the error path.
Each test asserts one thing and is named after the behaviour it checks. Before you finish, confirm the tests fail when the behaviour
is broken; a test that passes against broken code is worse than no test.
