Fix or change this CI/CD pipeline. Reply with FILE:/EDIT: blocks for the pipeline files only.

REQUEST:
{goal}

Rules: keep what the pipeline already does; never turn a failing step green by deleting it, adding `|| true`, `continue-on-error` or an echo; run the commands this
repository really has (see the findings); first-party actions pinned to a major version, third-party actions to a commit SHA only if you know it (otherwise leave the tag);
`permissions: contents: read` unless a job needs more; add `timeout-minutes`; untrusted event text goes through `env:`.

CURRENT PIPELINE FILES AND PROJECT CONTEXT:
{context}

BLOCKING FINDINGS (each is a failure the first run would hit, or a security problem). Fix every one:
{findings}

{feedback}
