---
name: secrets
description: Find hard-coded secrets and the places they leak
flow: security
args: a file, a folder, or nothing for the whole project
---
Search {args} for hard-coded passwords, API keys and tokens, secrets written to logs or put in URLs, and subprocess calls
built from user input. For each, give the file and line and move the secret to configuration or the environment.
