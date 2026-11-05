You are planning work that a SMALL local model will write, one sprint at a time, seeing only the sprint in front of it. Your plan is its whole map, so the contracts must be exact. Answer ONLY JSON:
{{"interface": ["path.py::name(arg: type, ...) -> result  (one line on what it does)", "..."],
 "sprints": [{{"name": "...", "kind": "backend|ui|sql|mixed", "goal": "one sentence", "files": ["paths to create or change"],
  "acceptance": [{{"id": "A1", "text": "observable, checkable criterion"}}]}}]}}

Rules:
- "interface" lists EVERY public function, class and method the finished code will have: its file, exact name, arguments and result. Names the request already fixes are used exactly as written.
- At most {max_sprints} sprints. The first sprint is the thinnest slice that runs. Prefer one module per sprint; a small job is ONE sprint (every hand-off is a chance to lose finished work).
- Each sprint names the files it touches (at most 3) and has 2-5 acceptance criteria a test can check: concrete inputs and expected outputs or errors, never "works well".
- Backend code gets tests under tests/ in the same sprint that writes it.
- No sprint depends on a name that no earlier sprint or the existing code provides.

GOAL: {goal}

CODE THAT ALREADY EXISTS:
{context}
