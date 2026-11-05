Plan the work as small SPRINTS that each leave the project working. Answer ONLY JSON:
{{"interface": ["path.py::name(args) -> result", "..."],
 "sprints": [{{"name": "...", "kind": "backend|ui|sql|mixed", "goal": "one sentence", "files": ["paths you expect to create or change"],
  "acceptance": [{{"id": "A1", "text": "observable, checkable criterion"}}]}}]}}
Rules: at most {max_sprints} sprints; the first sprint is the thinnest working slice; one module per sprint where you can; every sprint has 2-5 acceptance criteria that a test or a script can check; backend code gets tests under tests/. "interface" lists every public name the finished code will have, with its arguments; reuse the names the brief says already exist instead of inventing new ones.

GOAL: {goal}

BRIEF FROM THE SCOUT (what exists; if it is empty or says it stopped, rely on the code below):
{brief}

CODE:
{context}
