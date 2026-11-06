"""The template catalog: everything a gallery needs to show the project starters well.

    catalog()                    -> JSON-able list, one entry per template in arch_templates.TEMPLATES
    entry(tid)                   -> one entry
    svg(tid)                     -> the thumbnail's SVG text (static/templates/<id>.svg)
    render(tid, dest, vars=None) -> write the starter into a folder; same files as arch_templates.render()
    write_svgs()                 -> regenerate every thumbnail from GLYPHS (run once after adding a template)

Colours are theme TOKEN names (accent, green, amber, violet, red, teal, blue, pink), never hex: the hub maps a
token to its own CSS variable, so light and dark themes both work. Thumbnails draw with currentColor and
var(--tpl-accent, currentColor) for the same reason, and load nothing external.
"""
import ast
import functools
import json
import os
import re

import arch_templates as T

HERE = os.path.dirname(os.path.abspath(__file__))
SVG_DIR = os.path.join(HERE, "static", "templates")
CATEGORIES = ("web", "cli", "data", "ai", "game", "infra", "security", "desktop", "extension", "bot")
COLOURS = ("accent", "green", "amber", "violet", "red", "teal", "blue", "pink")
DIFFICULTY = ("beginner", "intermediate", "advanced")

# id: (category, colour, difficulty, languages, one-line summary, next steps)
META = {
    "empty": ("cli", "accent", "beginner", ["any"], "A blank folder with a README, a smoke test and a .gitignore.",
              ["Describe the goal in README.md", "Let a flow build the first module", "Replace the smoke test with real tests"]),
    "agent_tool": ("ai", "violet", "intermediate", ["python"], "An agent loop on a local model with timed, capped tools.",
                   ["Add a tool to the dispatcher", "Point it at your Ollama model", "Add a scripted test for the new tool"]),
    "mcp_server": ("ai", "violet", "intermediate", ["python"], "A Model Context Protocol server over stdio with tested tools.",
                   ["Add your own tool and its schema", "Register it in your MCP client", "Test tools/call with bad input"]),
    "python_lib": ("cli", "blue", "beginner", ["python"], "A package with a pure core, a thin CLI and tests.",
                   ["Rename the core functions", "Publish with python -m build", "Add type hints and a docs page"]),
    "worker": ("infra", "amber", "intermediate", ["python", "sql"], "A durable SQLite job queue with retries and dead letters.",
               ["Write a real handler", "Run the worker as a service", "Add a status page"]),
    "security_lab": ("security", "red", "advanced", ["python"], "Red and blue sides of the same findings, scope-checked.",
                     ["Add a scenario", "Write a detection rule for it", "Grade an attack path against scope"]),
    "red_team": ("security", "red", "advanced", ["python"], "Grades proposed attack paths against evidence and scope.",
                 ["Load your findings", "Tighten the scope file", "Report the rejected paths"]),
    "range": ("security", "red", "advanced", ["python"], "A real loopback-only practice target to test against.",
              ["Add a deliberately weak endpoint", "Write the test that proves it", "Fix it and watch the test flip"]),
    "rest_api": ("web", "accent", "beginner", ["python", "sql"], "A JSON API on http.server and SQLite, no dependencies.",
                 ["Add a resource", "Extend openapi.json", "Add pagination tests"]),
    "fullstack": ("web", "accent", "intermediate", ["python", "javascript", "html", "css"], "Accessible front end on a tested JSON API.",
                  ["Add a form for a new field", "Add an API test for it", "Style the empty and error states"]),
    "static_web": ("web", "teal", "beginner", ["html", "css", "javascript"], "A no-build web page with localStorage and dark mode.",
                   ["Change the content", "Add a second page", "Deploy to any static host"]),
    "data_sql": ("data", "green", "intermediate", ["sql", "python"], "Versioned migrations, constraints and named queries.",
                 ["Add a migration", "Write a named query", "Test a constraint rejects bad rows"]),
    "fastapi": ("web", "accent", "intermediate", ["python"], "A typed, auto-documented API with Pydantic (needs fastapi).",
                ["pip install -r requirements.txt", "Add a router", "Open /docs"]),
    "network_service": ("infra", "blue", "advanced", ["python"], "A framed, checksummed TCP protocol on loopback.",
                        ["Add a message type", "Fuzz the decoder", "Measure throughput"]),
    "iot_device": ("infra", "teal", "advanced", ["python", "sql"], "A device fleet backend with safe OTA updates.",
                   ["Add a sensor type", "Tighten the OTA gate", "Chart the readings"]),
    "cli_tool": ("cli", "blue", "beginner", ["python"], "Subcommands, --json output and documented exit codes.",
                 ["Rename the commands for your domain", "Add a subcommand", "Ship it with a console script"]),
    "tui_app": ("desktop", "violet", "intermediate", ["python"], "A curses picker with a pure, fully tested model.",
                ["Add a key binding", "Show item details", "pip install windows-curses on Windows"]),
    "etl_pipeline": ("data", "green", "intermediate", ["python", "sql"], "CSV in, validated rows into SQLite, rejects reported.",
                     ["Point it at your CSV", "Add a validation rule", "Schedule the load"]),
    "state_machine": ("cli", "violet", "intermediate", ["python"], "A declarative state machine with guarded transitions.",
                      ["Model your workflow", "Add a guard", "Render the diagram"]),
    "event_bus": ("infra", "amber", "intermediate", ["python"], "Publish/subscribe with replay from an event log.",
                  ["Add an event type", "Write a subscriber", "Replay from a point in time"]),
    "cache_layer": ("infra", "amber", "intermediate", ["python"], "A TTL + LRU cache with tested expiry.",
                    ["Wrap a slow function", "Tune size and TTL", "Add hit-rate metrics"]),
    "plugin_host": ("infra", "violet", "advanced", ["python"], "Discover, validate and isolate plugins.",
                    ["Write a plugin", "Add a capability to the contract", "Reject a bad plugin in a test"]),
    "docs_site": ("web", "teal", "beginner", ["python", "html", "css"], "Markdown in docs/ becomes a static HTML site.",
                  ["Write your pages", "Change the theme colours", "Publish the out/ folder"]),
    "tool_server": ("ai", "violet", "intermediate", ["python"], "Typed tools over local HTTP for an agent to call.",
                    ["Add a tool", "Cap its output", "Call it from an agent"]),
    "agent_fleet": ("ai", "violet", "advanced", ["python"], "Several agents at once, one file owner each, three outcomes.",
                    ["Describe your jobs", "Set budgets", "Read the not_sure results"]),
    "lease_queue": ("infra", "amber", "advanced", ["python", "sql"], "At-least-once jobs with leases and visibility timeouts.",
                    ["Run two workers", "Kill one mid-job", "Watch the lease expire and retry"]),
    "api_gateway": ("infra", "blue", "advanced", ["python"], "A rate-limited gateway with token buckets.",
                    ["Put it in front of rest_api", "Tune the buckets", "Add per-key limits"]),
    "event_store": ("data", "green", "advanced", ["python", "sql"], "Append-only events with optimistic concurrency.",
                    ["Model an aggregate", "Add a projection", "Test a concurrent write conflict"]),
    "model_cli": ("ai", "violet", "beginner", ["python"], "A command line over your local model with a reply cache.",
                  ["Point it at your model", "Add a prompt file", "Check cache hits"]),
    "telemetry_collector": ("infra", "teal", "advanced", ["python"], "Metrics and traces over loopback HTTP, with percentiles.",
                            ["Send spans from your app", "Read p95 per route", "Add an alert threshold"]),
    "fts_rag": ("ai", "violet", "intermediate", ["python", "sql"], "Retrieval over SQLite FTS5 with cited answers.",
                ["Index your notes", "Ask a question", "Check the citations"]),
    "auth_service": ("security", "red", "intermediate", ["python", "sql"], "Users, hashed sessions, lockout and signed tokens.",
                     ["Set AUTH_SECRET", "Add password reset", "Put it behind HTTPS"]),
    "chat_bot": ("bot", "pink", "beginner", ["python"], "A platform-free bot core with Slack and Discord adapters.",
                 ["Add a command", "Wire the Slack adapter", "Keep tokens in environment variables"]),
    "chrome_extension": ("extension", "amber", "beginner", ["javascript", "html", "json"], "A Manifest V3 extension with a popup and storage.",
                         ["Load unpacked in chrome://extensions", "Add a content-script feature", "Add an icon set"]),
    "data_cleaner": ("data", "green", "beginner", ["python"], "Rule-driven CSV cleaning with an audit of every change.",
                     ["Write rules for your columns", "Run it on your file", "Review the rejected rows"]),
    "file_sync": ("desktop", "teal", "intermediate", ["python"], "One-way folder sync that plans before it writes.",
                  ["Dry-run against a backup folder", "Add ignore globs", "Schedule it"]),
    "flask_app": ("web", "accent", "beginner", ["python", "html"], "A Flask app factory with a JSON API and a page (needs flask).",
                  ["pip install -r requirements.txt", "Add a route", "Add a form to the page"]),
    "home_automation": ("infra", "teal", "intermediate", ["python"], "An MQTT-style broker mock, devices and rules.",
                        ["Add a device", "Write a rule", "Swap in paho-mqtt"]),
    "log_analyzer": ("data", "green", "beginner", ["python"], "Access logs to a report: status, top paths, percentiles.",
                     ["Run it on your logs", "Add a field to the regex", "Export JSON to a dashboard"]),
    "ml_train": ("ai", "violet", "intermediate", ["python"], "Logistic regression from a CSV, honest metrics, JSON model.",
                 ["Swap in your CSV", "Add a feature", "Compare runs on the held-out set"]),
    "notebook_report": ("data", "green", "beginner", ["python", "html"], "'# %%' cells run into an HTML report and an .ipynb.",
                        ["Write your analysis cells", "Add a chart with bar()", "Share the HTML"]),
    "pdf_report": ("data", "amber", "intermediate", ["python"], "Real PDF files from the standard library, with charts.",
                   ["Point it at your CSV", "Add a section", "Add a page footer"]),
    "security_scanner": ("security", "red", "intermediate", ["python"], "Finds secrets and risky calls in your own code.",
                         ["Scan your repo", "Write a baseline", "Fail CI on new findings"]),
    "svg_dashboard": ("web", "blue", "beginner", ["python", "html", "svg"], "A self-contained HTML dashboard with SVG charts.",
                      ["Feed it your metrics JSON", "Add a chart type", "Publish the page"]),
    "terminal_game": ("game", "pink", "beginner", ["python"], "2048 in the terminal with a pure, seeded engine.",
                      ["Change the board size", "Add an undo move", "Write a tiny AI player"]),
    "vscode_extension": ("extension", "blue", "intermediate", ["javascript", "json"], "A no-build VS Code extension with commands and a setting.",
                         ["Press F5 to try it", "Add a command", "Package with vsce"]),
}

