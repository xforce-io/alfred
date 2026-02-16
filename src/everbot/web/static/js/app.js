let currentAgent = null;
let currentSessionId = null;
let ws = null;
let autoScroll = true; // 自动滚动开关
let traceModalData = null;
let currentTraceTab = 'trace';
let trajectoryIncludeHeartbeat = false;

// 智能自动滚动函数
function scrollToBottom() {
  const messagesDiv = document.getElementById('chat-messages');
  if (!messagesDiv || !autoScroll) return;

  // 使用 requestAnimationFrame 确保在 DOM 渲染完成后执行
  requestAnimationFrame(() => {
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
  });
}

function scrollToLatest() {
  const messagesDiv = document.getElementById('chat-messages');
  if (!messagesDiv) return;
  autoScroll = true;
  messagesDiv.scrollTop = messagesDiv.scrollHeight;
  // 手动触发一次隐藏，避免滚动过程中闪烁
  const latestBtn = document.getElementById('chat-latest');
  if (latestBtn) latestBtn.classList.remove('visible');
}

function updateLatestButtonVisibility() {
  const messagesDiv = document.getElementById('chat-messages');
  const latestBtn = document.getElementById('chat-latest');
  if (!messagesDiv || !latestBtn) return;

  // 如果距离底部超过 100px，显示“回到最新”按钮
  const isNearBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight < 100;
  latestBtn.classList.toggle('visible', !isNearBottom);
}

// 监听用户手动滚动
document.addEventListener('DOMContentLoaded', () => {
  const messagesDiv = document.getElementById('chat-messages');
  if (messagesDiv) {
    messagesDiv.addEventListener('scroll', () => {
      // 如果用户向上滚动超过 50px，停止自动跟随
      const isAtBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight < 50;
      autoScroll = isAtBottom;
      updateLatestButtonVisibility();
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeTraceModal();
    }
  });
});

// Tab 切换
function switchTab(tab) {
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.panel');

  tabs.forEach(t => t.classList.remove('active'));
  panels.forEach(p => p.classList.remove('active'));

  // 查找对应的 tab 元素（通过参数匹配，而不是依赖 event 对象）
  const activeTab = Array.from(tabs).find(t => t.getAttribute('onclick').includes(`'${tab}'`));
  if (activeTab) activeTab.classList.add('active');

  const panel = document.getElementById(tab + '-panel');
  if (panel) panel.classList.add('active');

  if (tab === 'status') loadStatus();
  if (tab === 'logs') connectLogStream();
  if (tab === 'settings') { loadTelegramSettings(); loadSkillsSettings(); }
}

// 加载 Agent 列表
async function loadAgents() {
  const res = await fetch('/api/agents');
  const agents = await res.json();
  const selector = document.getElementById('agent-selector');
  selector.innerHTML = agents.map(a =>
    `<option value="${a}">${a}</option>`
  ).join('');
  if (agents.length > 0) {
    currentAgent = agents[0];
    selector.value = currentAgent;
    await switchAgent(currentAgent);
  }
}

function resetChatView() {
  document.getElementById('chat-messages').innerHTML = '';
  currentAssistantMessageDiv = null;
  currentAssistantContent = "";
  lastAssistantMessageDiv = null;
  document.getElementById('chat-status-bar').textContent = "";
}

function renderSessionLabel(session) {
  const sid = session?.session_id || '';
  const msgCount = Number(session?.message_count || 0);
  const updatedAt = session?.updated_at ? String(session.updated_at).replace('T', ' ').slice(5, 16) : '';
  const isPrimary = !!currentAgent && sid === `web_session_${currentAgent}`;

  if (isPrimary) {
    return `默认会话 (${msgCount} 条)`;
  }

  let channelTag = '';
  if (sid.startsWith('tg_session_')) channelTag = '[TG] ';
  else if (sid.startsWith('discord_session_')) channelTag = '[Discord] ';
  const marker = sid.includes('__') ? sid.split('__')[1] : sid;
  const shortMarker = marker.length > 18 ? marker.slice(0, 18) : marker;
  const timePart = updatedAt ? ` · ${updatedAt}` : '';
  return `${channelTag}会话 ${shortMarker} (${msgCount} 条${timePart})`;
}

async function loadSessions(agent, preferredSessionId = null) {
  const sessionSelector = document.getElementById('session-selector');
  const res = await fetch(`/api/agents/${encodeURIComponent(agent)}/sessions?limit=20`);
  const payload = await res.json();
  const sessions = Array.isArray(payload?.sessions) ? payload.sessions : [];
  sessionSelector.innerHTML = sessions.map((s) => (
    `<option value="${s.session_id}" title="${escapeHtml(s.session_id)}">${escapeHtml(renderSessionLabel(s))}</option>`
  )).join('');

  const shouldHideSessionSelector = sessions.length <= 1;
  sessionSelector.style.display = shouldHideSessionSelector ? 'none' : '';

  if (sessions.length === 0) {
    currentSessionId = `web_session_${agent}`;
    sessionSelector.innerHTML = `<option value="${currentSessionId}" title="${escapeHtml(currentSessionId)}">默认会话 (0 条)</option>`;
    sessionSelector.value = currentSessionId;
    return currentSessionId;
  }

  const preferredExists = preferredSessionId && sessions.some((s) => s.session_id === preferredSessionId);
  if (preferredExists) {
    currentSessionId = preferredSessionId;
  } else {
    // Prefer a web session as default to avoid accidentally landing on a
    // TG/Discord session whose history could be mistakenly cleared.
    const firstWeb = sessions.find((s) => String(s.session_id).startsWith('web_session_'));
    currentSessionId = firstWeb ? firstWeb.session_id : sessions[0].session_id;
  }
  sessionSelector.value = currentSessionId;
  return currentSessionId;
}

async function switchAgent(agent) {
  currentAgent = agent;
  closeTraceModal();
  if (ws) ws.close();
  resetChatView();
  const sessionId = await loadSessions(agent);
  connectWebSocket(agent, sessionId);
}

// 切换 Agent / 会话
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('agent-selector').addEventListener('change', async (e) => {
    await switchAgent(e.target.value);
  });
  document.getElementById('session-selector').addEventListener('change', (e) => {
    const sessionId = e.target.value;
    if (!currentAgent || !sessionId) return;
    currentSessionId = sessionId;
    closeTraceModal();
    if (ws) ws.close();
    resetChatView();
    connectWebSocket(currentAgent, sessionId);
  });

  // 处理输入框的回车发送，并阻止默认行为（防止焦点漂移时意外触发表单或其他按钮）
  const chatInput = document.getElementById('chat-input');
  if (chatInput) {
    chatInput.addEventListener('keydown', (e) => {
      if (e.isComposing || e.repeat) return;
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    // 点击或聚焦输入框时，自动滚动到最新位置
    chatInput.addEventListener('click', scrollToLatest);
    chatInput.addEventListener('focus', scrollToLatest);
  }
});

