Write the design for the spec below. Output each file as:
DOC: relative/path
<file content, no code fence around the whole file>

Required files:
1. DOC: docs/architecture.md with headings: Overview, Components (a table: component | responsibility | owns data | talks to), Diagram (a ```mermaid flowchart or C4 diagram with at least 4 nodes and labelled edges), Data flow (numbered), Failure modes (what breaks, how we detect it, what happens), Testing strategy (what is tested how), Security and privacy basics.
2. DOC: docs/adr/0001-<slug>.md (and 0002... for each other significant decision): headings Status, Context, Decision, Alternatives considered (at least two, with why rejected), Consequences (good and bad).
{domain_files}
Reference spec requirement ids (R1, R2...) where a component or decision satisfies them. Prefer the simplest design that meets the requirements; justify any extra moving part.

SPEC:
{spec}

{context}