# A small original glyph per template, drawn in a 120x80 box (the frame is added by _svg). Strokes use
# currentColor; the coloured accent uses var(--tpl-accent, currentColor).
A = 'var(--tpl-accent, currentColor)'
GLYPHS = {
    "empty": f'<rect x="38" y="18" width="44" height="44" rx="6" fill="none" stroke="currentColor" stroke-dasharray="4 4"/><path d="M60 32v16M52 40h16" stroke="{A}" stroke-width="3"/>',
    "agent_tool": f'<circle cx="40" cy="40" r="14" fill="none" stroke="{A}" stroke-width="3"/><circle cx="40" cy="40" r="4" fill="{A}"/><path d="M58 40h12M80 28l10 12-10 12" fill="none" stroke="currentColor" stroke-width="3"/>',
    "mcp_server": f'<rect x="22" y="22" width="32" height="36" rx="4" fill="none" stroke="currentColor" stroke-width="3"/><rect x="66" y="22" width="32" height="36" rx="4" fill="none" stroke="{A}" stroke-width="3"/><path d="M54 34h12M66 46H54" stroke="currentColor" stroke-width="2"/>',
    "python_lib": f'<path d="M30 24h40v10H40v12h30v10H30z" fill="none" stroke="currentColor" stroke-width="3"/><path d="M78 30l12 10-12 10" fill="none" stroke="{A}" stroke-width="3"/>',
    "worker": f'<rect x="20" y="26" width="16" height="28" rx="3" fill="{A}"/><rect x="42" y="26" width="16" height="28" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><rect x="64" y="26" width="16" height="28" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M86 40h14M94 34l6 6-6 6" fill="none" stroke="currentColor" stroke-width="3"/>',
    "security_lab": f'<path d="M60 16l24 8v16c0 14-10 22-24 26-14-4-24-12-24-26V24z" fill="none" stroke="currentColor" stroke-width="3"/><path d="M60 22v40" stroke="{A}" stroke-width="3"/>',
    "red_team": f'<circle cx="60" cy="40" r="22" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="60" cy="40" r="12" fill="none" stroke="{A}" stroke-width="3"/><circle cx="60" cy="40" r="3" fill="{A}"/>',
    "range": f'<path d="M24 56h72" stroke="currentColor" stroke-width="3"/><rect x="50" y="22" width="20" height="34" fill="none" stroke="{A}" stroke-width="3"/><path d="M60 30v18M52 39h16" stroke="{A}" stroke-width="2"/>',
    "rest_api": f'<text x="60" y="48" text-anchor="middle" font-family="monospace" font-size="22" fill="currentColor">{{ }}</text><path d="M30 58h60" stroke="{A}" stroke-width="3"/>',
    "fullstack": f'<rect x="22" y="18" width="76" height="18" rx="3" fill="none" stroke="currentColor" stroke-width="3"/><rect x="22" y="44" width="76" height="18" rx="3" fill="none" stroke="{A}" stroke-width="3"/><path d="M60 36v8" stroke="currentColor" stroke-width="3"/>',
    "static_web": f'<rect x="22" y="18" width="76" height="46" rx="4" fill="none" stroke="currentColor" stroke-width="3"/><path d="M22 28h76" stroke="currentColor" stroke-width="2"/><rect x="30" y="36" width="28" height="20" rx="2" fill="{A}"/>',
    "data_sql": f'<ellipse cx="60" cy="24" rx="24" ry="8" fill="none" stroke="{A}" stroke-width="3"/><path d="M36 24v32c0 4 11 8 24 8s24-4 24-8V24M36 40c0 4 11 8 24 8s24-4 24-8" fill="none" stroke="currentColor" stroke-width="3"/>',
    "fastapi": f'<circle cx="60" cy="40" r="24" fill="none" stroke="currentColor" stroke-width="3"/><path d="M64 22L50 44h12l-6 16 16-24H60z" fill="{A}"/>',
    "network_service": f'<circle cx="30" cy="40" r="8" fill="{A}"/><circle cx="90" cy="40" r="8" fill="none" stroke="currentColor" stroke-width="3"/><path d="M38 36h44M82 44H38" stroke="currentColor" stroke-width="2" stroke-dasharray="6 4"/>',
    "iot_device": f'<rect x="44" y="30" width="32" height="30" rx="4" fill="none" stroke="currentColor" stroke-width="3"/><path d="M48 22a18 18 0 0 1 24 0M42 14a28 28 0 0 1 36 0" fill="none" stroke="{A}" stroke-width="3"/>',
    "cli_tool": f'<rect x="20" y="18" width="80" height="46" rx="5" fill="none" stroke="currentColor" stroke-width="3"/><path d="M30 32l10 8-10 8" fill="none" stroke="{A}" stroke-width="3"/><path d="M46 50h22" stroke="currentColor" stroke-width="3"/>',
    "tui_app": f'<rect x="20" y="18" width="80" height="46" rx="5" fill="none" stroke="currentColor" stroke-width="3"/><rect x="28" y="28" width="64" height="8" fill="{A}"/><path d="M28 44h50M28 54h40" stroke="currentColor" stroke-width="3"/>',
    "etl_pipeline": f'<path d="M18 30h24l8 10-8 10H18z" fill="none" stroke="currentColor" stroke-width="3"/><path d="M54 40h14" stroke="currentColor" stroke-width="3"/><ellipse cx="88" cy="30" rx="12" ry="5" fill="none" stroke="{A}" stroke-width="3"/><path d="M76 30v20c0 3 5 5 12 5s12-2 12-5V30" fill="none" stroke="{A}" stroke-width="3"/>',
    "state_machine": f'<circle cx="30" cy="40" r="10" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="90" cy="40" r="10" fill="none" stroke="{A}" stroke-width="3"/><path d="M40 34q20-14 40 0M80 46q-20 14-40 0" fill="none" stroke="currentColor" stroke-width="2"/>',
    "event_bus": f'<path d="M18 40h84" stroke="currentColor" stroke-width="4"/><circle cx="36" cy="40" r="5" fill="{A}"/><circle cx="60" cy="40" r="5" fill="{A}"/><circle cx="84" cy="40" r="5" fill="{A}"/><path d="M36 40V22M60 40v18M84 40V22" stroke="currentColor" stroke-width="2"/>',
    "cache_layer": f'<path d="M60 18l30 10-30 10-30-10z" fill="none" stroke="{A}" stroke-width="3"/><path d="M30 40l30 10 30-10M30 52l30 10 30-10" fill="none" stroke="currentColor" stroke-width="3"/>',
    "plugin_host": f'<path d="M30 30h18a6 6 0 1 1 12 0h18v18a6 6 0 1 0 0 12v4H30z" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="54" cy="46" r="5" fill="{A}"/>',
    "docs_site": f'<path d="M30 18h40l14 14v32H30z" fill="none" stroke="currentColor" stroke-width="3"/><path d="M38 38h36M38 46h36M38 54h24" stroke="{A}" stroke-width="3"/>',
    "tool_server": f'<path d="M40 56l20-20M62 22a10 10 0 1 0 14 14l-8-2-4-4z" fill="none" stroke="currentColor" stroke-width="3"/><rect x="28" y="52" width="12" height="12" rx="2" fill="{A}"/>',
    "agent_fleet": f'<circle cx="34" cy="34" r="8" fill="{A}"/><circle cx="60" cy="34" r="8" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="86" cy="34" r="8" fill="none" stroke="currentColor" stroke-width="3"/><path d="M26 58h68" stroke="currentColor" stroke-width="3"/>',
    "lease_queue": f'<rect x="20" y="28" width="56" height="24" rx="4" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="90" cy="40" r="12" fill="none" stroke="{A}" stroke-width="3"/><path d="M90 32v8l6 4" stroke="{A}" stroke-width="2" fill="none"/>',
    "api_gateway": f'<path d="M24 60V30l36-14 36 14v30" fill="none" stroke="currentColor" stroke-width="3"/><rect x="50" y="38" width="20" height="22" fill="{A}"/>',
    "event_store": f'<path d="M24 24h72M24 36h72M24 48h72" stroke="currentColor" stroke-width="3"/><path d="M24 60h40" stroke="{A}" stroke-width="3"/><circle cx="74" cy="60" r="4" fill="{A}"/>',
    "model_cli": f'<rect x="20" y="18" width="80" height="46" rx="5" fill="none" stroke="currentColor" stroke-width="3"/><path d="M30 34l8 6-8 6" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="78" cy="40" r="10" fill="none" stroke="{A}" stroke-width="3"/><circle cx="78" cy="40" r="3" fill="{A}"/>',
    "telemetry_collector": f'<path d="M20 52l18-14 14 8 18-22 14 12 16-10" fill="none" stroke="{A}" stroke-width="3"/><path d="M20 62h80" stroke="currentColor" stroke-width="2"/>',
    "fts_rag": f'<circle cx="52" cy="36" r="16" fill="none" stroke="currentColor" stroke-width="3"/><path d="M64 48l16 16" stroke="currentColor" stroke-width="4"/><path d="M44 32h16M44 40h10" stroke="{A}" stroke-width="3"/>',
    "auth_service": f'<rect x="38" y="36" width="44" height="28" rx="4" fill="none" stroke="currentColor" stroke-width="3"/><path d="M46 36v-8a14 14 0 0 1 28 0v8" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="60" cy="50" r="4" fill="{A}"/>',
    "chat_bot": f'<path d="M24 22h72v30H56l-14 12V52H24z" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="46" cy="37" r="4" fill="{A}"/><circle cx="60" cy="37" r="4" fill="{A}"/><circle cx="74" cy="37" r="4" fill="{A}"/>',
    "chrome_extension": f'<path d="M36 30h12a6 6 0 1 1 12 0h12v12a6 6 0 1 0 0 12v10H36V54a6 6 0 1 1 0-12z" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="54" cy="48" r="5" fill="{A}"/>',
    "data_cleaner": f'<path d="M22 24h40M22 34h30M22 44h40M22 54h24" stroke="currentColor" stroke-width="3"/><path d="M72 50l8 8 18-22" fill="none" stroke="{A}" stroke-width="4"/>',
    "file_sync": f'<rect x="18" y="26" width="30" height="30" rx="3" fill="none" stroke="currentColor" stroke-width="3"/><rect x="72" y="26" width="30" height="30" rx="3" fill="none" stroke="{A}" stroke-width="3"/><path d="M52 36h16M62 30l6 6-6 6" fill="none" stroke="currentColor" stroke-width="3"/>',
    "flask_app": f'<path d="M50 16h20M54 16v18L38 62h44L66 34V16" fill="none" stroke="currentColor" stroke-width="3"/><path d="M44 52h32l4 10H40z" fill="{A}"/>',
    "home_automation": f'<path d="M24 42l36-26 36 26M32 36v26h56V36" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="60" cy="48" r="6" fill="{A}"/>',
    "log_analyzer": f'<path d="M22 22h50M22 32h60M22 42h40M22 52h56" stroke="currentColor" stroke-width="3" opacity="0.6"/><rect x="20" y="38" width="64" height="8" fill="none" stroke="{A}" stroke-width="2"/><path d="M90 30v28" stroke="{A}" stroke-width="3"/>',
    "ml_train": f'<path d="M20 62h80M20 62V18" stroke="currentColor" stroke-width="2"/><path d="M24 58c20-40 30 0 70-36" fill="none" stroke="{A}" stroke-width="3"/><circle cx="40" cy="46" r="3" fill="currentColor"/><circle cx="70" cy="30" r="3" fill="currentColor"/>',
    "notebook_report": f'<rect x="26" y="16" width="68" height="18" rx="3" fill="none" stroke="currentColor" stroke-width="3"/><rect x="26" y="40" width="68" height="24" rx="3" fill="none" stroke="{A}" stroke-width="3"/><path d="M34 58v-6M44 58v-10M54 58v-4" stroke="{A}" stroke-width="3"/>',
    "pdf_report": f'<path d="M32 16h40l14 14v34H32z" fill="none" stroke="currentColor" stroke-width="3"/><rect x="40" y="44" width="8" height="12" fill="{A}"/><rect x="52" y="36" width="8" height="20" fill="{A}"/><rect x="64" y="40" width="8" height="16" fill="{A}"/>',
    "security_scanner": f'<path d="M50 16l20 7v14c0 12-8 19-20 23-12-4-20-11-20-23V23z" fill="none" stroke="currentColor" stroke-width="3"/><circle cx="78" cy="50" r="10" fill="none" stroke="{A}" stroke-width="3"/><path d="M85 57l9 9" stroke="{A}" stroke-width="4"/>',
    "svg_dashboard": f'<rect x="18" y="16" width="38" height="20" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><rect x="64" y="16" width="38" height="20" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M20 62l16-12 14 6 18-14 16 8 18-10" fill="none" stroke="{A}" stroke-width="3"/>',
    "terminal_game": f'<rect x="30" y="14" width="24" height="24" rx="3" fill="{A}"/><rect x="58" y="14" width="24" height="24" rx="3" fill="none" stroke="currentColor" stroke-width="3"/><rect x="30" y="42" width="24" height="24" rx="3" fill="none" stroke="currentColor" stroke-width="3"/><rect x="58" y="42" width="24" height="24" rx="3" fill="{A}" opacity="0.5"/>',
    "vscode_extension": f'<path d="M78 16L44 44 30 34l-6 4 14 12-14 12 6 4 14-10 34 28" fill="none" stroke="currentColor" stroke-width="3"/><path d="M78 16v58" stroke="{A}" stroke-width="5"/>',
}


