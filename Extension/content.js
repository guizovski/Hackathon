// ============================================================
// CLIP Assistant - Content Script
// ============================================================

(function () {
  if (document.getElementById('clip-assistant-root')) return;

  // ============================================================
  // HELPERS
  // ============================================================

  function getStudentNumber() {
    const url = new URL(window.location.href);
    let aluno = url.searchParams.get('aluno');
    if (!aluno) {
      const m = document.body.innerHTML.match(/[?&]aluno=(\d+)/);
      if (m) aluno = m[1];
    }
    return aluno;
  }

  function getStudentName() {
    const FULL_NAME_RE = /^[A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇ][a-záéíóúàâêôãõüç]+(?:\s+[A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇ][a-záéíóúàâêôãõüç]+){2,}$/;
    const firstLast = name => {
      const p = name.trim().split(/\s+/);
      return p.length > 1 ? p[0] + ' ' + p[p.length - 1] : p[0];
    };

    // 1. Título da página: "CLIP - 69112 (LEI) - Nome Completo"
    const m = document.title.match(/CLIP\s*[-–]\s*[^-–]+-\s*(.+)/);
    if (m && FULL_NAME_RE.test(m[1].trim())) return firstLast(m[1]);

    // 2. Link com nome completo (canto superior direito do CLIP)
    for (const a of document.querySelectorAll('a')) {
      const t = a.textContent.trim();
      if (FULL_NAME_RE.test(t)) return firstLast(t);
    }

    // 3. h2 da página: "69112 (LEI) - Nome Completo"
    const h2 = document.querySelector('h2');
    if (h2) {
      const hm = h2.textContent.match(/[-–]\s*([A-ZÁÉÍÓÚÀÂÊÔÃÕÜÇ].+)/);
      if (hm && FULL_NAME_RE.test(hm[1].trim())) return firstLast(hm[1]);
    }

    return null;
  }

  function detectPageType(url) {
    if (/hor[aá]rio|horario/.test(url)) return '📅 Horário';
    if (/inscri/.test(url)) return '📝 Inscrição';
    if (/nota|grade/.test(url)) return '📊 Notas';
    if (/document/.test(url)) return '📄 Documentos';
    return '🌐 CLIP';
  }

  // ============================================================
  // BUILD UI
  // ============================================================

  const aluno = getStudentNumber();
  const studentName = getStudentName();
  const pageType = detectPageType(window.location.href);

  const container = document.createElement('div');
  container.id = 'clip-assistant-root';
  container.innerHTML = `
    <div id="clip-chatbot-window">
      <div id="clip-chatbot-header">
        <div class="clip-chatbot-header-info">
          <div class="clip-chatbot-avatar">🎓</div>
          <div class="clip-chatbot-title">
            <strong>Tejo</strong>
            <span>FCT-UNL · Assistente académico</span>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <div class="clip-chatbot-online">
            <div class="clip-chatbot-online-dot"></div>
            online
          </div>
          <button id="clip-chatbot-close">✕</button>
        </div>
      </div>

      <div id="clip-page-context">
        <span>📍</span>
        <span id="clip-page-label">${pageType}${studentName ? ' · ' + studentName : ''}</span>
      </div>

      <div id="clip-chatbot-messages"></div>

      <div id="clip-chatbot-suggestions">
        <button class="clip-suggestion-btn" data-q="Qual é o meu horário?">📅 O meu horário</button>
        <button class="clip-suggestion-btn" data-q="Quero ver os testes de avaliação">📋 Testes</button>
        <button class="clip-suggestion-btn" data-q="Tenho testes para me inscrever?">🔍 Verificar testes</button>
        <button class="clip-suggestion-btn" data-q="Inscrição letiva e UCs">📚 Inscrição letiva</button>
        <button class="clip-suggestion-btn" data-q="Tenho propinas em atraso?">⚠️ Propinas em atraso</button>
        <button class="clip-suggestion-btn" data-q="Que propinas tenho por pagar?">💶 Propinas por pagar</button>
        <button class="clip-suggestion-btn" data-q="Quais são as minhas notas?">📊 Notas</button>
        <button class="clip-suggestion-btn" data-q="Exportar horário .ics">📥 Exportar horário</button>
      </div>

      <div id="clip-chatbot-input-area">
        <textarea id="clip-chatbot-input" placeholder="Pergunta algo sobre o CLIP…" rows="1"></textarea>
        <button id="clip-chatbot-send">➤</button>
      </div>
    </div>

    <button id="clip-chatbot-toggle">🎓<div class="badge"></div></button>
  `;
  document.body.appendChild(container);

  // ============================================================
  // STATE
  // ============================================================

  const messagesEl = document.getElementById('clip-chatbot-messages');
  const inputEl    = document.getElementById('clip-chatbot-input');
  const sendBtn    = document.getElementById('clip-chatbot-send');
  const window_    = document.getElementById('clip-chatbot-window');
  const toggleBtn  = document.getElementById('clip-chatbot-toggle');
  const closeBtn   = document.getElementById('clip-chatbot-close');

  let isOpen   = false;
  let isLoading = false;
  let conversationHistory = [];

  // ============================================================
  // TOGGLE
  // ============================================================

  function toggleChat() {
    isOpen = !isOpen;
    window_.classList.toggle('open', isOpen);
    if (isOpen) {
      toggleBtn.textContent = '✕';
      toggleBtn.style.fontSize = '18px';
      if (messagesEl.children.length === 0) addWelcomeMessage();
      setTimeout(() => inputEl.focus(), 300);
    } else {
      toggleBtn.innerHTML = '🎓<div class="badge"></div>';
      toggleBtn.style.fontSize = '24px';
    }
  }
  toggleBtn.addEventListener('click', toggleChat);
  closeBtn.addEventListener('click', toggleChat);

  // ============================================================
  // MESSAGES
  // ============================================================

  function addWelcomeMessage() {
    const nome = getStudentName();
    const div = document.createElement('div');
    div.className = 'clip-message assistant';

    const bubble = document.createElement('div');
    bubble.className = 'clip-message-bubble';

    const greeting = nome
      ? `<span style="font-size:17px;font-weight:700;color:#00c896;display:block;margin-bottom:6px">Olá, ${nome}! 👋</span>`
      : `<span style="font-size:17px;font-weight:700;color:#00c896;display:block;margin-bottom:6px">Olá! 👋</span>`;

    const body = aluno
      ? `Sou o teu assistente para o <strong>CLIP da FCT-UNL</strong>.<br><br>Posso ajudar-te com horários, testes, propinas, notas e muito mais. O que precisas? 😊`
      : `Sou o <strong>CLIP Assistant da FCT-UNL</strong>.<br><br>Navega até à tua página de aluno para eu poder aceder ao teu contexto. O que precisas? 😊`;

    bubble.innerHTML = greeting + body;

    const time = document.createElement('div');
    time.className = 'clip-message-time';
    time.textContent = new Date().toLocaleTimeString('pt-PT', { hour: '2-digit', minute: '2-digit' });

    div.appendChild(bubble);
    div.appendChild(time);
    messagesEl.appendChild(div);
  }

  function routeLabel(url) {
    try {
      const s = String(url);
      if (s.includes('pagamentos_acad'))  return { icon: '💶', label: 'Propinas' };
      if (s.includes('resumo'))           return { icon: '📊', label: 'Notas' };
      if (s.includes('escolar'))          return { icon: '📚', label: 'Inscrição Letiva' };
      if (s.includes('testes_de_avalia')) return { icon: '📋', label: 'Testes' };
      if (s.includes('hor'))              return { icon: '📅', label: 'Horário' };
      if (s.includes('document'))         return { icon: '📄', label: 'Documentos' };
      if (s.includes('inscri'))           return { icon: '📝', label: 'Inscrição' };
      return { icon: '🔗', label: 'CLIP' };
    } catch (_) {
      return { icon: '🔗', label: 'CLIP' };
    }
  }

  function addMessage(role, text, isError = false, routes = []) {
    const div = document.createElement('div');
    div.className = `clip-message ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'clip-message-bubble' + (isError ? ' clip-error-bubble' : '');
    bubble.innerHTML = formatMessage(text);

    // Route buttons below the answer
    if (routes && routes.length > 0) {
      try {
        const routesWrap = document.createElement('div');
        routesWrap.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.07)';
        routes.forEach(url => {
          if (!url || typeof url !== 'string') return;
          const { icon, label } = routeLabel(url);
          const a = document.createElement('a');
          a.href = 'https://clip.fct.unl.pt' + (url.startsWith('http') ? url.replace('https://clip.fct.unl.pt', '') : url);
          if (url.startsWith('http')) a.href = url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          a.textContent = icon + ' ' + label;
          a.style.cssText = 'display:inline-flex;align-items:center;gap:5px;padding:5px 11px;background:rgba(0,200,150,0.12);color:#00c896;border:1px solid rgba(0,200,150,0.3);border-radius:6px;text-decoration:none;font-size:11.5px;font-weight:600';
          routesWrap.appendChild(a);
        });
        if (routesWrap.children.length > 0) bubble.appendChild(routesWrap);
      } catch (_) { /* never block the message from showing */ }
    }

    const time = document.createElement('div');
    time.className = 'clip-message-time';
    time.textContent = new Date().toLocaleTimeString('pt-PT', { hour: '2-digit', minute: '2-digit' });

    div.appendChild(bubble);
    div.appendChild(time);
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
  }

  function formatMessage(text) {
    const btn = (href, icon, label) =>
      `<br><a href="${href}" target="_blank" style="display:inline-flex;align-items:center;gap:7px;margin-top:10px;padding:8px 15px;background:#00c896;color:#0e0f11;border-radius:8px;text-decoration:none;font-size:12px;font-weight:600;box-shadow:0 4px 14px rgba(0,200,150,0.28);font-family:'IBM Plex Sans',sans-serif;letter-spacing:0.2px">${icon} ${label}</a><br>`;
    return text
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/`(.*?)`/g, '<code style="background:rgba(0,200,150,0.1);color:#00c896;padding:2px 6px;border-radius:4px;font-family:monospace;font-size:11.5px;font-weight:500">$1</code>')
      .replace(/(https:\/\/clip\.fct\.unl\.pt[^\s<"]*pagamentos_acad[^\s<"]*)/g,
        (_, u) => btn(u, '💶', 'Propinas'))
      .replace(/(https:\/\/clip\.fct\.unl\.pt[^\s<"]*situa[^\s<"]*resumo[^\s<"]*)/g,
        (_, u) => btn(u, '📊', 'Notas & Situação'))
      .replace(/(https:\/\/clip\.fct\.unl\.pt[^\s<"]*inscri[^\s<"]*escolar[^\s<"]*)/g,
        (_, u) => btn(u, '📚', 'Inscrição Letiva'))
      .replace(/(https:\/\/clip\.fct\.unl\.pt[^\s<"]*testes_de_avalia[^\s<"]*)/g,
        (_, u) => btn(u, '📋', 'Testes de Avaliação'))
      .replace(/(https:\/\/clip\.fct\.unl\.pt[^\s<"]*hor[^\s<"]*)/g,
        (_, u) => btn(u, '📅', 'Ver Horário'))
      .replace(/\n(\d+\.\s)/g, '<br><span style="color:#00c896;font-weight:600">$1</span>')
      .replace(/\n/g, '<br>');
  }

  function showTyping() {
    hideTyping();
    const div = document.createElement('div');
    div.className = 'clip-message assistant';
    div.id = 'clip-typing';
    div.innerHTML = `<div class="clip-typing-indicator"><div class="clip-typing-dot"></div><div class="clip-typing-dot"></div><div class="clip-typing-dot"></div></div>`;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function hideTyping() {
    document.getElementById('clip-typing')?.remove();
  }

  // ============================================================
  // ICS DOWNLOAD HELPER
  // ============================================================

  function triggerICSDownload(base64Data) {
    const bytes = atob(base64Data);
    const arr = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
    const blob = new Blob([arr], { type: 'text/calendar; charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'horario_clip.ics';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // ============================================================
  // SEND MESSAGE -> LLM SERVER
  // ============================================================

  async function sendMessage(text) {
    text = text.trim();
    if (!text || isLoading) return;

    // Hide suggestions after first message
    const suggestionsEl = document.getElementById('clip-chatbot-suggestions');
    if (suggestionsEl) suggestionsEl.style.display = 'none';

    addMessage('user', text);
    inputEl.value = '';
    inputEl.style.height = 'auto';
    conversationHistory.push({ role: 'user', content: text });

    let serverBaseUrl;
    try {
      const storage = await chrome.storage.local.get(['serverBaseUrl']);
      serverBaseUrl = storage.serverBaseUrl;
    } catch {
      addMessage('assistant', '⚠️ Contexto da extensão inválido. Recarrega a página do CLIP.', true);
      return;
    }

    if (!serverBaseUrl) {
      addMessage('assistant', '⚠️ Configura o **URL do Servidor Tejo** clicando no ícone 🎓 na barra do Chrome.', true);
      conversationHistory.pop();
      return;
    }

    isLoading = true;
    sendBtn.disabled = true;
    showTyping();

    // Step 1: get cookies from background (fast)
    const cookieResult = await chrome.runtime.sendMessage({ type: 'GET_COOKIES' });
    const sessionCookie = cookieResult?.cookie || '';

    // Step 2: proxy the LLM fetch through the background worker to avoid
    // mixed-content blocks (CLIP is HTTPS; localhost backend is HTTP).
    try {
      const result = await chrome.runtime.sendMessage({
        type: 'FETCH_CHAT',
        url: `${serverBaseUrl}/chat`,
        body: {
          question: text,
          session_cookie: sessionCookie,
          student_id: aluno || null,
          conversation_history: conversationHistory.slice(-10),
        },
      });

      hideTyping();
      isLoading = false;
      sendBtn.disabled = false;

      if (!result.ok) {
        const detail = result.data?.detail;
        const msg = result.error
          ? `❌ Não foi possível contactar o servidor: ${result.error}`
          : `❌ Erro do servidor: HTTP ${result.status}${detail ? ' — ' + detail : ''}`;
        addMessage('assistant', msg, true);
        conversationHistory.pop();
      } else {
        const data = result.data;
        if (data.ics_data) triggerICSDownload(data.ics_data);
        const reply = data.answer || 'Não consegui gerar uma resposta.';
        const routes = Array.isArray(data.routes_consulted)
          ? data.routes_consulted.filter(r => r && typeof r === 'string')
          : [];
        conversationHistory.push({ role: 'assistant', content: reply });
        addMessage('assistant', reply, false, routes);
      }
    } catch (e) {
      hideTyping();
      isLoading = false;
      sendBtn.disabled = false;
      addMessage('assistant', `❌ Não foi possível contactar o servidor: ${e.message}`, true);
      conversationHistory.pop();
    }

    inputEl.focus();
  }

  // ============================================================
  // INPUT HANDLERS
  // ============================================================

  sendBtn.addEventListener('click', () => sendMessage(inputEl.value));
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(inputEl.value); }
  });
  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 100) + 'px';
  });
  document.querySelectorAll('.clip-suggestion-btn').forEach(btn => {
    btn.addEventListener('click', () => sendMessage(btn.dataset.q));
  });

})();
