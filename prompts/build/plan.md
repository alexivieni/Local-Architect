Plan the work as small SPRINTS that each leave the project working. Answer ONLY JSON:
{{"sprints": [{{"name": "...", "kind": "backend|ui|sql|mixed", "goal": "one sentence", "files": ["paths you expect to create or change"],
  "acceptance": [{{"id": "A1", "text": "observable, checkable criterion"}}]}}]}}
Rules: at most {max_sprints} sprints; the first sprint is the thinnest working slice; every sprint has 2-5 acceptance criteria that a test or a script can check; backend code gets tests under tests/.

GOAL: {goal}

{context}
