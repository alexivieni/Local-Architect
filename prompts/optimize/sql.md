Make these SQL queries avoid full table scans, WITHOUT changing their results. Prefer adding an index in a NEW migration file (never edit a migration that already exists); change a query only if an index cannot help.

GOAL: {goal}
QUERY PLANS NOW (SCAN = reads every row):
{plans}

FILES:
{context}

{feedback}
