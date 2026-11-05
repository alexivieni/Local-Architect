// Popup: load the note for the active tab's site, save on every pause in typing.
(async function () {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const key = tab && siteKey(tab.url || "");
  const box = document.getElementById("note");
  const status = document.getElementById("status");
  if (!key) {
    box.disabled = true;
    status.textContent = "Notes work on http(s) pages only.";
    return;
  }
  document.getElementById("site").textContent = key.slice(5);
  const stored = await chrome.storage.local.get(key);
  box.value = stored[key] || "";
  let timer = null;
  box.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const text = cleanNote(box.value);
      if (text) await chrome.storage.local.set({ [key]: text });
      else await chrome.storage.local.remove(key);
      status.textContent = "Saved";
    }, 400);
  });
})();