let currentAssistantMessageDiv = null;
let currentAssistantContent = "";
let lastAssistantMessageDiv = null;
let lastSendFingerprint = '';
let lastSendTs = 0;

// WebSocket 连接（实时对话）
function connectWebSocket(agent, sessionId) {
  if (ws) ws.close();
  closeTraceModal();
  currentSessionId = sessionId || `web_session_${agent}`;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const query = new URLSearchParams({ session_id: currentSessionId });
  const socket = new WebSocket(`${protocol}//${location.host}/ws/chat/${encodeURIComponent(agent)}?${query.toString()}`);
  ws = socket;

  socket.onopen = () => {
    if (socket !== ws) return;
    document.getElementById('chat-status-bar').textContent = '';
    setLoading(false);
  };

  socket.onmessage = (event) => {
    if (socket !== ws) return;
    const data = JSON.parse(event.data);
    console.log('[DEBUG] ws.onmessage received:', data.type);
    if (data.type === 'message') {
      if (data.session_id) {
        currentSessionId = data.session_id;
        const sessionSelector = document.getElementById('session-selector');
        if (sessionSelector) sessionSelector.value = currentSessionId;
      }
      // If it's the first message from assistant after connection AND it's a greeting,
      // check if we should clear the placeholder.
      if (document.getElementById('chat-messages').children.length <= 1) {
        const messagesDiv = document.getElementById('chat-messages');
        if (messagesDiv.innerText.includes('选择一个 Agent 开始对话吧')) {
          console.log('[DEBUG] Clearing placeholder message');
          messagesDiv.innerHTML = '';
        }
      }
      if (data.source_type && data.source_type.startsWith('heartbeat') && data.role === 'assistant') {
        addMessage(data.role, `【心跳】\n${data.content}`);
      } else if (data.content && data.content.trim()) {
        addMessage(data.role, data.content);
      }
      setLoading(false);
    } else if (data.type === 'history') {
      if (data.session_id) {
        currentSessionId = data.session_id;
        const sessionSelector = document.getElementById('session-selector');
        if (sessionSelector) sessionSelector.value = currentSessionId;
      }
      // Clear current messages and load history.
      // History messages now include tool chain entries (role=assistant with tool_calls,
      // role=tool with tool_call_id) inline, preserving conversation order.
      console.log('[DEBUG] Received history:', data.messages?.length, 'messages');
      document.getElementById('chat-messages').innerHTML = '';

      // First pass: build tool_call_id → tool output lookup from role=tool messages
      const toolOutputMap = {};
      (data.messages || []).forEach(msg => {
        let role = (msg.role || '').toLowerCase().replace('role.', '');
        if (role === 'tool' && msg.tool_call_id) {
          toolOutputMap[msg.tool_call_id] = msg.content || '';
        }
      });

      // Second pass: render in order
      (data.messages || []).forEach(msg => {
        let role = (msg.role || '').toLowerCase().replace('role.', '');
        if (role === 'system') return;

        if (role === 'tool') {
          // Already handled via toolOutputMap when rendering the assistant tool_calls
          return;
        }

        if (role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
          // Render each tool call as a completed card
          msg.tool_calls.forEach((tc, i) => {
            const tcId = tc.id || tc.tool_call_id || ('history-tc-' + i);
            const fn = tc.function || {};
            handleSkillProgress({
              type: 'skill',
              id: tcId,
              status: 'completed',
              skill_name: fn.name || tc.name || '(tool)',
              skill_args: fn.arguments || tc.arguments || tc.args || '',
              skill_output: toolOutputMap[tcId] || '',
            });
          });
          // Also render the assistant text if it has meaningful content
          if (msg.content && msg.content.trim()) {
            addMessage('assistant', msg.content, true);
          }
          return;
        }

        // Heartbeat-injected assistant messages
        if (role === 'assistant' && msg.metadata && msg.metadata.source === 'heartbeat') {
          addMessage(role, `【心跳】\n${msg.content}`, true);
          return;
        }

        // Regular user or assistant message
        if (msg.content && msg.content.trim()) {
          addMessage(role, msg.content, true);
        }
      });

      setLoading(false);
      autoScroll = true;
      scrollToBottom();
    } else if (data.type === 'delta') {
      setLoading(true);
      if (!currentAssistantMessageDiv) {
        // Create a new assistant message for the stream
        currentAssistantMessageDiv = createMessageElement('assistant');
        currentAssistantContent = "";
        // 显示"生成中"占位符
        const bubble = currentAssistantMessageDiv.querySelector('.message-bubble');
        bubble.innerHTML = '<span class="typing-indicator"><span></span><span></span><span></span></span>';
      }
      currentAssistantContent += data.content;
      updateMessageContent(currentAssistantMessageDiv, currentAssistantContent);
    } else if (data.type === 'skill') {
      handleSkillProgress(data);
    } else if (data.type === 'end') {
      lastAssistantMessageDiv = currentAssistantMessageDiv;
      currentAssistantMessageDiv = null;
      currentAssistantContent = "";
      setLoading(false);
      document.getElementById('chat-status-bar').textContent = "";
    } else if (data.type === 'mailbox_drain') {
      if (data.events && data.events.length > 0) {
        data.events.forEach(function(content) {
          const msgDiv = createMessageElement('assistant');
          msgDiv.classList.add('mailbox-message');
          updateMessageContent(msgDiv, '[后台更新] ' + content);
        });
        scrollToBottom();
      }
    } else if (data.type === 'status') {
      document.getElementById('chat-status-bar').textContent = data.content;
    } else if (data.type === 'error') {
      addMessage('assistant', '错误: ' + data.content);
      setLoading(false);
    }
  };

  socket.onerror = (error) => {
    if (socket !== ws) return;
    console.error('WebSocket error:', error);
    document.getElementById('chat-status-bar').textContent = '连接异常，请重试';
    setLoading(false);
  };

  socket.onclose = () => {
    if (socket !== ws) return;
    document.getElementById('chat-status-bar').textContent = '连接已断开，请重试';
    setLoading(false);
  };
}

// 设置加载状态（显示/隐藏停止按钮）
function setLoading(loading) {
  const sendBtn = document.getElementById('chat-send');
  const stopBtn = document.getElementById('chat-stop');
  if (loading) {
    sendBtn.style.display = 'none';
    stopBtn.style.display = 'inline-block';
  } else {
    sendBtn.style.display = 'inline-block';
    stopBtn.style.display = 'none';
    // 关键：当按钮切换（尤其是停止按钮消失）时，将焦点强行还给输入框
    // 防止焦点漂移到顶部的“新建对话”按钮上
    document.getElementById('chat-input').focus();
  }
}

