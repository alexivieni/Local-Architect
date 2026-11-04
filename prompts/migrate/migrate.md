Migrate these call sites as the request says.

REQUEST: {goal}

Change every listed use of the old API to the new one. Keep behaviour identical: if the new API differs in an edge case (a default,
an error type, ordering), preserve the old behaviour explicitly and leave a one-line comment saying so. Update imports. Do not
change unrelated code. Every test must still pass.

REMAINING USES:
{findings}

CODE:
{context}

{feedback}
