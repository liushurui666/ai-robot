const $ = (selector) => document.querySelector(selector);
const state = {
  conversations: [],
  uploads: [],
  selectedId: null,
  activeView: "conversations",
  uploadStatus: "",
  remoteSecure: true,
  mediaPermissionRequired: false,
  eventSource: null,
  refreshTimer: null,
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function initials(name) {
  const clean = String(name || "?").trim();
  if (!clean) return "?";
  const words = clean.split(/\s+/);
  return words.length > 1
    ? `${words[0][0]}${words[1][0]}`.toUpperCase()
    : clean.slice(0, 2).toUpperCase();
}

function parseTime(value) {
  if (!value) return null;
  let raw = String(value).trim().replace(" ", "T");
  if (!/[zZ]|[+-]\d\d:\d\d$/.test(raw)) raw += "Z";
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? null : date;
}

const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
});
const timeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false,
});
const fullFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});

function formatTime(value, full = false) {
  const date = parseTime(value);
  if (!date) return "时间未知";
  return full ? fullFormatter.format(date) : timeFormatter.format(date);
}

function dayKey(value) {
  const date = parseTime(value);
  return date ? dateFormatter.format(date) : "时间未知";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("unauthorized");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2400);
}

function showLogin() {
  $("#loginView").hidden = false;
  $("#appView").hidden = true;
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
  window.setTimeout(() => $("#password").focus(), 0);
}

async function showApp() {
  $("#loginView").hidden = true;
  $("#appView").hidden = false;
  await refreshAll(false);
  connectEvents();
}

async function login(event) {
  event.preventDefault();
  const button = $("#loginButton");
  const error = $("#loginError");
  button.disabled = true;
  button.textContent = "正在验证…";
  error.textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("#password").value }),
    });
    $("#password").value = "";
    await showApp();
  } catch (err) {
    error.textContent = err.message === "too_many_attempts"
      ? "尝试次数过多，请一分钟后再试。"
      : "密码不正确，请检查服务器配置。";
  } finally {
    button.disabled = false;
    button.textContent = "安全登录";
  }
}

async function logout() {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch (_) {}
  showLogin();
}

function updateMetrics(data) {
  $("#conversationMetric").textContent = data.conversations ?? 0;
  $("#messageMetric").textContent = data.human_messages ?? 0;
  $("#uploadMetric").textContent = data.uploads ?? 0;
  $("#failedMetric").textContent = data.failed_uploads ?? 0;
  const badge = $("#failedNavBadge");
  badge.hidden = !data.failed_uploads;
  badge.textContent = data.failed_uploads || 0;
  const live = $("#liveStatus");
  state.remoteSecure = data.remote?.secure_transport !== false;
  state.mediaPermissionRequired = data.remote?.media_permission_required === true;
  $("#mediaPermissionNotice").hidden = !state.mediaPermissionRequired;
  live.classList.remove("offline");
  if (data.remote?.enabled && data.remote.secure_transport === false) {
    live.classList.add("offline");
    live.querySelector("span:last-child").textContent = "远程 HTTP";
    live.title = "当前线上服务使用未加密 HTTP，建议后续改为 HTTPS";
  } else if (data.remote?.enabled && !data.remote.connected) {
    live.classList.add("offline");
    live.querySelector("span:last-child").textContent = "远程断开";
  } else {
    live.querySelector("span:last-child").textContent = "实时同步";
  }
}

