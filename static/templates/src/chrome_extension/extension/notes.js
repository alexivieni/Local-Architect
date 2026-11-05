// Pure helpers (no chrome.* here) so they are easy to reason about and reuse.
const MAX_NOTE = 2000;

function siteKey(url) {
  try {
    const u = new URL(url);
    return u.protocol.startsWith("http") ? "note:" + u.hostname : null;
  } catch (e) {
    return null;
  }
}

function cleanNote(text) {
  return String(text || "").replace(/\s+$/, "").slice(0, MAX_NOTE);
}

if (typeof module !== "undefined") module.exports = { siteKey, cleanNote, MAX_NOTE };
