You are the second reader. Several reviewers each read the code below through one lens and reported findings. Most reviewers over-report; your job is to keep ONLY the findings you can confirm yourself in the code shown.

For each finding:
- Find the quoted code in CODE below. If you cannot find it, or it does not do what the finding says, DROP the finding.
- If two reviewers report the same problem, keep it once.
- Keep the severity only if the code supports it; lower it otherwise.

Answer in Markdown with exactly these sections:
## Kept
One bullet per kept finding: [severity] path.py:function -- the problem -- `the code that shows it` -- the smallest fix -- kind: bug | risk | design
("bug" = wrong behaviour a test could show; "risk" = unsafe but hard to test; "design" = structure.)
## Dropped
One bullet per dropped finding, with the reason in a few words (not in the code, misread, duplicate, style only).

Never add a finding no reviewer reported.

REQUEST: {goal}

FACTS FROM TOOLS:
{facts}

CODE:
{context}

THE REVIEWERS' FINDINGS (a pass shown as a name in braces did not finish):

### correctness
{steps.lens@1.text}

### failure paths
{steps.lens@2.text}

### security and data
{steps.lens@3.text}
