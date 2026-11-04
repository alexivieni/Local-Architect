These are hardening findings from a scanner (Dockerfile/CI/config hygiene). Fix every real one with the smallest
change that does not change what the service/pipeline is for (pin versions, drop root, remove baked-in secrets to the
environment, add a healthcheck, fix YAML/CI issues). If a finding is a false positive here, leave it and say why in one line.

FINDINGS:
{findings}

CODE:
{context}

{feedback}
