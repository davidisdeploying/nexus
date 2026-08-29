(() => {
  'use strict';

  const devices = [...document.querySelectorAll('.device')];
  const providerTabs = [...document.querySelectorAll('.provider-tab')];
  const tabs = [...document.querySelectorAll('.role-tab')];
  const strategyPanel = document.getElementById('strategyPanel');
  const workerPanel = document.getElementById('workerPanel');
  const terminalState = document.getElementById('terminalState');
  const terminalHost = document.getElementById('terminalHost');
  const strategySeat = document.getElementById('strategySeat');
  const strategyProvider = document.getElementById('strategyProvider');
  const strategyTitle = document.getElementById('strategyTitle');
  const strategyExplainer = document.getElementById('strategyExplainer');
  const workerSeat = document.getElementById('workerSeat');
  const workerProvider = document.getElementById('workerProvider');
  const workerTitle = document.getElementById('workerTitle');
  const workerExplainer = document.getElementById('workerExplainer');
  const workerRuns = document.getElementById('workerRuns');
  const workerRunsTitle = document.getElementById('workerRunsTitle');
  const workerMessage = document.getElementById('workerMessage');
  const providerDetail = document.getElementById('providerDetail');
  const terminalElement = document.getElementById('terminal');
  const strategyConsole = document.querySelector('.strategy-console');
  const sessionLabel = document.getElementById('sessionLabel');
  const strategyModel = document.getElementById('strategyModel');
  const autoMode = document.getElementById('autoMode');
  const viewTabs = [...document.querySelectorAll('.view-tab')];
  const chatView = document.getElementById('chatView');
  const terminalView = document.getElementById('terminalView');
  const chatThread = document.getElementById('chatThread');
  const chatProviderLabel = document.getElementById('chatProviderLabel');
  const chatComposer = document.getElementById('chatComposer');
  const chatInput = document.getElementById('chatInput');
  const sendPrompt = document.getElementById('sendPrompt');
  const slashButton = document.getElementById('slashButton');
  const slashPalette = document.getElementById('slashPalette');

  let selectedHost = devices[0]?.dataset.host || 'alpha';
  let selectedLabel = devices[0]?.querySelector('.device-name')?.textContent || 'Alpha';
  let selectedSeat = devices[0]?.dataset.seat || 'Worker2';
  let selectedProvider = providerTabs[0]?.dataset.provider || 'claude';
  let selectedProviderLabel = providerTabs[0]?.dataset.label || 'Claude';
  let selectedMode = 'strategy';
  let socket = null;
  let reconnectTimer = null;
  let reconnectDelay = 1000;
  let deliberateClose = false;
  let wantConnection = false;
  let selectedView = 'chat';
  let activeAssistantMessage = null;
  let chatBaseline = '';
  let lastSubmittedPrompt = '';
  let chatMirrorTimer = null;
  let permissionMode = 'unknown';

  const SLASH_COMMANDS = {
    claude: ['/model ', '/permissions', '/status', '/usage', '/resume', '/agents', '/help'],
    codex: ['/model', '/status', '/review', '/resume', '/help'],
    gemini: ['/model', '/status', '/usage', '/agents', '/help'],
  };

  const terminal = new Terminal({
    cursorBlink: true,
    cursorStyle: 'bar',
    fontFamily: '"SFMono-Regular", Menlo, Monaco, Consolas, monospace',
    fontSize: window.innerWidth < 700 ? 12 : 13,
    lineHeight: 1.18,
    scrollback: 6000,
    allowProposedApi: false,
    theme: {
      background: '#030808',
      foreground: '#dceeea',
      cursor: '#54e6da',
      selectionBackground: '#1e7772',
      black: '#081211',
      brightBlack: '#56716e',
      red: '#ff766f',
      brightRed: '#ff9d98',
      green: '#43df87',
      brightGreen: '#71f2a5',
      yellow: '#f1bd65',
      brightYellow: '#ffd58b',
      blue: '#55a8ff',
      brightBlue: '#83c1ff',
      magenta: '#d397ff',
      brightMagenta: '#e0b4ff',
      cyan: '#54e6da',
      brightCyan: '#83fff4',
      white: '#dceeea',
      brightWhite: '#ffffff',
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  terminal.loadAddon(fitAddon);
  const webLinksAddon = new WebLinksAddon.WebLinksAddon((_event, uri) => {
    window.open(uri, '_blank', 'noopener,noreferrer');
  });
  terminal.loadAddon(webLinksAddon);
  terminal.open(terminalElement);

  function bytesToBase64(bytes) {
    let binary = '';
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
    }
    return btoa(binary);
  }

  function base64ToBytes(value) {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function socketIsOpen() {
    return socket?.readyState === WebSocket.OPEN;
  }

  function sendRaw(data) {
    if (!socketIsOpen()) return false;
    socket.send(JSON.stringify({
      type: 'input',
      data: bytesToBase64(new TextEncoder().encode(data)),
    }));
    return true;
  }

  function setComposerConnected(connected) {
    strategyConsole.dataset.connected = String(connected);
    sendPrompt.disabled = !connected;
    chatInput.placeholder = connected
      ? `Message ${selectedProviderLabel}…`
      : 'Connect first…';
    strategyModel.disabled = !connected || strategyModel.options.length < 2;
    autoMode.disabled = !connected;
  }

  function appendChatMessage(kind, text, meta = '') {
    const article = document.createElement('article');
    article.className = `chat-message ${kind}`;
    if (meta) {
      const heading = document.createElement('div');
      heading.className = 'message-meta';
      const author = document.createElement('span');
      author.textContent = kind === 'user' ? 'You' : selectedProviderLabel;
      const detail = document.createElement('span');
      detail.textContent = meta;
      heading.append(author, detail);
      article.append(heading);
    }
    const body = kind === 'assistant' ? document.createElement('pre') : document.createElement('p');
    body.textContent = text;
    article.append(body);
    chatThread.append(article);
    article.scrollIntoView({ block: 'nearest' });
    return { article, body };
  }

  function terminalSnapshot(maxLines = 140) {
    const buffer = terminal.buffer?.active;
    if (!buffer) return '';
    const first = Math.max(0, buffer.length - maxLines);
    const lines = [];
    for (let row = first; row < buffer.length; row += 1) {
      const line = buffer.getLine(row);
      if (line) lines.push(line.translateToString(true));
    }
    return lines.join('\n').replace(/[\s\n]+$/u, '');
  }

  function transcriptDelta(baseline, current, prompt) {
    if (!current) return '';
    const before = baseline.split('\n');
    const after = current.split('\n');
    let shared = 0;
    while (shared < before.length && shared < after.length && before[shared] === after[shared]) shared += 1;
    let delta = after.slice(shared).join('\n').trim();
    if (!delta && current !== baseline) delta = current.slice(-12000).trim();
    if (prompt && delta.startsWith(prompt)) delta = delta.slice(prompt.length).trimStart();
    return delta.slice(-16000);
  }

  function detectPermissionMode(text = terminalSnapshot(40)) {
    const lower = text.toLowerCase();
    if (lower.includes('auto mode on') || lower.includes('auto mode active')) return 'auto';
    if (lower.includes('manual mode on') || lower.includes('manual mode')) return 'manual';
    if (lower.includes('accept edits on') || lower.includes('accept edits')) return 'accept edits';
    if (lower.includes('plan mode on') || lower.includes('plan mode')) return 'plan';
    return 'unknown';
  }

  function updatePermissionMode() {
    if (selectedProvider !== 'claude') return;
    const detected = detectPermissionMode();
    if (detected !== 'unknown') permissionMode = detected;
    autoMode.textContent = permissionMode === 'auto' ? 'auto on' : 'auto off';
    autoMode.setAttribute('aria-pressed', String(permissionMode === 'auto'));
    autoMode.title = permissionMode === 'unknown'
      ? 'Cycle Claude permission mode until auto is active'
      : `Claude permission mode: ${permissionMode}`;
  }

  function scheduleChatMirror() {
    clearTimeout(chatMirrorTimer);
    chatMirrorTimer = setTimeout(() => {
      updatePermissionMode();
      if (!activeAssistantMessage) return;
      const delta = transcriptDelta(chatBaseline, terminalSnapshot(), lastSubmittedPrompt);
      if (delta) activeAssistantMessage.body.textContent = delta;
      activeAssistantMessage.article.classList.add('message-live');
    }, 90);
  }

  function renderSlashPalette() {
    slashPalette.replaceChildren();
    (SLASH_COMMANDS[selectedProvider] || ['/help']).forEach((command) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = command.trim();
      button.addEventListener('click', () => {
        chatInput.value = command;
        autoGrowComposer();
        chatInput.focus();
        slashPalette.hidden = true;
      });
      slashPalette.append(button);
    });
  }

  function autoGrowComposer() {
    chatInput.style.height = 'auto';
    chatInput.style.height = `${Math.min(chatInput.scrollHeight, 152)}px`;
  }

  function switchSessionView(view) {
    selectedView = view === 'terminal' ? 'terminal' : 'chat';
    const showTerminal = selectedView === 'terminal';
    chatView.hidden = showTerminal;
    terminalView.hidden = !showTerminal;
    viewTabs.forEach((button) => {
      const active = button.dataset.view === selectedView;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    if (showTerminal) {
      requestAnimationFrame(() => {
        fitAddon.fit();
        if (socketIsOpen()) {
          socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }));
        }
        terminal.focus();
      });
    } else {
      chatInput.focus();
    }
    if (window.matchMedia('(max-width: 700px)').matches) {
      strategyPanel.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }

  function socketUrl() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${scheme}//${location.host}/control/ws?host=${encodeURIComponent(selectedHost)}` +
      `&provider=${encodeURIComponent(selectedProvider)}` +
      `&cols=${terminal.cols}&rows=${terminal.rows}`;
  }

  function setTerminalState(text, state = '') {
    terminalState.textContent = text;
    terminalState.dataset.state = state;
  }

  function disconnectTerminal(clear = false) {
    wantConnection = false;
    deliberateClose = true;
    clearTimeout(reconnectTimer);
    if (socket) socket.close();
    socket = null;
    if (clear) terminal.clear();
    setTerminalState('ready');
    setComposerConnected(false);
    terminalHost.textContent = `${selectedProvider} · ${selectedHost}`;
  }

  function connectTerminal(clear = false) {
    wantConnection = true;
    clearTimeout(reconnectTimer);
    if (socket) {
      deliberateClose = true;
      socket.close();
    }
    deliberateClose = false;
    if (clear) terminal.clear();
    setTerminalState('connecting…');
    terminalHost.textContent = `${selectedProvider} · ${selectedHost}`;
    if (!terminalView.hidden) fitAddon.fit();
    const ws = new WebSocket(socketUrl());
    socket = ws;
    ws.addEventListener('open', () => {
      reconnectDelay = 1000;
      setTerminalState('connected', 'ok');
      setComposerConnected(true);
      if (selectedView === 'terminal') terminal.focus();
      else chatInput.focus();
      if (window.matchMedia('(max-width: 700px)').matches) {
        strategyPanel.scrollIntoView({ block: 'start', behavior: 'smooth' });
      }
      ws.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }));
    });
    ws.addEventListener('message', (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (_) { return; }
      if (msg.type === 'output') {
        terminal.write(base64ToBytes(msg.data), scheduleChatMirror);
      }
      if (msg.type === 'error') {
        setTerminalState('error', 'error');
        terminal.writeln(`\r\n[Nexus relay error: ${msg.message}]`);
      }
      if (msg.type === 'status') setTerminalState(msg.running ? 'connected' : 'exited');
    });
    ws.addEventListener('close', () => {
      if (socket !== ws) return;
      setTerminalState('disconnected');
      setComposerConnected(false);
      if (!deliberateClose && wantConnection) {
        reconnectTimer = setTimeout(() => connectTerminal(false), reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
      }
    });
    ws.addEventListener('error', () => {
      setTerminalState('connection error', 'error');
      setComposerConnected(false);
    });
  }

  terminal.onData((data) => {
    sendRaw(data);
  });

  function selectedProviderButton() {
    return providerTabs.find((button) => button.dataset.provider === selectedProvider)
      || providerTabs[0];
  }

  function updateUrl() {
    const url = new URL(location.href);
    url.pathname = '/control';
    url.searchParams.set('provider', selectedProvider);
    url.searchParams.set('mode', selectedMode);
    url.searchParams.set('host', selectedHost);
    history.replaceState(null, '', url);
  }

  function updateContext() {
    const providerButton = selectedProviderButton();
    selectedProviderLabel = providerButton?.dataset.label || selectedProvider;
    strategyProvider.textContent = selectedProviderLabel;
    workerProvider.textContent = selectedProviderLabel;
    strategySeat.textContent = selectedSeat;
    workerSeat.textContent = selectedSeat;
    strategyTitle.textContent = `${selectedProviderLabel} on ${selectedLabel}`;
    workerTitle.textContent = `${selectedLabel} ${selectedProviderLabel} bounded run`;
    strategyExplainer.textContent = `A real interactive ${selectedProviderLabel} CLI session. Native approvals, resume controls, artifacts, and conversation history stay with ${selectedProviderLabel} on this node.`;
    workerExplainer.textContent = `One scoped headless ${selectedProviderLabel} run using the same fleet profile and fixed model as Interactive mode. Only the exact prompt, timeout, evidence contract, and bounded approval policy differ.`;
    workerRunsTitle.textContent = `Recent ${selectedProviderLabel} bounded runs`;
    providerDetail.textContent = selectedMode === 'strategy'
      ? providerButton?.dataset.strategyDetail || ''
      : providerButton?.dataset.workerDetail || '';
    terminalElement.setAttribute('aria-label', `Interactive ${selectedProviderLabel} fleet terminal`);
    terminalHost.textContent = `${selectedProvider} · ${selectedHost}`;
    sessionLabel.textContent = `${selectedProviderLabel} · ${selectedLabel}`;
    chatProviderLabel.textContent = selectedProviderLabel;
    const modelOptions = JSON.parse(providerButton?.dataset.strategyModels || '[]');
    strategyModel.replaceChildren(...modelOptions.map((model) => {
      const option = document.createElement('option');
      option.value = model;
      option.textContent = model;
      return option;
    }));
    strategyModel.value = providerButton?.dataset.strategyModel || modelOptions[0] || '';
    autoMode.hidden = providerButton?.dataset.supportsAutoMode !== 'true';
    permissionMode = 'unknown';
    updatePermissionMode();
    renderSlashPalette();
    setComposerConnected(socketIsOpen());
    updateUrl();
  }

  function updateSelectedDevice(button, connect = false) {
    devices.forEach((item) => item.classList.toggle('selected', item === button));
    selectedHost = button.dataset.host;
    selectedSeat = button.dataset.seat;
    selectedLabel = button.querySelector('.device-name').textContent;
    disconnectTerminal(true);
    updateContext();
    if (connect && selectedMode === 'strategy') connectTerminal(false);
  }

  devices.forEach((button) => button.addEventListener('click', () => updateSelectedDevice(button)));

  providerTabs.forEach((button) => button.addEventListener('click', () => {
    providerTabs.forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-selected', String(active));
    });
    selectedProvider = button.dataset.provider;
    disconnectTerminal(true);
    updateContext();
    refreshStatus();
  }));

  tabs.forEach((tab) => tab.addEventListener('click', () => {
    tabs.forEach((item) => {
      const active = item === tab;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    const strategy = tab.dataset.panel === 'strategy';
    selectedMode = strategy ? 'strategy' : 'worker';
    strategyPanel.hidden = !strategy;
    strategyPanel.classList.toggle('active', strategy);
    workerPanel.hidden = strategy;
    workerPanel.classList.toggle('active', !strategy);
    if (!strategy) disconnectTerminal(false);
    updateContext();
    if (strategy) {
      requestAnimationFrame(() => {
        fitAddon.fit();
      });
    }
  }));

  viewTabs.forEach((button) => {
    button.addEventListener('click', () => switchSessionView(button.dataset.view));
  });
  document.querySelectorAll('[data-open-terminal]').forEach((button) => {
    button.addEventListener('click', () => switchSessionView('terminal'));
  });

  chatInput.addEventListener('input', () => {
    autoGrowComposer();
    slashPalette.hidden = !chatInput.value.startsWith('/');
  });
  chatInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      chatComposer.requestSubmit();
    }
    if (event.key === 'Escape') slashPalette.hidden = true;
  });
  slashButton.addEventListener('click', () => {
    slashPalette.hidden = !slashPalette.hidden;
    if (!slashPalette.hidden) chatInput.focus();
  });

  chatComposer.addEventListener('submit', (event) => {
    event.preventDefault();
    const prompt = chatInput.value.trim();
    if (!prompt || !socketIsOpen()) return;
    if (activeAssistantMessage) activeAssistantMessage.article.classList.remove('message-live');
    appendChatMessage('user', prompt, 'message');
    chatBaseline = terminalSnapshot();
    lastSubmittedPrompt = prompt;
    activeAssistantMessage = appendChatMessage('assistant', 'Waiting for CLI output…', 'live response');
    activeAssistantMessage.article.classList.add('message-live');
    sendRaw(`${prompt}\r`);
    chatInput.value = '';
    slashPalette.hidden = true;
    autoGrowComposer();
    chatInput.focus();
  });

  strategyModel.addEventListener('change', () => {
    if (!socketIsOpen()) return;
    const model = strategyModel.value;
    appendChatMessage('system', `Switching this live ${selectedProviderLabel} session to ${model}.`);
    sendRaw(`/model ${model}\r`);
  });

  autoMode.addEventListener('click', async () => {
    if (selectedProvider !== 'claude' || !socketIsOpen()) return;
    updatePermissionMode();
    const target = permissionMode === 'auto' ? 'manual' : 'auto';
    autoMode.disabled = true;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      if (target === 'auto' && permissionMode === 'auto') break;
      if (target === 'manual' && permissionMode !== 'auto' && permissionMode !== 'unknown') break;
      sendRaw('\u001b[Z');
      await new Promise((resolve) => setTimeout(resolve, 240));
      updatePermissionMode();
    }
    if (permissionMode === 'unknown') {
      appendChatMessage('system', 'Claude opened a native permission-mode control. Finish the selection in Terminal view.');
      switchSessionView('terminal');
    } else {
      appendChatMessage('system', `Claude permission mode: ${permissionMode}.`);
    }
    autoMode.disabled = false;
  });

  document.getElementById('reconnectTerminal').addEventListener('click', () => connectTerminal(false));
  document.getElementById('stopTerminal').addEventListener('click', async () => {
    disconnectTerminal(false);
    setTerminalState('stopping…');
    const response = await fetch(`/api/control/strategy/${encodeURIComponent(selectedProvider)}/${encodeURIComponent(selectedHost)}/stop`, {
      method: 'POST',
      headers: { 'Accept': 'application/json' },
    });
    setTerminalState(response.ok ? 'stopped' : 'stop failed', response.ok ? '' : 'error');
  });

  document.querySelectorAll('.keybar button').forEach((button) => {
    button.addEventListener('click', () => {
      const key = JSON.parse(`"${button.dataset.key}"`);
      sendRaw(key);
      terminal.focus();
    });
  });

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (strategyPanel.hidden || terminalView.hidden) return;
      fitAddon.fit();
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }));
      }
  }, 100);
  });

  if (window.visualViewport) {
    const updateKeyboardOffset = () => {
      const viewport = window.visualViewport;
      const offset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
      document.documentElement.style.setProperty('--nexus-keyboard-offset', `${offset}px`);
    };
    window.visualViewport.addEventListener('resize', updateKeyboardOffset);
    window.visualViewport.addEventListener('scroll', updateKeyboardOffset);
    updateKeyboardOffset();
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;',
    }[char]));
  }

  function renderRuns(runs) {
    const selectedRuns = runs.filter((run) => {
      const provider = run.provider === 'gemini' ? 'gemini' : run.provider;
      return provider === selectedProvider;
    });
    if (!selectedRuns.length) {
      workerRuns.innerHTML = '<p class="empty">No runs launched from this Nexus process.</p>';
      return;
    }
    workerRuns.innerHTML = selectedRuns.map((run) => {
      const output = [run.output, run.error].filter(Boolean).join('\n\n--- stderr ---\n');
      return `<details class="run">
        <summary><span>${escapeHtml(run.provider_label || selectedProviderLabel)} · ${escapeHtml(run.host)}</span>
          <span class="run-id">${escapeHtml(run.job_id)}</span>
          <span class="run-state ${escapeHtml(run.state)}">${escapeHtml(run.state)}</span>
        </summary>
        <pre>${escapeHtml(output || 'Waiting for output…')}</pre>
      </details>`;
    }).join('');
  }

  async function refreshStatus() {
    try {
      const response = await fetch('/api/control/status', { headers: { 'Accept': 'application/json' } });
      if (!response.ok) return;
      const data = await response.json();
      data.hosts.forEach((host) => {
        const button = devices.find((item) => item.dataset.host === host.key);
        if (!button) return;
        const state = button.querySelector('.device-state b');
        const strategy = host.strategies?.[selectedProvider] || {};
        state.textContent = strategy.running ? `${selectedProviderLabel} interactive live` : 'available';
      });
      if (data.routing) {
        const selectedRouting = data.routing[selectedProvider] || {};
        const strategy = selectedRouting.strategy || {};
        const worker = selectedRouting.worker || {};
        document.getElementById('strategyRouting').textContent = strategy.ok
          ? `Pinned interactive route: ${strategy.model} · ${strategy.state} · ${strategy.reason}`
          : `${selectedProviderLabel} interactive route unavailable: ${strategy.error || 'no eligible provider'}`;
        document.getElementById('workerRouting').textContent = worker.ok
          ? `Pinned bounded-run route: ${worker.model} · ${worker.state} · ${worker.reason}`
          : `${selectedProviderLabel} bounded-run route unavailable: ${worker.error || 'no eligible provider'}`;
      }
      renderRuns(data.workers);
    } catch (_) {
      document.getElementById('globalState').textContent = 'relay unavailable';
    }
  }

  document.getElementById('launchWorker').addEventListener('click', async () => {
    const prompt = document.getElementById('workerPrompt').value.trim();
    const confirmed = document.getElementById('workerConfirmed').checked;
    const timeoutSeconds = Number(document.getElementById('workerTimeout').value);
    const taskSize = document.getElementById('workerTaskSize').value;
    if (!prompt || !confirmed) {
      workerMessage.textContent = 'Add a bounded prompt and confirm that you reviewed it.';
      return;
    }
    const button = document.getElementById('launchWorker');
    button.disabled = true;
    workerMessage.textContent = `Launching ${selectedProviderLabel} for ${selectedSeat} on ${selectedLabel}…`;
    try {
      const response = await fetch('/api/control/workers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({
          host: selectedHost,
          provider: selectedProvider,
          prompt,
          timeout_seconds: timeoutSeconds,
          task_size: taskSize,
          confirmed: true,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      const route = data.routing || {};
      workerMessage.textContent = `Launched ${data.job_id} · ${route.state || 'routed'} · ${route.reason || `${selectedProviderLabel} pin`}.`;
      document.getElementById('workerConfirmed').checked = false;
      await refreshStatus();
    } catch (error) {
      workerMessage.textContent = `Launch failed: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  });

  function initializeSelection() {
    const params = new URLSearchParams(location.search);
    const provider = params.get('provider');
    const providerButton = providerTabs.find((button) => button.dataset.provider === provider)
      || providerTabs[0];
    const host = params.get('host');
    const hostButton = devices.find((button) => button.dataset.host === host) || devices[0];
    const mode = params.get('mode') === 'worker' ? 'worker' : 'strategy';
    const modeButton = tabs.find((button) => button.dataset.panel === mode) || tabs[0];

    selectedProvider = providerButton?.dataset.provider || 'claude';
    selectedHost = hostButton?.dataset.host || 'alpha';
    selectedSeat = hostButton?.dataset.seat || 'Worker2';
    selectedLabel = hostButton?.querySelector('.device-name')?.textContent || 'Alpha';
    selectedMode = mode;

    providerTabs.forEach((button) => {
      const active = button === providerButton;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    devices.forEach((button) => button.classList.toggle('selected', button === hostButton));
    tabs.forEach((button) => {
      const active = button === modeButton;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    const strategy = mode === 'strategy';
    strategyPanel.hidden = !strategy;
    strategyPanel.classList.toggle('active', strategy);
    workerPanel.hidden = strategy;
    workerPanel.classList.toggle('active', !strategy);
    updateContext();
  }

  initializeSelection();
  if (!terminalView.hidden) fitAddon.fit();
  refreshStatus();
  setInterval(refreshStatus, 4000);
})();