// 发送消息
function sendMessage() {
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message || !currentAgent) return;

  const now = Date.now();
  const fingerprint = `${currentAgent}|${currentSessionId || ''}|${message}`;
  if (fingerprint === lastSendFingerprint && now - lastSendTs < 1200) {
    return;
  }
  lastSendFingerprint = fingerprint;
  lastSendTs = now;

  if (!ws || ws.readyState !== WebSocket.OPEN) {
    document.getElementById('chat-status-bar').textContent = '连接未就绪，正在重连...';
    setLoading(false);
    connectWebSocket(currentAgent, currentSessionId);
    return;
  }

  document.getElementById('chat-status-bar').textContent = "";
  addMessage('user', message);
  input.value = '';
  setLoading(true);

  ws.send(JSON.stringify({ message }));

  // 确保发送后输入框依然保有焦点
  input.focus();
}

// 停止消息
function stopMessage() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ action: 'stop' }));
  }
  setLoading(false);
  // 停止后立即将焦点返回输入框
  document.getElementById('chat-input').focus();
}

function createMessageElement(role, isHistory = false) {
  const messagesDiv = document.getElementById('chat-messages');
  const messageDiv = document.createElement('div');
  messageDiv.className = `message ${role}${isHistory ? ' history' : ''}`;
  messageDiv.innerHTML = `<div class="message-bubble"></div>`;
  messagesDiv.appendChild(messageDiv);
  scrollToBottom();
  return messageDiv;
}

function updateMessageContent(messageDiv, content) {
  const bubble = messageDiv.querySelector('.message-bubble');
  bubble.innerHTML = formatMessage(content);
  scrollToBottom();
}

// 添加消息到界面
function addMessage(role, content, isHistory = false) {
  const messageDiv = createMessageElement(role, isHistory);
  updateMessageContent(messageDiv, content);
}

// 格式化消息内容（使用 marked.js）
function formatMessage(content) {
  if (!content) return '';

  // 配置 marked 渲染器以添加代码语言标记
  const renderer = new marked.Renderer();

  renderer.code = function (tokenOrCode, languageOrNone) {
    // 适配新版 marked (v11+) 传入 token 对象，以及旧版传入字符串的情况
    const isNewVersion = typeof tokenOrCode === 'object';
    const code = isNewVersion ? tokenOrCode.text : tokenOrCode;
    const language = (isNewVersion ? tokenOrCode.lang : languageOrNone) || 'text';

    return `
      <div class="code-block-container">
        <div class="code-block-header">
          <span class="code-block-lang">${language}</span>
          <button class="copy-button" onclick="copyToClipboard(this)">复制</button>
        </div>
        <pre><code class="language-${language}">${code}</code></pre>
      </div>
    `;
  };

  marked.setOptions({
    renderer: renderer,
    breaks: true,
    gfm: true,
  });

  // 使用 marked 解析 Markdown，并使用 DOMPurify 进行过滤
  return DOMPurify.sanitize(marked.parse(content));
}

// 加载状态
async function loadStatus() {
  const res = await fetch('/api/status');
  const status = await res.json();

  const running = status.snapshot?.status === 'running';
  document.getElementById('daemon-status').innerHTML = `
    <span class="status-badge ${running ? 'running' : 'stopped'}">
      ${running ? '运行中' : '已停止'}
    </span>
    ${status.pid ? `<span style="margin-left:12px; color:#666;">PID: ${status.pid}</span>` : ''}
  `;

  const agents = status.snapshot?.agents || [];
  document.getElementById('agents-list').innerHTML = agents.length > 0
    ? agents.map(a => `<div style="padding:4px 0;">• ${a}</div>`).join('')
    : '<div style="color:#999;">无 Agent</div>';
}

// 心跳事件配置
const EVENT_CONFIG = {
  idle:            { icon: '💤', color: '#9ca3af', label: '空闲', hidden: true },
  reflect_skipped: { icon: '⏭️', color: '#9ca3af', label: '反思跳过' },
  reflect:         { icon: '🔍', color: '#2563eb', label: '反思' },
  task_start:      { icon: '🚀', color: '#d97706', label: '任务开始' },
  task_done:       { icon: '✅', color: '#059669', label: '任务完成' },
  task_failed:     { icon: '❌', color: '#dc2626', label: '任务失败' },
  skipped:         { icon: '⏭️', color: '#9ca3af', label: '跳过' },
  corrupted:       { icon: '⚠️', color: '#dc2626', label: '格式损坏' },
};
const MAX_EVENTS = 200;
let logEventSource = null;

function renderEventDescription(evt) {
  const type = evt.event || '';
  switch (type) {
    case 'idle': return '暂无待办任务';
    case 'reflect_skipped': return evt.reason === 'disabled' ? '反思已禁用' : '文件未变更，跳过反思';
    case 'reflect': return evt.result === 'proposals' ? `发现 ${evt.proposal_count || 0} 个新 routine 提议` : '反思完成，无新提议';
    case 'task_start': return `${evt.title || evt.task_id || ''}${evt.execution_mode === 'isolated' ? ' (隔离)' : ''}`;
    case 'task_done': return evt.title || evt.task_id || '';
    case 'task_failed': return `${evt.title || evt.task_id || ''} — ${evt.error || '未知错误'}`;
    case 'skipped': return evt.reason === 'inactive' ? '非活跃时段' : '会话锁冲突';
    case 'corrupted': return 'HEARTBEAT.md JSON 解析失败';
    default: return type;
  }
}

function connectLogStream() {
  const container = document.getElementById('heartbeat-events');
  if (!container) return;
  container.innerHTML = '<div class="event-item event-item--skipped" style="justify-content:center;">等待事件...</div>';

  if (logEventSource) { logEventSource.close(); logEventSource = null; }

  const es = new EventSource('/api/logs/heartbeat/events/stream');
  logEventSource = es;

  let firstEvent = true;
  es.onmessage = (e) => {
    if (firstEvent) { container.innerHTML = ''; firstEvent = false; }

    let evt;
    try { evt = JSON.parse(e.data); } catch (_) { return; }

    const type = evt.event || 'unknown';
    const config = EVENT_CONFIG[type] || { icon: '❓', color: '#6b7280', label: type };
    if (config.hidden) return;

    const ts = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString('zh-CN', { hour12: false }) : '';
    const desc = renderEventDescription(evt);
    const agentTag = evt.agent ? `<span class="event-agent">${escapeHtml(evt.agent)}</span>` : '';

    const el = document.createElement('div');
    el.className = `event-item event-item--${escapeHtml(type)}`;
    el.innerHTML = `
      <span class="event-time">${escapeHtml(ts)}</span>
      <span class="event-icon">${config.icon}</span>
      <span class="event-label" style="color:${config.color}">${escapeHtml(config.label)}</span>
      ${agentTag}
      <span class="event-desc">${escapeHtml(desc)}</span>
    `;
    container.appendChild(el);

    // Trim old events
    while (container.children.length > MAX_EVENTS) {
      container.removeChild(container.firstChild);
    }
    container.scrollTop = container.scrollHeight;
  };
}

// 触发心跳
async function triggerHeartbeat() {
  const agent = prompt('输入 Agent 名称:');
  if (!agent) return;
  await fetch(`/api/agents/${encodeURIComponent(agent)}/heartbeat?force=true`, {
    method: 'POST'
  });
  alert('心跳已触发');
}