def _svg(tid):
    title = T.TEMPLATES[tid]["title"].replace("&", "&amp;").replace("<", "&lt;")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80" width="120" height="80" role="img" '
            f'aria-label="{title}" fill="none" stroke-linecap="round" stroke-linejoin="round">'
            f'<title>{title}</title>'
            '<rect x="1" y="1" width="118" height="78" rx="10" fill="var(--tpl-bg, transparent)" stroke="currentColor" stroke-opacity="0.15"/>'
            f'{GLYPHS.get(tid, GLYPHS["empty"])}</svg>\n')


def write_svgs():
    os.makedirs(SVG_DIR, exist_ok=True)
    written = []
    for tid in T.TEMPLATES:
        path = os.path.join(SVG_DIR, f"{tid}.svg")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_svg(tid))
        written.append(path)
    return written


def svg(tid):
    path = os.path.join(SVG_DIR, f"{tid}.svg")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return _svg(tid)


def _tree(files):
    """Sorted, indented file tree of the rendered starter (placeholders shown as the package name 'app')."""
    paths = sorted(p.replace("__PKG__", "app") for p in files)
    lines, seen = [], set()
    for p in paths:
        parts = p.split("/")
        for depth in range(len(parts) - 1):
            d = "/".join(parts[:depth + 1])
            if d not in seen:
                seen.add(d)
                lines.append("  " * depth + parts[depth] + "/")
        lines.append("  " * (len(parts) - 1) + parts[-1])
    return lines


