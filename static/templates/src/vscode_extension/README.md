# __NAME__

A VS Code extension skeleton in plain JavaScript (no TypeScript, no bundler).

- `__PKG__.countWords` shows the word count of the selection (or the file).
- `__PKG__.toggleStatus` hides/shows a live status-bar counter.
- Setting `__PKG__.excludeCode`: skip fenced code blocks in Markdown.

## Run it

Open this folder in VS Code and press **F5** (Run Extension). Package with `npx @vscode/vsce package`.

## Layout

    package.json        manifest: commands, configuration, activation events
    src/extension.js    activate/deactivate, wires VS Code to the core
    src/core.js         pure logic (no `vscode` import) -- easy to test
    .vscode/launch.json F5 config
    tests/              Python consistency checks; add JS unit tests with `node --test` later

    python -m pytest -q