function handleSkillProgress(data) {
  const messagesDiv = document.getElementById('chat-messages');
  const cardId = 'skill-card-' + data.id;
  let targetCard = document.getElementById(cardId);

  // 如果有正在流式输出的助手消息，先"冻结"它（结束当前流），
  // 这样工具卡片会在它之后出现，保持时间顺序
  if (currentAssistantMessageDiv && currentAssistantMessageDiv.isConnected && !targetCard) {
    // 保存当前流式消息，以便后续文字继续时创建新消息块
    lastAssistantMessageDiv = currentAssistantMessageDiv;
    currentAssistantMessageDiv = null;
  }

  if (!messagesDiv) return; // 安全检查

  // 提取用于展示的参数简述
  let argsSummary = '';
  if (data.skill_args) {
    try {
      const args = typeof data.skill_args === 'string' ? JSON.parse(data.skill_args) : data.skill_args;
      if (Array.isArray(args)) {
        argsSummary = args.map(a => typeof a === 'object' ? (a.value || JSON.stringify(a)) : a).join(', ');
      } else if (typeof args === 'object') {
        argsSummary = Object.values(args).join(', ');
      } else {
        argsSummary = args;
      }
    } catch (e) {
      argsSummary = data.skill_args;
    }
  }
  const displayTitle = `${data.skill_name}(${argsSummary})`;
  const safeDisplayTitle = escapeHtml(displayTitle);
  const safeSkillOutput = data.skill_output == null ? '' : escapeHtml(String(data.skill_output));

  if (data.status === 'processing') {
    const cardHtml = `
      <div class="tool-call gradient-border">
        <div class="tool-call-header">
          <div class="tool-call-icon pulse">🛠️</div>
          <div class="tool-call-name">${safeDisplayTitle}</div>
        </div>
        <div class="tool-call-status">执行中...</div>
        ${safeSkillOutput ? `<div class="tool-call-preview"><pre><code>${safeSkillOutput}</code></pre></div>` : ''}
      </div>
    `;

    if (targetCard) {
      targetCard.innerHTML = cardHtml;
    } else {
      targetCard = document.createElement('div');
      targetCard.id = cardId;
      targetCard.className = 'message assistant tool';
      targetCard.innerHTML = cardHtml;
      messagesDiv.appendChild(targetCard);
    }
  } else if (data.status === 'completed' || data.status === 'failed') {
    const isSuccess = data.status === 'completed';
    const statusIcon = isSuccess ? '✅' : '❌';
    const statusClass = isSuccess ? 'status-success' : 'status-failed';
    const outputId = 'tool-output-' + data.id;
    const outputMeta = data.skill_output_truncated
      ? `<div class="tool-result-meta">输出过长，已截断展示（total: ${data.skill_output_total_chars} chars）</div>`
      : '';

    const cardHtml = `
      <div class="tool-result ${statusClass}">
        <div class="tool-result-header">
          <div class="tool-result-icon">${statusIcon}</div>
          <div class="tool-result-name">${safeDisplayTitle}</div>
        </div>
        ${outputMeta}
        <div id="${outputId}" class="tool-result-content">
          ${safeSkillOutput
        ? `<pre><code>${safeSkillOutput}</code></pre>`
        : '<pre><code><span style="opacity:0.5; font-style:italic;">(no output)</span></code></pre>'
      }
        </div>
      </div>
    `;

    if (targetCard) {
      targetCard.innerHTML = cardHtml;
    } else {
      targetCard = document.createElement('div');
      targetCard.id = cardId;
      targetCard.className = 'message assistant tool';
      targetCard.innerHTML = cardHtml;
      messagesDiv.appendChild(targetCard);
    }
  }

  scrollToBottom();
}

// 复制代码到剪贴板
async function copyToClipboard(button) {
  const container = button.closest('.code-block-container');
  const codeElement = container.querySelector('code');
  const text = codeElement.innerText;

  try {
    await navigator.clipboard.writeText(text);
    const originalText = button.innerText;
    button.innerText = '已复制!';
    button.classList.add('copied');
    setTimeout(() => {
      button.innerText = originalText;
      button.classList.remove('copied');
    }, 2000);
  } catch (err) {
    console.error('Failed to copy:', err);
  }
}

function describeElement(el) {
  if (!el) return 'none';
  const tag = (el.tagName || '').toLowerCase();
  const id = el.id ? `#${el.id}` : '';
  const className = (el.className || '').toString().trim().replace(/\s+/g, '.');
  const cls = className ? `.${className}` : '';
  return `${tag}${id}${cls}`;
}

function buildNewConversationDebugMeta(evt) {
  const trigger = evt?.type ? `event:${evt.type}` : 'programmatic';
  const trusted = evt?.isTrusted === true;
  const activeElement = describeElement(document.activeElement);
  const stack = (new Error('newConversation_trace')).stack || '';
  const stackPreview = stack
    .split('\n')
    .slice(1, 4)
    .map((line) => line.trim())
    .join(' | ')
    .slice(0, 300);
  return {
    trigger,
    trusted,
    active_element: activeElement,
    stack_preview: stackPreview || 'none',
  };
}

// 新建对话
async function newConversation(evt = null) {
  if (!currentAgent) return;
  const debugMeta = buildNewConversationDebugMeta(evt);
  console.warn('[SessionDebug] newConversation invoked', debugMeta);
  const confirmed = window.confirm('将创建一个新的独立会话，并保留当前会话历史。是否继续？');
  if (!confirmed) {
    console.warn('[SessionDebug] newConversation cancelled by user', debugMeta);
    return;
  }
  let createdSessionId = null;
  try {
    const response = await fetch(`/api/agents/${encodeURIComponent(currentAgent)}/sessions`, {
      method: 'POST',
      headers: {
        'X-Everbot-Trigger': debugMeta.trigger,
        'X-Everbot-Trusted': String(debugMeta.trusted),
        'X-Everbot-Active-Element': debugMeta.active_element,
        'X-Everbot-Callsite': debugMeta.stack_preview,
      },
    });
    if (!response.ok) {
      throw new Error(`Create session failed: ${response.status}`);
    }
    const payload = await response.json();
    createdSessionId = payload?.session_id || null;
    console.warn('[SessionDebug] newConversation created session', {
      ...debugMeta,
      created_session_id: createdSessionId,
    });
  } catch (e) {
    console.error('Failed to create session:', e);
    return;
  }
  await loadSessions(currentAgent, createdSessionId);
  resetChatView();
  if (ws) ws.close();
  traceModalData = null;
  connectWebSocket(currentAgent, currentSessionId);
}

