// ============================================================
// CLIP Assistant — Background Service Worker
// Handles cookie access and proxies /chat requests.
// Fetch runs here (not in content script) to avoid mixed
// content blocks when CLIP is served over HTTPS.
// ============================================================

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'GET_COOKIES') {
    chrome.cookies.getAll({ url: 'https://clip.fct.unl.pt' })
      .then(cookies => sendResponse({ cookie: cookies.map(c => `${c.name}=${c.value}`).join('; ') }))
      .catch(err => sendResponse({ cookie: '', error: err.message }));
    return true;
  }

  if (msg.type === 'FETCH_CHAT') {
    fetch(msg.url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(msg.body),
    })
      .then(async res => {
        const data = await res.json().catch(() => null);
        sendResponse({ ok: res.ok, status: res.status, data });
      })
      .catch(err => sendResponse({ ok: false, error: err.message }));
    return true;
  }
});