function renderConversations() {
  const list = $("#conversationList");
  list.replaceChildren();
  $("#conversationCount").textContent = `${state.conversations.length} 个会话`;
  if (!state.conversations.length) {
    list.append(el("div", "empty-list", "没有找到匹配的会话"));
    return;
  }
  for (const item of state.conversations) {
    const button = el("button", `conversation-item${state.selectedId === item.session_id ? " active" : ""}`);
    button.type = "button";
    button.dataset.sessionId = item.session_id;

    const avatar = el("div", `avatar${item.peer_id.startsWith("oc_") ? " group" : ""}`, initials(item.display_name));
    const main = el("div", "conversation-main");
    const title = el("div", "conversation-title");
    title.append(el("strong", "", item.display_name));
    if (item.upload_count) title.append(el("span", "tool-pill", `${item.upload_count} 附件`));
    main.append(title, el("div", "conversation-preview", item.last_content || "暂无可见文本"));
    const meta = el("div", "conversation-meta", formatTime(item.latest_event_at, true));
    if (item.failed_uploads) meta.append(el("span", "warning-dot"));
    button.append(avatar, main, meta);
    button.addEventListener("click", () => selectConversation(item.session_id));
    list.append(button);
  }
}

function renderSummaries(summaries) {
  const notice = $("#historyNotice");
  const list = $("#summaryList");
  list.replaceChildren();
  notice.hidden = !summaries.length;
  for (const summary of summaries) {
    const item = el("div", "summary-item");
    item.append(el("span", "summary-time", formatTime(summary.timestamp, true)));
    item.append(document.createTextNode(summary.content || ""));
    list.append(item);
  }
}

function toolPayload(event) {
  if (event.tool_calls?.length) return event.tool_calls;
  const raw = event.raw || {};
  return {
    tool: event.name || raw.name || "tool",
    result: event.content || "",
    tool_call_id: raw.tool_call_id || null,
  };
}

function renderMessageAttachments(uploads) {
  if (!uploads.length) return null;
  const container = el("div", "message-attachments");
  for (const upload of uploads) {
    if (upload.content_url && upload.kind === "image") {
      const link = el("a", "message-attachment image");
      link.href = upload.content_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = "查看原图";
      const image = el("img");
      image.src = upload.content_url;
      image.alt = upload.filename || "聊天图片";
      image.loading = "lazy";
      link.append(image, el("span", "attachment-action", "查看原图"));
      container.append(link);
      continue;
    }
    if (upload.content_url) {
      const link = el("a", "message-attachment file");
      link.href = upload.content_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.append(el("span", "file-mark", "FILE"), el("span", "file-name", upload.filename || "下载附件"));
      container.append(link);
      continue;
    }
    const failed = el("div", "message-attachment failed");
    const pendingText = state.mediaPermissionRequired
      ? "飞书只读权限尚未生效"
      : upload.kind === "image" ? "原图等待补拉" : "文件等待补拉";
    failed.append(el("span", "file-mark", "!"), el("span", "file-name", pendingText));
    container.append(failed);
  }
  return container;
}