async function resetEnvironment() {
  if (!currentAgent) return;
  const first = window.confirm('将重置该 Agent 的运行环境（会删除所有会话与 tmp 产物）。是否继续？');
  if (!first) return;
  const second = window.confirm('这是不可恢复操作。请再次确认执行“重置环境”。');
  if (!second) return;
  try {
    await fetch(`/api/agents/${encodeURIComponent(currentAgent)}/sessions/reset`, { method: 'POST' });
  } catch (e) {
    console.error('Failed to reset environment:', e);
    return;
  }
  traceModalData = null;
  await loadSessions(currentAgent);
  resetChatView();
  if (ws) ws.close();
  connectWebSocket(currentAgent, currentSessionId);
}

function getSessionChannelLabel(sessionId) {
  if (!sessionId) return '';
  if (sessionId.startsWith('tg_session_')) return 'Telegram';
  if (sessionId.startsWith('discord_session_')) return 'Discord';
  return '';
}

async function clearHistory() {
  if (!currentAgent) return;
  // Read session_id from the dropdown DOM at click time, not the global variable,
  // so that async updates to currentSessionId cannot cause accidental clearing.
  const selector = document.getElementById('session-selector');
  const targetSessionId = (selector && selector.value) || currentSessionId;
  if (!targetSessionId) return;
  const channel = getSessionChannelLabel(targetSessionId);
  const msg = channel
    ? `注意：你正在清除 ${channel} 频道的会话历史（${targetSessionId}）。\n此操作将影响 ${channel} 端的对话上下文，是否继续？`
    : '将清除当前会话的所有聊天记录。是否继续？';
  const confirmed = window.confirm(msg);
  if (!confirmed) return;
  try {
    await fetch(`/api/agents/${encodeURIComponent(currentAgent)}/sessions/${encodeURIComponent(targetSessionId)}/clear-history`, { method: 'POST' });
  } catch (e) {
    console.error('Failed to clear history:', e);
    return;
  }
  resetChatView();
  if (ws) ws.close();
  traceModalData = null;
  connectWebSocket(currentAgent, currentSessionId);
}

function escapeHtml(text) {
  const value = text == null ? '' : String(text);
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

async function fetchTraceData() {
  if (!currentAgent) return null;
  const activeSessionId = currentSessionId || `web_session_${currentAgent}`;
  const query = new URLSearchParams({ session_id: activeSessionId });
  const response = await fetch(`/api/agents/${encodeURIComponent(currentAgent)}/session/trace?${query.toString()}`);
  if (!response.ok) {
    throw new Error(`Trace API failed: ${response.status}`);
  }
  const payload = await response.json();
  if (payload && payload.session_id) {
    currentSessionId = payload.session_id;
  }
  return payload;
}

function openTraceModal() {
  if (!currentAgent) return;
  const overlay = document.getElementById('trace-modal-overlay');
  if (!overlay) return;
  overlay.classList.add('open');
  fetchTraceData()
    .then((data) => {
      traceModalData = data;
      renderTrace(data);
      renderTrajectory(data);
      renderDolphinTrajectory(data);
      switchTraceTab(currentTraceTab);
    })
    .catch((error) => {
      console.error('Failed to fetch trace data:', error);
      const err = `<div class="trajectory-event-meta">Failed to load trace data.</div>`;
      ['trace-content', 'trajectory-content', 'dolphin-content'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = err;
      });
    });
}

function closeTraceModal(event) {
  if (event && event.target) {
    const overlay = document.getElementById('trace-modal-overlay');
    if (event.target !== overlay) {
      return;
    }
  }
  const overlay = document.getElementById('trace-modal-overlay');
  if (overlay) overlay.classList.remove('open');
}

function switchTraceTab(tab) {
  currentTraceTab = tab;
  const tabs = {
    trace: document.getElementById('trace-tab-trace'),
    trajectory: document.getElementById('trace-tab-trajectory'),
    dolphin: document.getElementById('trace-tab-dolphin'),
  };
  const contents = {
    trace: document.getElementById('trace-content'),
    trajectory: document.getElementById('trajectory-content'),
    dolphin: document.getElementById('dolphin-content'),
  };
  Object.entries(tabs).forEach(([key, el]) => { if (el) el.classList.toggle('active', key === tab); });
  Object.entries(contents).forEach(([key, el]) => { if (el) el.classList.toggle('active', key === tab); });
}

function toggleTraceExpand(id, btn) {
  const body = document.getElementById(id);
  if (!body) return;
  const isCollapsed = body.classList.contains('collapsed');
  body.classList.toggle('collapsed', !isCollapsed);
  if (btn) btn.textContent = isCollapsed ? '收起' : '展开';
}

function toggleTraceNode(id) {
  const node = document.getElementById(id);
  if (node) {
    node.classList.toggle('expanded');
    const header = node.querySelector('.trace-node-header');
    // Optional: add some feedback on toggle
  }
}

