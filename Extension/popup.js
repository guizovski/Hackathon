document.addEventListener('DOMContentLoaded', async () => {
  const serverUrlInput = document.getElementById('serverUrl');
  const saveBtn        = document.getElementById('saveBtn');
  const messageEl      = document.getElementById('message');
  const statusBadge    = document.getElementById('statusBadge');
  const statusText     = document.getElementById('statusText');

  // Load existing config
  const result = await chrome.storage.local.get(['serverBaseUrl']);
  if (result.serverBaseUrl) serverUrlInput.value = result.serverBaseUrl;
  setStatus(result.serverBaseUrl);

  function setStatus(serverUrl) {
    if (serverUrl) {
      statusBadge.className = 'status-badge active';
      statusText.textContent = 'Servidor Tejo ativo';
    } else {
      statusBadge.className = 'status-badge inactive';
      statusText.textContent = 'Servidor não configurado';
    }
  }

  function showMessage(text, type) {
    messageEl.textContent = text;
    messageEl.className = `message ${type}`;
    messageEl.style.display = 'block';
    setTimeout(() => { messageEl.style.display = 'none'; }, 3000);
  }

  saveBtn.addEventListener('click', () => {
    const serverUrl = serverUrlInput.value.trim();

    if (!serverUrl) {
      showMessage('⚠️ Introduz o URL do servidor Tejo', 'error');
      return;
    }
    if (!/^https?:\/\/.+/.test(serverUrl)) {
      showMessage('⚠️ O URL deve começar com http:// ou https://', 'error');
      return;
    }

    chrome.storage.local.set({
      serverBaseUrl: serverUrl.replace(/\/$/, ''),
    });
    setStatus(serverUrl);
    showMessage('✅ Configuração guardada com sucesso!', 'success');
  });
});