# ---------------------------------------------------------------------------------------------- contracts
# A contract is derived by code (AST over the rendered files), never written by hand, so it cannot drift from the code:
#   {"template", "package", "modules": [{"path", "module", "public": [{"name", "kind", "signature", "methods"}]}],
#    "cli": {"module", "commands", "mains"}, "http": [{"method", "path", "where"}], "tests": {"command", "files", "count"}, "run"}
_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD")


def _sig(fn):
    r = f" -> {ast.unparse(fn.returns)}" if getattr(fn, "returns", None) is not None else ""
    return f"({ast.unparse(fn.args)}){r}"


def _public(tree):
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            out.append({"name": node.name, "kind": "function", "signature": _sig(node), "methods": []})
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            ms = [{"name": m.name, "signature": _sig(m)} for m in node.body
                  if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and (not m.name.startswith("_") or m.name == "__init__")]
            out.append({"name": node.name, "kind": "class", "signature": next((m["signature"] for m in ms if m["name"] == "__init__"), "()"),
                        "methods": [m for m in ms if m["name"] != "__init__"]})
    return out


def _is_path_expr(n):
    return (isinstance(n, ast.Name) and n.id in ("path", "route", "p")) or (isinstance(n, ast.Attribute) and n.attr == "path")


def _str_const(n):
    return isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("/")


