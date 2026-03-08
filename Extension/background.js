// ============================================================
// CLIP Assistant — Background Service Worker
// Only handles HttpOnly cookie access (fast, <1s).
// The slow LLM fetch runs directly in the content script.
// ============================================================

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'GET_COOKIES') {
    chrome.cookies.getAll({ url: 'https://clip.fct.unl.pt' })
      .then(cookies => sendResponse({ cookie: cookies.map(c => `${c.name}=${c.value}`).join('; ') }))
      .catch(err => sendResponse({ cookie: '', error: err.message }));
    return true;
  }
});
