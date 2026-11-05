// Service worker: show a dot on the toolbar icon when the current site has a note.
async function refreshBadge(tabId, url) {
  let key = null;
  try {
    const u = new URL(url || "");
    if (u.protocol.startsWith("http")) key = "note:" + u.hostname;
  } catch (e) { /* not a web page */ }
  const has = key ? Boolean((await chrome.storage.local.get(key))[key]) : false;
  await chrome.action.setBadgeText({ tabId, text: has ? "*" : "" });
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete") refreshBadge(tabId, tab.url);
});
chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await chrome.tabs.get(tabId);
  refreshBadge(tabId, tab.url);
});
