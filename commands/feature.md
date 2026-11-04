---
name: feature
description: Add one feature end to end, contract first
flow: feature
args: the feature, in one or two sentences
---
Add this feature: {args}

Start with the contract: the function signature or endpoint, its inputs, outputs and errors. Write a test for that contract that fails now.
Then implement the smallest version that makes it pass, wired in end to end so a user can actually reach it. Run the whole suite.
Done means the new test passes, the old tests still pass, and you can name the file a user calls. Do not add a second feature, a new
dependency, or a rewrite of nearby code.