def _http(tree, rel):
    found = []
    has_do = any(isinstance(c, ast.ClassDef) and any("RequestHandler" in ast.unparse(x) for x in c.bases) for c in ast.walk(tree))
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        where = f"{rel}:{fn.name}"
        verb = fn.name[3:].upper() if fn.name.startswith("do_") and fn.name[3:].upper() in _VERBS else None
        for dec in fn.decorator_list:                # @app.get("/x"), @app.route("/x", methods=[...]), @router.post("/x")
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.args and _str_const(dec.args[0]):
                meth = dec.func.attr.upper()
                if meth == "ROUTE":
                    ms = [k.value for k in dec.keywords if k.arg == "methods"]
                    vals = [e.value.upper() for e in ms[0].elts if isinstance(e, ast.Constant)] if ms and isinstance(ms[0], (ast.List, ast.Tuple)) else ["GET"]
                    found += [{"method": m, "path": dec.args[0].value, "where": where} for m in vals]
                elif meth in _VERBS:
                    found.append({"method": meth, "path": dec.args[0].value, "where": where})
        if not verb and has_do and not fn.name.startswith("do_"):
            verb = "ANY"                              # a routing helper the do_* handlers call
        if verb:                                      # http.server handlers compare the path with string constants
            for n in ast.walk(fn):
                if isinstance(n, ast.Compare) and _is_path_expr(n.left):
                    for c in n.comparators:
                        vals = list(c.elts) if isinstance(c, (ast.Tuple, ast.List, ast.Set)) else [c]
                        found += [{"method": verb, "path": v.value, "where": where} for v in vals if _str_const(v)]
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("fullmatch", "match", "search") \
                        and n.args and _str_const(n.args[0]):
                    found.append({"method": verb, "path": re.sub(r"\(.*?\)", "{}", n.args[0].value.replace("\\", "")), "where": where})
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "startswith" \
                        and _is_path_expr(n.func.value) and n.args and _str_const(n.args[0]):
                    found.append({"method": verb, "path": n.args[0].value + "*", "where": where})
    return found