function renderTimeline(events, uploads = []) {
  const timeline = $("#timeline");
  timeline.replaceChildren();
  const uploadsByEvent = new Map();
  for (const upload of uploads) {
    if (!upload.event_id) continue;
    if (!uploadsByEvent.has(upload.event_id)) uploadsByEvent.set(upload.event_id, []);
    uploadsByEvent.get(upload.event_id).push(upload);
  }
  let lastDay = null;
  for (const event of events) {
    const currentDay = dayKey(event.timestamp || event.first_seen_at);
    if (currentDay !== lastDay) {
      timeline.append(el("div", "day-divider", currentDay));
      lastDay = currentDay;
    }
    const isTool = event.role === "tool" || (event.tool_calls && event.tool_calls.length && !event.content);
    if (isTool) {
      const row = el("div", "message-row tool");
      const details = el("details", "tool-card");
      const toolName = event.name || event.tool_calls?.[0]?.function?.name || "工具调用";
      details.append(el("summary", "", `内部执行 · ${toolName}`));
      details.append(el("pre", "", JSON.stringify(toolPayload(event), null, 2)));
      row.append(details);
      timeline.append(row);
      continue;
    }
    const kind = event.is_cron ? "cron" : event.role === "user" ? "user" : "assistant";
    const row = el("div", `message-row ${kind}`);
    const avatarText = event.is_cron ? "时" : event.role === "user" ? initials($("#chatName").textContent) : "R";
    const avatar = el("div", `avatar${event.role === "user" ? "" : " group"}`, avatarText);
    const body = el("div", "message-body");
    const label = el("div", "message-label", event.is_cron ? "系统定时触发" : event.role === "user" ? $("#chatName").textContent : "提醒助手");
    if (event.is_cron) label.append(el("span", "cron-pill", "CRON"));
    const eventUploads = uploadsByEvent.get(event.event_id) || [];
    const cleanedContent = eventUploads.length
      ? String(event.content || "").replace(/\n?\[(image|file|audio|media):[^\]]+\]/gi, "").trim()
      : event.content;
    const content = cleanedContent || (eventUploads.length ? "附件" : event.tool_calls?.length ? "正在执行内部工具" : "空消息");
    body.append(label, el("div", "bubble", content));
    const attachments = renderMessageAttachments(eventUploads);
    if (attachments) body.append(attachments);
    body.append(el("div", "message-time", formatTime(event.timestamp || event.first_seen_at)));
    row.append(avatar, body);
    timeline.append(row);
  }
  if (!events.length) timeline.append(el("div", "empty-list", "这个会话还没有可见消息"));
  requestAnimationFrame(() => { timeline.scrollTop = timeline.scrollHeight; });
}

async function selectConversation(sessionId, { silent = false } = {}) {
  state.selectedId = sessionId;
  renderConversations();
  try {
    const detail = await api(`/api/conversations/${encodeURIComponent(sessionId)}?limit=5000`);
    $("#emptyConversation").hidden = true;
    $("#chatContent").hidden = false;
    $("#chatName").textContent = detail.display_name;
    $("#chatPeer").textContent = detail.peer_id;
    $("#chatAvatar").textContent = initials(detail.display_name);
    $("#chatAvatar").classList.toggle("group", detail.peer_id.startsWith("oc_"));
    $("#chatMessageCount").textContent = `${detail.event_total} 条完整记录`;
    $("#chatUpdated").textContent = `最后活跃 ${formatTime(detail.updated_at || detail.last_seen_at, true)}`;
    renderSummaries(detail.summaries || []);
    renderTimeline(detail.events || [], detail.uploads || []);
    $("#conversationPane").classList.remove("open");
    if (!silent && detail.truncated) showToast("记录较多，当前显示最近 5000 条");
  } catch (err) {
    if (err.message !== "unauthorized") showToast("会话载入失败");
  }
}

function renderUploads() {
  const list = $("#uploadList");
  list.replaceChildren();
  const filtered = state.uploads.filter(item => !state.uploadStatus || item.status === state.uploadStatus);
  if (!filtered.length) {
    list.append(el("div", "empty-list", "当前筛选下没有上传记录"));
    return;
  }
  for (const item of filtered) {
    const card = el("article", "upload-card");
    const preview = el(item.content_url ? "a" : "div", `upload-preview${item.status === "available" ? "" : " failed"}`);
    if (item.content_url) {
      preview.href = item.content_url;
      preview.target = "_blank";
      preview.rel = "noopener noreferrer";
      preview.title = item.kind === "image" ? "查看原图" : "下载文件";
    }
    if (item.content_url && item.kind === "image") {
      const image = el("img");
      image.src = item.content_url;
      image.alt = item.filename || "图片附件";
      image.loading = "lazy";
      preview.append(image);
    } else {
      preview.textContent = item.status === "available" ? "⇧" : "!";
    }
    const info = el("div", "upload-info");
    info.append(el("strong", "upload-name", item.filename || `${item.kind} 上传`));
    const first = el("div", "upload-line");
    first.append(el("span", "", item.display_name));
    const statusLabels = { available: "可查看", failed: "下载失败", missing: "文件缺失" };
    first.append(el("span", `status-badge status-${item.status}`, statusLabels[item.status] || item.status));
    const second = el("div", "upload-line");
    second.append(el("span", "", formatTime(item.timestamp || item.first_seen_at, true)));
    if (item.content_url) {
      const link = el("a", "upload-action", item.kind === "image" ? "查看原图" : "下载文件");
      link.href = item.content_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      second.append(link);
    } else if (item.error) {
      second.append(el("span", "", item.error));
    }
    info.append(first, second);
    card.append(preview, info);
    list.append(card);
  }
}

