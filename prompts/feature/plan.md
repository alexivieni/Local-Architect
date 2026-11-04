Plan ONE feature across all layers as small SPRINTS, contract first. Answer ONLY JSON:
{{"sprints": [{{"name": "...", "kind": "mixed", "goal": "one sentence", "files": ["paths you expect to create or change"],
  "acceptance": [{{"id": "A1", "text": "observable, checkable criterion"}}]}}]}}
Order: (1) the contract: data shape/schema and the API request/response/error shapes; (2) data layer + API; (3) UI that uses exactly that contract (empty, loading, error and success states); (4) an end-to-end check through the public interface.
Rules: at most {max_sprints} sprints; each leaves the project working; every sprint has 2-5 acceptance criteria that a test or script can check; backend code gets tests under tests/.

FEATURE: {goal}

{context}