def _cli(tree):
    return {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_parser" and n.args
            and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)}


def derive_contract(files, tid="", pkg="app", run="", test_cmd="python -m pytest -q"):
    """-> the contract of a rendered project. files: {relative path: text}. Pure: reads nothing from disk."""
    modules, cmds, cli_mod, http, tfiles, tcount = [], set(), "", [], [], 0
    for rel in sorted(files):
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(files[rel])
        except SyntaxError:
            continue
        base = os.path.basename(rel)
        if base.startswith("test_") or rel.startswith("tests/"):
            if base.startswith("test_"):
                tfiles.append(rel)
                tcount += sum(1 for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"))
            continue
        if base == "conftest.py":
            continue
        mod = rel[:-3].replace("/", ".")
        mod = mod[:-9] if mod.endswith(".__init__") else mod
        pub = _public(tree)
        if pub or base == "__main__.py":
            modules.append({"path": rel, "module": mod, "public": pub})
        c = _cli(tree)
        if c:
            cmds |= c
            cli_mod = cli_mod or mod
        http += _http(tree, rel)
    seen, hs = set(), []
    for h in sorted(http, key=lambda h: (h["path"], h["method"], h["where"])):
        if (h["method"], h["path"]) not in seen:
            seen.add((h["method"], h["path"]))
            hs.append(h)
    mains = sorted(m["module"].rsplit(".", 1)[0] for m in modules if m["path"].endswith("__main__.py") and "." in m["module"])
    return {"template": tid, "package": pkg, "modules": modules,
            "cli": {"module": cli_mod, "commands": sorted(cmds), "mains": mains},
            "http": hs, "tests": {"command": test_cmd, "files": tfiles, "count": tcount}, "run": run}


@functools.lru_cache(maxsize=None)
def _contract_cached(tid, name, addons):
    return json.dumps(derive_contract(T.render(tid, name, addons), tid, T.pkg_name(name), T.rendered_run(tid, name)), sort_keys=True)


def contract(tid, name="app", addons=()):
    """The machine-readable contract of template `tid` rendered as `name` (derived from the rendered code and its tests)."""
    return json.loads(_contract_cached(tid, name, tuple(addons or ())))


def read_contract(project):
    """-> the CONTRACT.json inside a rendered project, or None when there is none or it does not parse."""
    try:
        with open(os.path.join(project, "CONTRACT.json"), encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) and "modules" in c else None
    except (OSError, ValueError):
        return None


def as_interface_contract(c):
    """A project contract in the shape arch_flows.interface_contract(goal) returns, so a feature run can check new work
    against the known interface: {"imports": [(module, name)], "calls": [(name, n_positional, [keywords], text)], "mains": [module], "methods": []}."""
    imports, calls = [], []
    for m in (c or {}).get("modules", []):
        for p in m["public"]:
            imports.append((m["module"], p["name"]))
            if p["kind"] == "function":
                try:
                    a = ast.parse("def f" + p["signature"].split(") -> ")[0].rstrip(")") + "): pass").body[0].args
                    calls.append((p["name"], len(a.posonlyargs) + len(a.args) - len(a.defaults), [], p["name"] + "(...)"))
                except SyntaxError:
                    pass
    return {"imports": imports, "calls": calls, "mains": list(((c or {}).get("cli") or {}).get("mains", [])), "methods": []}


def entry(tid):
    t = T.TEMPLATES[tid]
    cat, colour, diff, langs, summary, steps = META.get(tid, ("cli", "accent", "intermediate", ["python"], t["blurb"].split(". ")[0], []))
    tags = [s.strip() for s in t["stack"].split("/") if s.strip()]
    tests = sum(1 for p in t["files"] if p.startswith("tests/test_") and p.endswith(".py"))
    return {
        "id": tid, "name": t["title"], "summary": summary, "description": t["blurb"],
        "category": cat, "languages": langs, "tags": tags, "difficulty": diff, "colour": colour,
        "needs": list(t["needs"]), "offline": not t["needs"], "run": t["run"], "test": "python -m pytest -q",
        "files": len(t["files"]), "test_files": tests, "tree": _tree(t["files"]), "next_steps": list(steps),
        "image": f"static/templates/{tid}.svg",
        "contract": contract(tid),
    }


def catalog():
    """-> list of JSON-able dicts, grouped by category order, then name."""
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted((entry(t) for t in T.TEMPLATES), key=lambda e: (order.get(e["category"], 99), e["name"].lower()))


def render(tid, dest, vars=None, addons=(), overwrite=False):
    """Write the starter's files (exactly arch_templates.render(tid, name, addons)) under dest.
    vars: {"name": display name}. Refuses to overwrite an existing file unless overwrite=True. -> list of paths written."""
    name = (vars or {}).get("name") or os.path.basename(os.path.abspath(dest)) or "app"
    files = T.render(tid, name, addons)
    if "CONTRACT.json" not in files:
        files["CONTRACT.json"] = json.dumps(contract(tid, name, addons), indent=2) + "\n"
    targets = {rel: os.path.join(dest, *rel.split("/")) for rel in files}
    clash = [p for p in targets.values() if os.path.exists(p)]
    if clash and not overwrite:
        raise FileExistsError(f"{len(clash)} file(s) already exist, e.g. {clash[0]}")
    for rel, path in targets.items():
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(files[rel])
    return sorted(targets.values())


if __name__ == "__main__":
    import json
    import sys
    if sys.argv[1:] == ["--write-svgs"]:
        print(f"wrote {len(write_svgs())} thumbnails to {SVG_DIR}")
    else:
        print(json.dumps(catalog(), indent=2))