function renderTrace(data) {
  const container = document.getElementById('trace-content');
  if (!container) return;

  const trace = data?.context_trace || {};
  const rawCallChain = Array.isArray(trace?.call_chain) ? trace.call_chain : [];
  const llmInteractions = Array.isArray(trace?.llm_interactions) ? trace.llm_interactions : [];
  const llmSummary = trace?.llm_summary || {};
  const skillSummary = trace?.skill_summary || {};
  const executionSummary = trace?.execution_summary || {};

  // Summary Stat Bar
  const summaryHtml = `
    <div class="trace-summary">
      <div class="trace-summary-item">
        <div class="trace-summary-label">Stages</div>
        <div class="trace-summary-value">${executionSummary.total_stages || 0}</div>
      </div>
      <div class="trace-summary-item">
        <div class="trace-summary-label">Total Time</div>
        <div class="trace-summary-value">${((executionSummary.total_execution_time || 0) * 1000).toFixed(0)}ms</div>
      </div>
      <div class="trace-summary-item">
        <div class="trace-summary-label">LLM Tokens</div>
        <div class="trace-summary-value">${llmSummary.total_tokens || 0}</div>
      </div>
      <div class="trace-summary-item">
        <div class="trace-summary-label">Skill Calls</div>
        <div class="trace-summary-value">${skillSummary.total_stages || 0}</div>
      </div>
    </div>
  `;

  let nodeIdCounter = 0;
  const llmById = new Map();
  llmInteractions.forEach((item) => {
    if (item && item.id) llmById.set(item.id, item);
  });

  function renderNode(node, isNested = false) {
    const id = `trace-node-${nodeIdCounter++}`;
    const stageType = (node.stage_type || '').toLowerCase();
    const llmInteraction = stageType.includes('llm') && node.id ? llmById.get(node.id) : null;
    const displayNode = llmInteraction
      ? {
        ...node,
        messages: Array.isArray(node.messages) && node.messages.length > 0
          ? node.messages
          : (llmInteraction.input_messages || []),
        tokens: node.tokens ?? llmInteraction.input_tokens ?? 0,
      }
      : node;

    const name = displayNode.name || displayNode.type || 'node';
    const status = (displayNode.status || '').toLowerCase();
    const durationMs = displayNode.duration ? Math.round(displayNode.duration * 1000) : 0;

    // Choose icon and badge based on type
    let icon = '🔘';
    let badgeClass = 'badge-flow';

    if (stageType.includes('llm')) {
      icon = '🤖';
      badgeClass = 'badge-llm';
    } else if (stageType.includes('skill') || stageType.includes('tool')) {
      icon = '🛠️';
      badgeClass = 'badge-tool';
    } else if (status === 'error' || status === 'failed') {
      icon = '❌';
      badgeClass = 'badge-error';
    } else if (name.includes('loop') || name.includes('flow')) {
      icon = '🔄';
    }

    // Extract quick data for the grid
    const quickData = [];
    if (displayNode.model) quickData.push({ label: 'Model', value: displayNode.model });
    if (displayNode.tokens) quickData.push({ label: 'Tokens', value: displayNode.tokens });
    if (displayNode.tool_name) quickData.push({ label: 'Tool', value: displayNode.tool_name });
    if (displayNode.exit_code !== undefined) quickData.push({ label: 'Exit Code', value: displayNode.exit_code });

    const childrenHtml = Array.isArray(displayNode.children) && displayNode.children.length > 0
      ? `<div class="trace-node-nested">${displayNode.children.map(c => renderNode(c, true)).join('')}</div>`
      : '';

    const html = `
      <div id="${id}" class="trace-node ${displayNode.children && displayNode.children.length > 0 ? 'has-children' : ''}">
        <div class="trace-node-header" onclick="toggleTraceNode('${id}')">
          <div class="trace-node-main">
            <div class="trace-node-icon">${icon}</div>
            <div class="trace-node-title">${escapeHtml(name)}</div>
            <div class="trace-node-badge ${badgeClass}">${escapeHtml(stageType || 'stage')}</div>
          </div>
          <div class="trace-node-meta">
            ${status ? `<div class="trace-node-status ${status}">${escapeHtml(status)}</div>` : ''}
            <div class="trace-node-duration">${durationMs}ms</div>
            <div class="trace-node-chevron">▼</div>
          </div>
        </div>
        <div class="trace-node-body">
          <div class="trace-node-content">
            ${quickData.length > 0 ? `
              <div class="trace-data-grid">
                ${quickData.map(d => `
                  <div class="trace-data-card">
                    <div class="trace-data-label">${escapeHtml(d.label)}</div>
                    <div class="trace-data-value">${escapeHtml(String(d.value))}</div>
                  </div>
                `).join('')}
              </div>
            ` : ''}
            ${renderNodeStructuredContent(displayNode)}
            <div class="trace-raw-container">
              <button class="trace-raw-toggle" onclick="event.stopPropagation(); toggleRawJson('${id}')">显示原始数据 (JSON)</button>
              <pre class="trace-node-json"><code>${escapeHtml(JSON.stringify(displayNode, null, 2))}</code></pre>
            </div>
            ${childrenHtml}
          </div>
        </div>
      </div>
    `;
    return html;
  }

  const nodesHtml = rawCallChain.length > 0
    ? `<div class="trace-tree-container">${rawCallChain.map(n => renderNode(n)).join('')}</div>`
    : `<div class="trajectory-event-meta">No context trace data. Send a message first.</div>`;
  container.innerHTML = summaryHtml + nodesHtml;
}

function renderTrajectory(data) {
  const container = document.getElementById('trajectory-content');
  if (!container) return;
  const allEvents = Array.isArray(data?.timeline) ? data.timeline : [];
  const events = allEvents.filter((event) => {
    const sourceType = event?.source_type || 'chat_user';
    if (trajectoryIncludeHeartbeat) return true;
    return sourceType !== 'heartbeat';
  });
  if (events.length === 0) {
    const filterHint = trajectoryIncludeHeartbeat
      ? 'No timeline events.'
      : 'No timeline events for chat view. Enable heartbeat filter to include background events.';
    container.innerHTML = `<div class="trajectory-event-meta">${filterHint}</div>`;
    return;
  }

  // Find turn_end event to get total_duration_ms from backend
  const turnEndEvent = events.find(e => e.type === 'turn_end');
  const backendTotalMs = turnEndEvent?.total_duration_ms || 0;

  const withTiming = events.map((event, idx) => {
    const ts = Date.parse(event?.timestamp || '');
    let durationMs = 0;

    if (event.type === 'turn_end') {
      // turn_end itself has 0 duration (it's the end marker)
      durationMs = 0;
    } else if (idx < events.length - 1) {
      // Calculate duration from this event to next event
      const nextTs = Date.parse(events[idx + 1]?.timestamp || '');
      durationMs = Number.isFinite(ts) && Number.isFinite(nextTs) && nextTs >= ts ? (nextTs - ts) : 0;
    }
    // For the last non-turn_end event, durationMs stays 0 (handled by totalMs from backend)

    return { ...event, durationMs };
  });

  // Use backend total if available, otherwise sum calculated durations
  const calculatedTotal = withTiming.reduce((sum, item) => sum + item.durationMs, 0);
  const totalMs = backendTotalMs || calculatedTotal;
  const colors = ['#2563eb', '#059669', '#d97706', '#7c3aed', '#dc2626', '#0f766e'];

  // Build summary header with total duration
  const summaryHtml = totalMs > 0
    ? `<div class="trace-summary" style="margin-bottom:14px;">
        <div class="trace-summary-item">
          <div class="trace-summary-label">Total Duration</div>
          <div class="trace-summary-value">${totalMs}ms</div>
        </div>
        <div class="trace-summary-item">
          <div class="trace-summary-label">Events</div>
          <div class="trace-summary-value">${events.length} / ${allEvents.length}</div>
        </div>
      </div>`
    : '';

  const filterHtml = `
    <div class="trajectory-event-meta" style="margin-bottom: 10px;">
      <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;">
        <input type="checkbox" ${trajectoryIncludeHeartbeat ? 'checked' : ''} onchange="toggleTrajectoryHeartbeat(this.checked)">
        Include heartbeat events
      </label>
    </div>
  `;

  const barHtml = totalMs > 0
    ? `
      <div class="trajectory-duration-bar">
        <div class="trace-summary-label" style="margin-bottom:6px;">Duration distribution</div>
        <div class="trajectory-duration-track">
          ${withTiming.filter(item => item.durationMs > 0).map((item, idx) => {
      const percent = Math.max((item.durationMs / totalMs) * 100, 0.8);
      return `<div class="trajectory-duration-segment" style="width:${percent}%; background:${colors[idx % colors.length]}" title="${escapeHtml(item.type || 'event')} ${item.durationMs}ms"></div>`;
    }).join('')}
        </div>
      </div>
    `
    : '';

  const eventHtml = withTiming.map((event) => {
    const meta = { ...event };
    delete meta.type;
    delete meta.timestamp;
    delete meta.durationMs; // Don't show computed durationMs in meta
    const durationDisplay = event.durationMs ? ` | +${event.durationMs}ms` : (event.total_duration_ms ? ` | total: ${event.total_duration_ms}ms` : '');
    return `
      <div class="trajectory-event">
        <div class="trajectory-event-top">
          <div class="trajectory-event-type">${escapeHtml(`${event.type || 'event'} · ${event.source_type || 'chat_user'}`)}</div>
          <div class="trajectory-event-time">${escapeHtml(event.timestamp || '')}${durationDisplay}</div>
        </div>
        <div class="trajectory-event-meta">${escapeHtml(JSON.stringify(meta, null, 2))}</div>
      </div>
    `;
  }).join('');

  container.innerHTML = filterHtml + summaryHtml + barHtml + eventHtml;
}

