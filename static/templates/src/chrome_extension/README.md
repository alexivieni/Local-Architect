# __NAME__

A Chrome (Manifest V3) extension that keeps a short note per site.

## Load it

1. Open `chrome://extensions`, switch on **Developer mode**.
2. **Load unpacked** and pick the `extension/` folder.
3. Click the toolbar icon: type a note, it is saved for the current site.

## Layout

    extension/manifest.json   MV3 manifest: permissions are only "storage" and "activeTab"
    extension/popup.html      the popup (no inline script: MV3's CSP forbids it)
    extension/popup.js        reads/writes chrome.storage.local, keyed by hostname
    extension/notes.js        pure helpers shared by popup and tests (hostname key, trimming)
    extension/background.js   service worker: badge text when a site has a note
    extension/content.js      content script (kept tiny; add page logic here)
    tests/                    Python checks: manifest shape, files exist, no remote code, CSP-safe popup

    python -m pytest -q
