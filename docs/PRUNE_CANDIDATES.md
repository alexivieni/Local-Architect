# Prune candidates (report-first, 2026-11-05)

Nothing here was deleted. Method: a word-boundary search of every top-level `*.py`, `*.bat`, `*.html`, `Makefile` and `tools/*.py` for each module name.

## Code with no reference outside its own selftest
| Module | Referenced only by | Note |
|---|---|---|
| `arch_batch.py` | `arch_selftest_cacheplus`, `arch_selftest_costs` | overnight half-price batches; CLAUDE.md describes it but no command or hub route imports it |
| `arch_iterate.py` | `arch_selftest_iterate` | no caller |
| `arch_overlap.py` | `arch_selftest_overlap` | new today, probably not wired yet (owner to confirm) |
| `arch_recipes.py` | `arch_selftest_contracts` | new today, probably not wired yet (owner to confirm) |
| `architect_embed.py` | `arch_selftest_ready` | public embedding API, KEEP |

Dynamic imports by string would also match the search, so these have no string references either. Confirm with the owner before removing.

## Other
- Old `agents/*.yaml`: none remain (all agents are `.md`).
- Root `architect_config.json.bak*` (11 files) and `hub_data/config.*.json`, `ledger.backup_*`, `*.bak-before-*`: generated backups, not deleted (they hold settings; the person should decide).
- Duplicate preset cards for the UI agent: grep `hub_ui.html` for preset ids shown both in Settings and Models before shipping; not verified here.
- `docs/` dated reports (`*_20261104.md`) are history, not stale; `docs/release_v13_7.html` is generated from the README release notes and can be rebuilt.
- README says both "the embedding model and the vector store are gone" (v13.4 section) and describes a vector index with `nomic-embed-text` (v-old section); the current truth is optional `retrieval.embed_model`, OFF by default. Fixed in the summary line; the long historical sections were left as release notes.