async function loadConversations() {
  const query = $("#conversationSearch").value.trim();
  const data = await api(`/api/conversations?q=${encodeURIComponent(query)}`);
  state.conversations = data.conversations || [];
  renderConversations();
}

async function loadUploads() {
  const data = await api("/api/uploads");
  state.uploads = data.uploads || [];
  renderUploads();
}

async function refreshAll(showMessage = true) {
  try {
    const [metrics] = await Promise.all([
      api("/api/overview"),
      loadConversations(),
      loadUploads(),
    ]);
    updateMetrics(metrics);
    if (state.selectedId) await selectConversation(state.selectedId, { silent: true });
    if (showMessage) showToast("数据已刷新");
  } catch (err) {
    if (err.message !== "unauthorized") showToast("刷新失败，请检查服务状态");
  }
}

function scheduleRefresh() {
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = window.setTimeout(() => refreshAll(false), 250);
}

function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  const status = $("#liveStatus");
  const source = new EventSource("/api/events", { withCredentials: true });
  state.eventSource = source;
  source.addEventListener("ready", () => {
    status.classList.toggle("offline", !state.remoteSecure);
    status.querySelector("span:last-child").textContent = state.remoteSecure ? "实时同步" : "远程 HTTP";
  });
  source.addEventListener("changed", scheduleRefresh);
  source.onerror = () => {
    status.classList.add("offline");
    status.querySelector("span:last-child").textContent = "正在重连";
  };
}

function switchView(view) {
  state.activeView = view;
  document.querySelectorAll(".nav-item").forEach(button => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  $("#conversationsView").hidden = view !== "conversations";
  $("#uploadsView").hidden = view !== "uploads";
  $("#pageTitle").textContent = view === "conversations" ? "会话记录" : "上传记录";
  $("#mobileConversationButton").hidden = view !== "conversations";
}

function bindEvents() {
  $("#loginForm").addEventListener("submit", login);
  $("#logoutButton").addEventListener("click", logout);
  $("#refreshButton").addEventListener("click", () => refreshAll(true));
  $("#togglePassword").addEventListener("click", () => {
    const input = $("#password");
    input.type = input.type === "password" ? "text" : "password";
  });
  document.querySelectorAll(".nav-item").forEach(button => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  let searchTimer;
  $("#conversationSearch").addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(loadConversations, 220);
  });
  $("#toggleSummaries").addEventListener("click", () => {
    const list = $("#summaryList");
    list.hidden = !list.hidden;
    $("#toggleSummaries").textContent = list.hidden ? "展开摘要" : "收起摘要";
  });
  document.querySelectorAll(".filter-chip").forEach(button => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".filter-chip").forEach(item => item.classList.remove("active"));
      button.classList.add("active");
      state.uploadStatus = button.dataset.uploadStatus;
      renderUploads();
    });
  });
  $("#openConversationPane").addEventListener("click", () => $("#conversationPane").classList.add("open"));
  $("#mobileConversationButton").addEventListener("click", () => $("#conversationPane").classList.add("open"));
  $("#closeConversationPane").addEventListener("click", () => $("#conversationPane").classList.remove("open"));
}

async function initialize() {
  bindEvents();
  try {
    const auth = await api("/api/auth");
    if (auth.authenticated) await showApp();
    else showLogin();
  } catch (_) {
    showLogin();
  }
}

initialize();
