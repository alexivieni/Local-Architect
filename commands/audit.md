---
name: audit
description: Adversarially hunt for bugs, each confirmed by a failing test
flow: hunt
args: a file, module, or nothing for the whole project
---
Hunt for real bugs in {args}.

Think like someone trying to break it: empty and huge inputs, None, unicode, boundaries, wrong order of calls, concurrent use, failures
from the disk or network. A suspicion is not a finding: each bug you report must come with a test that fails now. Give the file, line,
the failing test and the smallest fix. Report fewer, confirmed bugs rather than many guesses, and do not count style issues.
