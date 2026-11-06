# data (local)

The Anthropic data-analyst plugin, run by the models on this machine. Ten commands, each with the same name and arguments
as the original, each loading its skill page and (once the harness patch in the plugin report lands) running its workflow:

| Command | Arguments | Workflow (agents) |
|---------|-----------|-------------------|
| `/analyze` | `<question>` | scope -> analyse -> validate + recompute (parallel) -> report |
| `/explore-data` | `<table or file>` | structure / quality / patterns profilers (parallel) -> Data Profile |
| `/write-query` | `<description of what data you need>` | write -> best-practice review -> rewrite |
| `/create-viz` | `<data source> [chart type]` | chart selection -> code |
| `/build-dashboard` | `<description> [data source]` | plan -> build one self-contained HTML file |
| `/validate-data` | `<analysis to review>` | methodology / QA checklist / pitfalls / visuals lenses + two independent recomputes (parallel) -> Validation Report |
| `/data-context-extractor` | `<company or domain>` | discover schema -> write skill + reference files |
| `/sql-queries` | `<request>` | write -> review -> rewrite |
| `/statistical-analysis` | `<data and question>` | compute -> caution check -> report |
| `/data-visualization` | `<data and goal>` | chart selection -> code |

Enable: `python architect.py plugins install plugins/data` (or it is found under plugins/), then `plugins enable data`.
Run a workflow directly: `python architect.py crew data:validate-data --goal "..." [--fake]`.
Tests (offline): `py -3.12 -m pytest -q plugins/data/tests`.
See NOTICE.md (Apache-2.0, adapted) and CONNECTORS.md.