function toggleTrajectoryHeartbeat(enabled) {
  trajectoryIncludeHeartbeat = !!enabled;
  if (traceModalData) {
    renderTrajectory(traceModalData);
  }
}

function renderDolphinTrajectory(data) {
  const container = document.getElementById('dolphin-content');
  if (!container) return;
  const dt = data?.trajectory || {};
  const messages = Array.isArray(dt?.trajectory) ? dt.trajectory : [];
  const stages = Array.isArray(dt?.stages) ? dt.stages : [];

  if (messages.length === 0) {
    container.innerHTML = '<div class="trajectory-event-meta">No trajectory data. Send a message first, then reopen Trace.</div>';
    return;
  }

  // Summary
  const systemMsgs = messages.filter(m => m.role === 'system');
  const userMsgs = messages.filter(m => m.role === 'user');
  const assistantMsgs = messages.filter(m => m.role === 'assistant');
  const toolMsgs = messages.filter(m => m.role === 'tool');

  const summaryHtml = `
    <div class="trace-summary" style="margin-bottom:14px;">
      <div class="trace-summary-item"><div class="trace-summary-label">Messages</div><div class="trace-summary-value">${messages.length}</div></div>
      <div class="trace-summary-item"><div class="trace-summary-label">Stages</div><div class="trace-summary-value">${stages.length}</div></div>
      <div class="trace-summary-item"><div class="trace-summary-label">System</div><div class="trace-summary-value">${systemMsgs.length}</div></div>
      <div class="trace-summary-item"><div class="trace-summary-label">User</div><div class="trace-summary-value">${userMsgs.length}</div></div>
      <div class="trace-summary-item"><div class="trace-summary-label">Assistant</div><div class="trace-summary-value">${assistantMsgs.length}</div></div>
      <div class="trace-summary-item"><div class="trace-summary-label">Tool</div><div class="trace-summary-value">${toolMsgs.length}</div></div>
    </div>
  `;

  // Build stage index lookup: message index -> stage name
  const msgStageMap = {};
  stages.forEach(s => {
    const range = s.message_range || {};
    const start = range.start || 0;
    const end = range.end || 0;
    for (let i = start; i < end; i++) {
      msgStageMap[i] = s.stage_name || '';
    }
  });

  // Render messages
  const roleColors = {
    system: '#7c3aed',
    user: '#2563eb',
    assistant: '#059669',
    tool: '#d97706',
  };

  const messagesHtml = messages.map((msg, idx) => {
    const role = msg.role || 'unknown';
    const color = roleColors[role] || '#666';
    const stageName = msgStageMap[idx] || '';
    const model = msg.model || '';
    const content = msg.content || '';
    const toolCalls = msg.tool_calls;
    const toolCallId = msg.tool_call_id || '';
    const bodyId = `dolphin-msg-${idx}`;
    const collapsed = content.length > 600;

    let metaParts = [`#${idx}`];
    if (stageName) metaParts.push(stageName);
    if (model) metaParts.push(model);
    if (toolCallId) metaParts.push(`tool_call_id: ${toolCallId}`);

    let toolCallsHtml = '';
    if (toolCalls && toolCalls.length > 0) {
      const tcSummary = toolCalls.map(tc => {
        const fn = tc.function || {};
        return `${fn.name || tc.name || '?'}(${(fn.arguments || '').substring(0, 100)}${(fn.arguments || '').length > 100 ? '...' : ''})`;
      }).join(', ');
      toolCallsHtml = `<div class="dolphin-tool-calls">tool_calls: ${escapeHtml(tcSummary)}</div>`;
    }

    return `
      <div class="trace-msg role-${escapeHtml(role)}">
        <div class="trace-msg-header">
          <div><span style="color:${color}; font-weight:600;">${escapeHtml(role)}</span></div>
          <div>${escapeHtml(metaParts.join(' | '))}</div>
        </div>
        <div id="${bodyId}" class="trace-msg-body ${collapsed ? 'collapsed' : ''}">${escapeHtml(content)}</div>
        ${collapsed ? `<button class="trace-expand-btn" onclick="toggleTraceExpand('${bodyId}', this)">展开</button>` : ''}
        ${toolCallsHtml}
      </div>
    `;
  }).join('');

  // Stages overview
  const stagesHtml = stages.length > 0 ? `
    <div class="trajectory-event" style="margin-bottom:12px;">
      <div class="trajectory-event-top">
        <div class="trajectory-event-type">Stages</div>
      </div>
      <div class="trajectory-event-meta">${stages.map(s => {
    const range = s.message_range || {};
    return `${escapeHtml(s.stage_name || '?')} [${range.start || 0}-${range.end || 0})`;
  }).join(' → ')}</div>
    </div>
  ` : '';

  container.innerHTML = summaryHtml + stagesHtml + messagesHtml;
}

function downloadTraceJSON(type) {
  if (!traceModalData) return;
  let payload = traceModalData;
  let filename = `${currentAgent || 'session'}_trace.json`;
  if (type === 'trace') {
    payload = {
      session_id: traceModalData.session_id,
      agent_name: traceModalData.agent_name,
      created_at: traceModalData.created_at,
      updated_at: traceModalData.updated_at,
      context_trace: traceModalData.context_trace || {},
    };
    filename = `${currentAgent || 'session'}_context_trace.json`;
  } else if (type === 'trajectory') {
    payload = {
      session_id: traceModalData.session_id,
      agent_name: traceModalData.agent_name,
      timeline: traceModalData.timeline || [],
    };
    filename = `${currentAgent || 'session'}_timeline.json`;
  } else if (type === 'dolphin') {
    payload = traceModalData.trajectory || {};
    filename = `${currentAgent || 'session'}_trajectory.json`;
  }

  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}


function toggleRawJson(nodeId) {
  const nodeElement = document.getElementById(nodeId);
  if (!nodeElement) return;
  const jsonBlock = nodeElement.querySelector('.trace-node-json');
  if (!jsonBlock) return;
  const isOpen = jsonBlock.classList.toggle('open');
  const btn = nodeElement.querySelector('.trace-raw-toggle');
  if (btn) btn.textContent = isOpen ? '隐藏原始数据 (JSON)' : '显示原始数据 (JSON)';
}

