---
name: ci
description: Make the CI pipeline run the tests and fail on a failing test
flow: cicd
args: optional, the CI system or workflow file
---
Make continuous integration run this project's tests{args}.

Use the CI system already in the repo; if there is none, add one workflow file. It must install the project, run the full test suite with
the same command a developer uses locally, and exit non-zero when any test fails. Prove that: say how you checked a failing test makes
the pipeline fail. Pin the language version. Do not skip, mark as allowed-to-fail or delete tests to get a green run, and add no secrets.