function renderNodeStructuredContent(node) {
  const sections = [];
  const stageType = (node.stage_type || '').toLowerCase();

  // 1. LLM Prompt/Messages
  if (stageType.includes('llm')) {
    if (node.messages && Array.isArray(node.messages)) {
      sections.push({
        label: 'LLM Messages',
        value: `<div class="trace-details" style="gap:8px;">${node.messages.map(m => `
          <div class="trace-detail-section" style="border-style: dashed; background: transparent;">
            <div class="trace-detail-label" style="background: ${m.role === 'system' ? '#f1f5f9' : (m.role === 'user' ? '#eff6ff' : '#ecfdf5')}; color: ${m.role === 'system' ? '#64748b' : (m.role === 'user' ? '#2563eb' : '#059669')}">
              ${escapeHtml(m.role || 'role')}
            </div>
            <div class="trace-detail-value" style="padding: 8px 12px; font-size: 12px; opacity: 0.9;"><pre>${escapeHtml(m.content || '')}</pre></div>
          </div>
        `).join('')}</div>`
      });
    } else if (node.prompt) {
      sections.push({ label: 'Prompt', value: `<pre>${escapeHtml(node.prompt)}</pre>` });
    }

    // 2. LLM Response
    const responseText = node.answer || node.response || node.content;
    if (responseText && typeof responseText === 'string') {
      sections.push({ label: 'LLM Response', value: `<pre style="color: #059669; font-weight: 500;">${escapeHtml(responseText)}</pre>` });
    }
  }

  // 3. Skill/Tool Input
  if (stageType.includes('skill') || stageType.includes('tool')) {
    let args = node.args || node.arguments || node.skill_args;
    if (args) {
      if (typeof args === 'object') args = JSON.stringify(args, null, 2);
      sections.push({ label: 'Tool Arguments', value: `<pre style="color: #2563eb;">${escapeHtml(args)}</pre>` });
    }

    // 4. Skill/Tool Output
    let output = node.output || node.result || node.skill_output || node.answer;
    if (output && !stageType.includes('llm')) {
      if (typeof output === 'object') output = JSON.stringify(output, null, 2);
      sections.push({ label: 'Tool Output', value: `<pre>${escapeHtml(output)}</pre>` });
    }
  }

  // 5. General Error
  const error = node.error || node.exception || (node.message && node.status === 'error' ? node.message : null);
  if (error) {
    sections.push({ label: 'Error Trace', value: `<div class="status-error"><pre style="color: #dc2626; background: #fff5f5; padding: 10px; border-radius: 6px;">${escapeHtml(error)}</pre></div>` });
  }

  // 6. Meta Data
  const metaFields = [];
  const handledKeys = ['name', 'type', 'status', 'stage_type', 'duration', 'children', 'model', 'tokens', 'tool_name', 'exit_code', 'messages', 'prompt', 'answer', 'response', 'content', 'args', 'arguments', 'skill_args', 'output', 'result', 'skill_output', 'error', 'exception'];

  Object.entries(node).forEach(([key, value]) => {
    if (!handledKeys.includes(key) && value !== null && value !== undefined && typeof value !== 'object') {
      metaFields.push({ key, value });
    }
  });

  if (metaFields.length > 0) {
    sections.push({
      label: 'Node Meta',
      value: `<div style="display:grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px;">
        ${metaFields.map(m => `
          <div style="background: #f8fafc; padding: 6px 10px; border-radius: 6px; border: 1px solid #f1f5f9;">
            <span style="font-weight:700; color:#64748b; font-size: 10px; text-transform: uppercase;">${escapeHtml(m.key)}</span>
            <div style="color:#1e293b; font-family: 'SF Mono', monospace; font-size: 12px; margin-top: 2px;">${escapeHtml(String(m.value))}</div>
          </div>
        `).join('')}
      </div>`
    });
  }

  if (sections.length === 0) return '';

  return `<div class="trace-details">
    ${sections.map(s => `
      <div class="trace-detail-section">
        <div class="trace-detail-label">${s.label}</div>
        <div class="trace-detail-value">${s.value}</div>
      </div>
    `).join('')}
  </div>`;
}

// Skills Settings
async function loadSkillsSettings() {
  const container = document.getElementById('skills-list');
  if (!container) return;
  try {
    const res = await fetch('/api/settings/skills');
    const data = await res.json();
    const skills = data.skills || [];
    if (skills.length === 0) {
      container.innerHTML = '<div class="settings-hint">没有已安装的技能</div>';
      return;
    }
    container.innerHTML = skills.map(skill => `
      <div class="skill-item">
        <div class="skill-info">
          <div class="skill-info-top">
            <span class="skill-name">${escapeHtml(skill.title)}</span>
            <span class="skill-id">${escapeHtml(skill.name)}</span>
          </div>
          ${skill.description ? `<div class="skill-desc">${escapeHtml(skill.description)}</div>` : ''}
        </div>
        <label class="skill-toggle">
          <input type="checkbox" ${skill.enabled ? 'checked' : ''} onchange="toggleSkill('${escapeHtml(skill.name)}', this.checked)" />
          <span class="skill-toggle-slider"></span>
        </label>
      </div>
    `).join('');
  } catch (e) {
    console.error('Failed to load skills settings:', e);
    container.innerHTML = '<div class="settings-hint" style="color:#dc3545;">加载失败</div>';
  }
}

async function toggleSkill(skillName, enabled) {
  try {
    const res = await fetch(`/api/settings/skills/${encodeURIComponent(skillName)}/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (e) {
    console.error('Failed to toggle skill:', e);
    // Reload to revert the toggle state
    loadSkillsSettings();
  }
}

// Telegram Settings
async function loadTelegramSettings() {
  try {
    const res = await fetch('/api/settings/telegram');
    const data = await res.json();

    document.getElementById('tg-enabled').checked = !!data.enabled;
    document.getElementById('tg-bot-token').value = data.bot_token_display || '';
    document.getElementById('tg-allowed-chat-ids').value = (data.allowed_chat_ids || []).join(', ');

    const agentSelect = document.getElementById('tg-default-agent');
    const agents = data.agents || [];
    agentSelect.innerHTML = '<option value="">-- 选择 Agent --</option>' +
      agents.map(a => `<option value="${escapeHtml(a)}">${escapeHtml(a)}</option>`).join('');
    if (data.default_agent) agentSelect.value = data.default_agent;

    document.getElementById('tg-save-status').textContent = '';
  } catch (e) {
    console.error('Failed to load telegram settings:', e);
  }
}

async function saveTelegramSettings() {
  const statusEl = document.getElementById('tg-save-status');
  statusEl.textContent = '保存中...';
  statusEl.className = 'settings-status';
  try {
    const res = await fetch('/api/settings/telegram', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled: document.getElementById('tg-enabled').checked,
        default_agent: document.getElementById('tg-default-agent').value,
        allowed_chat_ids: document.getElementById('tg-allowed-chat-ids').value,
      }),
    });
    const data = await res.json();
    statusEl.textContent = data.message || '已保存';
    statusEl.className = 'settings-status success';
  } catch (e) {
    statusEl.textContent = '保存失败: ' + e.message;
    statusEl.className = 'settings-status error';
  }
}

// 初始化
loadAgents();
