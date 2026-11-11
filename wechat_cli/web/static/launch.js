(() => {
  "use strict";

  const PREFERRED_CAMPAIGN_NAME = "OPC大课发售";
  const API_PREFIX = "/api/launch";
  const INVITE_BAR_COLORS = [
    "#E35764",
    "#E7C16E",
    "#65B8B2",
    "#C08DD6",
    "#53659D",
    "#F2894C",
    "#8FCEA0",
    "#669FEA",
    "#D99AB0",
  ];
  const state = {
    campaigns: [],
    activeCampaignId: "",
    activeCampaign: null,
    currentScreen: "dashboard",
    dashboard: null,
    rooms: [],
    campaignRooms: [],
    campaignRoomIds: new Set(),
    traceItems: [],
    currentMemberDetail: null,
    currentDetailTab: "invite",
    currentRoomMembers: null,
    healthTimer: null,
    toastTimer: null,
  };

  const byId = (id) => document.getElementById(id);
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function avatarProxyUrl(url) {
    const value = String(url || "").trim();
    return value ? `/api/avatar?url=${encodeURIComponent(value)}` : "";
  }

  function avatarFallbackGlyph(label, { group = false } = {}) {
    const value = String(label || "").trim();
    for (const char of value) {
      if (/\p{L}|\p{N}/u.test(char)) return char.toLocaleUpperCase("zh-CN");
    }
    for (const char of value) {
      if (!/\s/u.test(char)) return char;
    }
    return group ? "群" : "人";
  }

  function avatarMarkup(url, label, className = "person-avatar", { group = false } = {}) {
    const src = avatarProxyUrl(url);
    const fallback = escapeHtml(avatarFallbackGlyph(label, { group }));
    return `<span class="${escapeHtml(className)} avatar-shell" title="${escapeHtml(label || "头像")}">
      <span class="avatar-fallback-letter${group ? " group-fallback-letter" : ""}" aria-hidden="true">${fallback}</span>
      ${src ? `<img src="${escapeHtml(src)}" alt="${escapeHtml(label || "头像")}" loading="lazy" data-avatar-image>` : ""}
    </span>`;
  }

  function setAvatarContainer(id, url, label, { group = false } = {}) {
    const node = byId(id);
    if (!node) return;
    const src = avatarProxyUrl(url);
    const fallback = escapeHtml(avatarFallbackGlyph(label, { group }));
    node.innerHTML = `<span class="avatar-fallback-letter${group ? " group-fallback-letter" : ""}" aria-hidden="true">${fallback}</span>${src ? `<img src="${escapeHtml(src)}" alt="${escapeHtml(label || "头像")}" loading="lazy" data-avatar-image>` : ""}`;
  }

  function identityMarkup(url, label, { group = false, strong = false } = {}) {
    const text = strong ? `<strong>${escapeHtml(label || "待确认")}</strong>` : `<span>${escapeHtml(label || "待确认")}</span>`;
    return `<span class="identity-with-avatar">${avatarMarkup(url, label, group ? "group-avatar" : "person-avatar", { group })}${text}</span>`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatNumber(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric.toLocaleString("zh-CN") : "—";
  }

  function formatTime(timestamp, { includeDate = false } = {}) {
    const numeric = Number(timestamp);
    if (!Number.isFinite(numeric) || numeric <= 0) return "暂无事件";
    const date = new Date(numeric * 1000);
    if (Number.isNaN(date.getTime())) return "暂无事件";
    const options = includeDate
      ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }
      : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
    return new Intl.DateTimeFormat("zh-CN", options).format(date);
  }

  function dateToStartTimestamp(value) {
    if (!value) return "";
    const date = new Date(`${value}T00:00:00`);
    return Number.isNaN(date.getTime()) ? "" : Math.floor(date.getTime() / 1000);
  }

  function dateToEndTimestamp(value) {
    if (!value) return "";
    const date = new Date(`${value}T23:59:59`);
    return Number.isNaN(date.getTime()) ? "" : Math.floor(date.getTime() / 1000);
  }

  function queryString(params) {
    const search = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== "" && value !== null && value !== undefined) {
        search.set(key, String(value));
      }
    });
    const encoded = search.toString();
    return encoded ? `?${encoded}` : "";
  }

  async function apiGet(path, params = {}) {
    const response = await fetch(`${path}${queryString(params)}`, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      const error = payload.error || {};
      throw new Error(error.message || `请求失败（HTTP ${response.status}）`);
    }
    return payload.data;
  }

  async function apiPost(path, body = {}) {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      const error = payload.error || {};
      throw new Error(error.message || `请求失败（HTTP ${response.status}）`);
    }
    return payload.data;
  }

  function toast(message, kind = "success") {
    const node = byId("toast");
    if (!node) return;
    clearTimeout(state.toastTimer);
    node.textContent = message;
    node.classList.toggle("error", kind === "error");
    node.hidden = false;
    state.toastTimer = setTimeout(() => {
      node.hidden = true;
    }, 3600);
  }

  function currentCampaign() {
    return state.campaigns.find((item) => item.campaign_id === state.activeCampaignId) || null;
  }

  function activeCampaignRequired() {
    if (!state.activeCampaignId) {
      toast("当前没有可用活动，请先创建 Campaign。", "error");
      return false;
    }
    return true;
  }

  function showScreen(screen, { load = true } = {}) {
    state.currentScreen = screen;
    all(".screen").forEach((node) => node.classList.toggle("active", node.id === screen));
    all("[data-screen]").forEach((node) => node.classList.toggle("active", node.dataset.screen === screen));
    if (window.innerWidth > 760) window.scrollTo({ top: 0, behavior: "smooth" });
    if (load) loadCurrentScreen().catch((error) => toast(error.message, "error"));
  }

  async function openTrace({ roomId = "", query = "" } = {}) {
    showScreen("trace", { load: false });
    await ensureTraceRoomOptions();
    byId("trace-room-filter").value = roomId && state.campaignRoomIds.has(roomId) ? roomId : "";
    byId("trace-query").value = query;
    await loadTrace();
  }

  async function loadCampaigns({ preferCampaignId = "" } = {}) {
    const data = await apiGet(`${API_PREFIX}/campaigns`);
    state.campaigns = Array.isArray(data.campaigns) ? data.campaigns : [];
    const select = byId("campaign-select");
    select.replaceChildren();
    if (!state.campaigns.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "暂无活动";
      select.appendChild(option);
      state.activeCampaignId = "";
      state.activeCampaign = null;
      return;
    }

    for (const campaign of state.campaigns) {
      const option = document.createElement("option");
      option.value = campaign.campaign_id;
      option.textContent = `${campaign.name}${campaign.ended_at ? " · 已结束" : ""}`;
      select.appendChild(option);
    }

    const existing = state.campaigns.find((item) => item.campaign_id === preferCampaignId)
      || state.campaigns.find((item) => item.campaign_id === state.activeCampaignId)
      || state.campaigns.find((item) => item.name === PREFERRED_CAMPAIGN_NAME && !item.ended_at)
      || state.campaigns.find((item) => !item.ended_at)
      || state.campaigns[0];
    state.activeCampaignId = existing.campaign_id;
    state.activeCampaign = existing;
    select.value = existing.campaign_id;
    await loadCampaignRoomSet();
  }

  async function loadCampaignRoomSet() {
    state.campaignRooms = [];
    state.campaignRoomIds = new Set();
    if (!state.activeCampaignId) return;
    const data = await apiGet(`${API_PREFIX}/rooms`, { campaign_id: state.activeCampaignId });
    state.campaignRooms = Array.isArray(data.rooms) ? data.rooms : [];
    state.campaignRoomIds = new Set(state.campaignRooms.map((room) => room.room_id));
  }

  async function switchCampaign(campaignId) {
    if (!campaignId || campaignId === state.activeCampaignId) return;
    state.activeCampaignId = campaignId;
    state.activeCampaign = currentCampaign();
    await loadCampaignRoomSet();
    await Promise.all([loadHealth(), loadCurrentScreen()]);
    toast(`已切换到：${state.activeCampaign?.name || "活动"}`);
  }

  function renderHealth(health) {
    const button = byId("health-button");
    const label = byId("health-label");
    const metric = byId("metric-health");
    const detail = byId("metric-health-detail");
    button.classList.remove("health-running", "health-scanning", "health-error", "health-stale", "health-starting");

    const stale = Boolean(health?.stale);
    const status = health?.status || "starting";
    let stateClass = "health-starting";
    let text = "监听节点 · 检查中";
    let metricText = "检查中";
    if (status === "scanning") {
      stateClass = "health-scanning";
      text = "本机监听扫描中";
      metricText = "扫描中";
    } else if (status === "running" && !stale) {
      stateClass = "health-running";
      const age = health.last_success_at ? Math.max(0, Math.floor(Date.now() / 1000 - health.last_success_at)) : null;
      text = `本机监听正常${age === null ? "" : ` · ${age}s`}`;
      metricText = "正常";
    } else if (stale) {
      stateClass = "health-stale";
      text = "监听数据滞后";
      metricText = "滞后";
    } else if (status === "error") {
      stateClass = "health-error";
      text = "监听异常";
      metricText = "异常";
    }
    button.classList.add(stateClass);
    label.textContent = text;
    metric.textContent = metricText;
    metric.classList.toggle("metric-green", status === "running" && !stale);
    metric.classList.toggle("metric-ochre", stale || status === "error");
    const last = health?.last_success_at ? `最后成功 ${formatTime(health.last_success_at)}` : "尚无成功扫描";
    const progress = status === "scanning" ? `${last} · 本轮扫描进行中` : last;
    detail.textContent = health?.error_message ? `${progress} · ${health.error_message}` : progress;
    button.dataset.healthMessage = health?.error_message || `${progress}${health?.last_event_at ? ` · 最后事件 ${formatTime(health.last_event_at)}` : ""}`;
  }

  async function loadHealth() {
    try {
      const health = await apiGet(`${API_PREFIX}/health`);
      renderHealth(health);
      return health;
    } catch (error) {
      renderHealth({ status: "error", stale: true, error_message: error.message });
      throw error;
    }
  }

  async function loadDashboard() {
    if (!activeCampaignRequired()) {
      renderDashboardEmpty();
      return;
    }
    const dashboard = await apiGet(`${API_PREFIX}/dashboard`, {
      campaign_id: state.activeCampaignId,
      invite_window: byId("invite-window").value || "campaign",
    });
    state.dashboard = dashboard;
    const campaign = currentCampaign();
    byId("dashboard-subtitle").textContent = `${campaign?.name || "当前活动"} · 群规模、入群节奏与邀请增长一屏掌握。`;
    byId("metric-room-count").textContent = formatNumber(dashboard.room_count);
    byId("metric-members").textContent = formatNumber(dashboard.current_members);
    byId("metric-joined").textContent = `+${formatNumber(dashboard.today_joined ?? 0)}`;
    const left = dashboard.today_left || { available: false, value: null };
    byId("metric-left").textContent = left.available ? formatNumber(left.value) : "暂不可用";
    byId("metric-left-detail").textContent = left.available ? "来自可靠退群事实" : "仅在可靠识别后显示";
    renderHealth(dashboard.watcher || {});
    renderInviteBars(dashboard.invite_ranking || [], dashboard.invite_window || "campaign");
    renderInviteRanking(dashboard.invite_ranking || [], dashboard.invite_window || "campaign");
  }

  function renderDashboardEmpty() {
    byId("metric-room-count").textContent = "0";
    byId("metric-members").textContent = "0";
    byId("metric-joined").textContent = "+0";
    byId("invite-bars").innerHTML = '<div class="chart-empty">请先创建活动并加入微信群。</div>';
    byId("invite-ranking").innerHTML = '<div class="panel-empty">暂无邀请排行</div>';
  }

  function renderInviteBars(items, inviteWindow = "campaign") {
    const root = byId("invite-bars");
    const scopeLabel = inviteWindowLabel(inviteWindow);
    const scope = byId("invite-bars-scope-label");
    if (scope) scope.textContent = `${scopeLabel} · 按邀请人数从高到低`;
    const ranking = [...(items || [])].sort((a, b) => (Number(b.count) || 0) - (Number(a.count) || 0) || String(a.name || "").localeCompare(String(b.name || ""), "zh-CN"));
    root.replaceChildren();
    if (!ranking.length) {
      root.innerHTML = `<div class="chart-empty">${inviteWindow === "today" ? "今日暂无邀请数据" : inviteWindow === "7d" ? "近 7 天暂无邀请数据" : "当前活动暂无邀请数据"}</div>`;
      return;
    }
    const max = Math.max(1, ...ranking.map((item) => Number(item.count) || 0));
    for (const [index, item] of ranking.entries()) {
      const count = Number(item.count) || 0;
      const barColor = INVITE_BAR_COLORS[index % INVITE_BAR_COLORS.length];
      const row = document.createElement("div");
      row.className = "invite-bar-row";
      row.innerHTML = `
        ${avatarMarkup(item.avatar_url, item.name, "person-avatar")}
        <span class="invite-bar-name" title="${escapeHtml(item.name || "待确认")}">${escapeHtml(item.name || "待确认")}</span>
        <span class="invite-bar-track"><span class="invite-bar-fill" style="--bar-color:${barColor};width:${Math.max(2, Math.round((count / max) * 100))}%"></span></span>
        <span class="invite-bar-count">${formatNumber(count)} 人</span>`;
      root.appendChild(row);
    }
  }

  function inviteWindowLabel(inviteWindow) {
    if (inviteWindow === "today") return "今日";
    if (inviteWindow === "7d") return "近 7 天";
    return "活动累计";
  }

  function renderInviteRanking(items, inviteWindow = "campaign") {
    const root = byId("invite-ranking");
    const scopeLabel = inviteWindowLabel(inviteWindow);
    const scope = byId("ranking-scope-label");
    if (scope) scope.textContent = `${scopeLabel} · 点击成员可继续查看溯源详情`;
    root.replaceChildren();
    const ranking = [...(items || [])].sort((a, b) => (Number(b.count) || 0) - (Number(a.count) || 0) || String(a.name || "").localeCompare(String(b.name || ""), "zh-CN"));
    if (!ranking.length) {
      const emptyText = inviteWindow === "today"
        ? "今日暂无可归因邀请数据"
        : inviteWindow === "7d"
          ? "近 7 天暂无可归因邀请数据"
          : "当前活动暂无可归因邀请数据";
      root.innerHTML = `<div class="panel-empty">${emptyText}</div>`;
      return;
    }
    ranking.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ranking-row";
      button.dataset.traceQuery = item.name || "";
      button.innerHTML = `
        ${avatarMarkup(item.avatar_url, item.name, "rank-avatar")}
        <span class="rank-person"><strong>${escapeHtml(item.name || "待确认")}</strong><small>第 ${index + 1} 名 · 点击进入溯源</small></span>
        <span class="rank-count">${formatNumber(item.count)} 人</span>`;
      root.appendChild(button);
    });
  }

  async function loadGroups() {
    const range = byId("group-range").value;
    const params = {
      query: byId("group-query").value.trim(),
      health: byId("group-health-filter").value,
    };
    if (range === "campaign") {
      if (!activeCampaignRequired()) return;
      params.campaign_id = state.activeCampaignId;
    }
    const data = await apiGet(`${API_PREFIX}/rooms`, params);
    state.rooms = Array.isArray(data.rooms) ? data.rooms : [];
    if (range === "campaign") {
      state.campaignRooms = state.rooms;
      state.campaignRoomIds = new Set(state.rooms.map((room) => room.room_id));
    }
    renderGroups();
  }

  function roomHealth(room) {
    const status = room.health_status || "unknown";
    if (status === "running") return { label: "监听正常", className: "status-badge" };
    if (status === "error") return { label: "监听异常", className: "status-badge warn" };
    return { label: "等待扫描", className: "status-badge warn" };
  }

  function archiveStatus(room) {
    if (!room.archiving_enabled) return { label: "长期溯源已停止", className: "status-badge warn" };
    if (room.archive_projection_state === "ready" && room.archive_current_source === "archive") {
      return { label: "长期溯源已就绪", className: "status-badge" };
    }
    if (room.archive_projection_state === "refresh_error") {
      return { label: "长期溯源刷新失败", className: "status-badge warn" };
    }
    return { label: "长期溯源同步中", className: "status-badge warn" };
  }

  function renderGroups() {
    const root = byId("group-list");
    root.replaceChildren();
    if (!state.rooms.length) {
      root.innerHTML = '<div class="loading-block">没有符合当前筛选条件的群聊。</div>';
      return;
    }
    state.rooms.forEach((room) => {
      const health = roomHealth(room);
      const archive = archiveStatus(room);
      const inCampaign = state.campaignRoomIds.has(room.room_id);
      const article = document.createElement("article");
      article.className = "group-card";
      article.dataset.roomId = room.room_id;
      const leftValue = room.today_left?.available ? formatNumber(room.today_left.value) : "—";
      article.innerHTML = `
        <div class="group-identity">
          ${avatarMarkup(room.avatar_url, room.display_name || room.room_id, "group-avatar", { group: true })}
          <div class="group-identity-text">
            <h2 class="group-name" title="${escapeHtml(room.display_name || room.room_id)}">${escapeHtml(room.display_name || room.room_id)}</h2>
            <p class="group-meta">最后事件 ${escapeHtml(formatTime(room.last_event_at))} · <span class="${health.className}">${health.label}</span> · <span class="${archive.className}">${archive.label}</span>${room.today_left?.available ? ` · 今日退群 ${leftValue}` : ""}</p>
          </div>
        </div>
        <div class="group-stats">
          <div class="group-stat"><strong>${formatNumber(room.member_count ?? 0)}</strong><span>当前成员</span></div>
          <div class="group-stat"><strong class="metric-green">+${formatNumber(room.today_joined ?? 0)}</strong><span>今日新增</span></div>
          <div class="group-stat"><strong>${formatNumber(room.invite_event_count ?? 0)}</strong><span>邀请事件</span></div>
        </div>
        <div class="group-actions">
          <button class="button secondary" type="button" data-room-action="trace">邀请溯源</button>
          <button class="button secondary" type="button" data-room-action="members" ${inCampaign ? "" : "disabled"}>查看成员</button>
          <button class="button secondary wide-action" type="button" data-room-action="archive-refresh" ${room.archiving_enabled ? "" : "disabled"}>刷新群关系</button>
          ${room.archiving_enabled
            ? '<button class="button remove-action" type="button" data-room-action="archive-stop">停止长期溯源</button>'
            : '<button class="button primary" type="button" data-room-action="archive-start">开始长期溯源</button>'}
          ${inCampaign
            ? '<button class="button remove-action" type="button" data-room-action="remove">从活动移出</button>'
            : '<button class="button primary" type="button" data-room-action="add">加入当前活动</button>'}
        </div>`;
      root.appendChild(article);
    });
  }

  async function rescanAndReload() {
    const data = await apiPost(`${API_PREFIX}/rescan`, {});
    toast(`扫描完成：${formatNumber(data.inserted_events || 0)} 个新事件`);
    await Promise.all([loadHealth(), loadCampaignRoomSet()]);
    await loadCurrentScreen();
  }

  async function archiveAction(roomId, action) {
    const data = await apiPost(`${API_PREFIX}/archive/${action}`, { room_id: roomId });
    if (action === "refresh") toast("群关系已刷新");
    if (action === "start") toast("已开始长期溯源");
    if (action === "stop") toast("已停止后续归档；历史记录仍保留");
    await loadGroups();
    return data;
  }

  async function openRoomMembers(roomId) {
    if (!activeCampaignRequired()) return;
    const data = await apiGet(`${API_PREFIX}/room-members`, {
      campaign_id: state.activeCampaignId,
      room_id: roomId,
    });
    state.currentRoomMembers = data;
    const roomName = data.room?.display_name || data.room?.room_id || "群成员";
    byId("members-dialog-title").textContent = roomName;
    setAvatarContainer("members-dialog-avatar", data.room?.avatar_url, roomName, { group: true });
    const members = Array.isArray(data.members) ? data.members : [];
    byId("members-dialog-count").textContent = `${formatNumber(members.length)} 人`;
    const root = byId("members-dialog-list");
    root.replaceChildren();
    if (!members.length) {
      root.innerHTML = '<div class="loading-block">当前没有可展示成员数据。</div>';
    } else {
      for (const member of members) {
        const username = member.username || member.user_name || member.wxid || "";
        const name = member.display_name || member.remark || member.nick_name || member.nickname || username || "未知成员";
        const row = document.createElement("div");
        row.className = "member-row";
        row.innerHTML = `${avatarMarkup(member.avatar_url, name, "person-avatar")}<span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(username || "无可用账号标识")}</small></span>`;
        root.appendChild(row);
      }
    }
    byId("members-dialog").showModal();
  }

  async function exportAllMembers() {
    if (!activeCampaignRequired()) return;
    await loadCampaignRoomSet();
    if (!state.campaignRooms.length) {
      toast("当前活动没有群聊可导出。", "error");
      return;
    }
    const rows = [["群聊", "成员名称", "成员标识"]];
    for (const room of state.campaignRooms) {
      const data = await apiGet(`${API_PREFIX}/room-members`, {
        campaign_id: state.activeCampaignId,
        room_id: room.room_id,
      });
      for (const member of data.members || []) {
        const username = member.username || member.user_name || member.wxid || "";
        const name = member.display_name || member.remark || member.nick_name || member.nickname || username || "";
        rows.push([room.display_name || room.room_id, name, username]);
      }
    }
    downloadCsv(`${safeFilename(currentCampaign()?.name || "活动")}-群成员.csv`, rows);
  }

  function exportCurrentRoomMembers() {
    const data = state.currentRoomMembers;
    if (!data) return;
    const rows = [["群聊", "成员名称", "成员标识"]];
    for (const member of data.members || []) {
      const username = member.username || member.user_name || member.wxid || "";
      const name = member.display_name || member.remark || member.nick_name || member.nickname || username || "";
      rows.push([data.room?.display_name || data.room?.room_id || "", name, username]);
    }
    downloadCsv(`${safeFilename(data.room?.display_name || "群聊")}-成员.csv`, rows);
  }

  async function loadTrace() {
    if (!activeCampaignRequired()) {
      renderTrace([]);
      return;
    }
    await ensureTraceRoomOptions();
    const params = {
      campaign_id: state.activeCampaignId,
      room_id: byId("trace-room-filter").value,
      query: byId("trace-query").value.trim(),
      method: byId("trace-method").value,
      start_ts: dateToStartTimestamp(byId("trace-start").value),
      end_ts: dateToEndTimestamp(byId("trace-end").value),
    };
    const data = await apiGet(`${API_PREFIX}/trace`, params);
    state.traceItems = Array.isArray(data.items) ? data.items : [];
    renderTrace(state.traceItems);
  }

  async function ensureTraceRoomOptions() {
    if (!state.campaignRooms.length && state.activeCampaignId) await loadCampaignRoomSet();
    const select = byId("trace-room-filter");
    const previous = select.value;
    select.replaceChildren();
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "全部活动群";
    select.appendChild(allOption);
    for (const room of state.campaignRooms) {
      const option = document.createElement("option");
      option.value = room.room_id;
      option.textContent = room.display_name || room.room_id;
      select.appendChild(option);
    }
    if (previous && state.campaignRoomIds.has(previous)) select.value = previous;
  }

  function methodLabel(method) {
    if (method === "direct") return "直接邀请";
    if (method === "qr") return "二维码";
    if (method === "unattributed_qr") return "二维码 · 待归因";
    return method || "待确认";
  }

  function renderTrace(items) {
    const tbody = byId("trace-table-body");
    tbody.replaceChildren();
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">当前筛选条件下暂无溯源数据。</td></tr>';
      return;
    }
    for (const item of items) {
      const tr = document.createElement("tr");
      tr.dataset.roomId = item.room_id || "";
      tr.dataset.memberKey = item.member_key || item.member_name || "";
      tr.dataset.memberName = item.member_name || item.member_key || "成员";
      tr.dataset.episodeId = item.episode_id || "";
      tr.dataset.inviterKey = item.inviter_key || "";
      const cells = [
        ["成员", identityMarkup(item.member_avatar_url, item.member_name || "待确认", { strong: true })],
        ["所在群", identityMarkup(item.room_avatar_url, item.room_name || item.room_id || "", { group: true })],
        ["入群时间", escapeHtml(formatTime(item.event_time, { includeDate: true }))],
        ["邀请人", identityMarkup(item.inviter_avatar_url, item.inviter_name || "待确认")],
        ["方式", `<span class="method-badge ${escapeHtml(item.method || "")}">${escapeHtml(methodLabel(item.method))}</span>`],
        ["邀请人数", formatNumber(item.invite_count || 0)],
        ["状态", `<span class="status-badge warn">${escapeHtml(item.status || "状态待确认")}</span>${item.episode_id ? ' <button class="button secondary" type="button" data-manual-confirm>人工确认</button>' : ""}`],
      ];
      for (const [label, htmlValue] of cells) {
        const td = document.createElement("td");
        td.dataset.label = label;
        td.innerHTML = htmlValue;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  async function manualConfirmTraceRow(row) {
    const roomId = row.dataset.roomId || "";
    const episodeId = row.dataset.episodeId || "";
    if (!roomId || !episodeId) throw new Error("当前关系没有可人工确认的归档 Episode。");
    const currentInviter = row.dataset.inviterKey || "";
    const inviter = window.prompt(
      "请输入确认后的邀请人微信稳定标识（留空表示确认无法归因）：",
      currentInviter,
    );
    if (inviter === null) return;
    const reason = window.prompt("请输入本次人工确认依据：", "人工核验");
    if (reason === null || !reason.trim()) throw new Error("人工确认必须填写依据。");
    await apiPost(`${API_PREFIX}/archive/manual-confirm`, {
      room_id: roomId,
      episode_id: episodeId,
      inviter_username: inviter.trim() || null,
      unresolved: !inviter.trim(),
      method: null,
      event_time: null,
      reason: reason.trim(),
    });
    toast("人工确认已写入并完成关系重算");
    await loadTrace();
  }

  async function openMemberDetail(roomId, memberKey, displayName) {
    if (!activeCampaignRequired()) return;
    const drawer = byId("member-drawer");
    const mask = byId("drawer-mask");
    byId("member-drawer-name").textContent = displayName || "成员详情";
    setAvatarContainer("member-drawer-avatar", "", displayName || "成员详情");
    byId("member-drawer-meta").textContent = `${currentCampaign()?.name || "当前活动"} · 正在读取`;
    byId("member-drawer-body").innerHTML = '<div class="loading-block">正在读取成员详情…</div>';
    mask.hidden = false;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    const data = await apiGet(`${API_PREFIX}/member-detail`, {
      campaign_id: state.activeCampaignId,
      room_id: roomId,
      member_key: memberKey,
    });
    state.currentMemberDetail = data;
    setAvatarContainer("member-drawer-avatar", data.member_avatar_url, displayName || "成员详情");
    state.currentDetailTab = "invite";
    all("[data-detail-tab]").forEach((button) => button.classList.toggle("active", button.dataset.detailTab === "invite"));
    byId("member-drawer-meta").textContent = `${currentCampaign()?.name || "当前活动"} · ${roomId}`;
    renderMemberDetail();
  }

  function closeMemberDrawer() {
    const drawer = byId("member-drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    byId("drawer-mask").hidden = true;
    state.currentMemberDetail = null;
  }

  function renderMemberDetail() {
    const root = byId("member-drawer-body");
    const detail = state.currentMemberDetail;
    if (!detail) {
      root.innerHTML = '<div class="loading-block">暂无详情</div>';
      return;
    }
    const sections = detail.sections || {};
    const tab = state.currentDetailTab;
    if (tab === "invite") {
      const items = sections.invite?.items || [];
      if (!items.length) {
        root.innerHTML = '<div class="detail-callout">当前成员没有可可靠关联的邀请事实，系统不会猜测邀请人。</div>';
        return;
      }
      root.innerHTML = `<div class="detail-callout">邀请关系来自微信系统消息；二维码无法可靠归因时会明确显示“待归因”。</div><div class="timeline">${items.map((item) => {
        const p = item.payload || {};
        const inviter = p.method === "unattributed_qr" ? "待归因" : (p.inviter_name_raw || item.actor_id || "待确认");
        const invitee = p.invitee_name_raw || item.subject_id || "当前成员";
        return `<div class="timeline-item"><div class="invite-relation-identities">${identityMarkup(item.inviter_avatar_url, inviter)}<span class="relation-arrow">→</span>${identityMarkup(item.invitee_avatar_url, invitee)}</div><p>${escapeHtml(methodLabel(p.method))} · ${escapeHtml(formatTime(item.event_time, { includeDate: true }))}</p></div>`;
      }).join("")}</div>`;
      return;
    }
    if (tab === "messages") {
      const items = sections.messages?.items || [];
      root.innerHTML = items.length
        ? `<div class="timeline">${items.map((item) => `<div class="timeline-item"><strong>${escapeHtml(item.message_type || "消息")} · ${escapeHtml(formatTime(item.event_time, { includeDate: true }))}</strong><p>${escapeHtml(item.text_content || "[无可展示文字内容]")}</p></div>`).join("")}</div>`
        : '<div class="detail-callout">30 天消息缓存中暂未找到可关联到该成员的消息。</div>';
      return;
    }
    if (tab === "media") {
      const items = sections.media?.items || [];
      root.innerHTML = items.length
        ? `<div class="media-grid">${items.map((item) => `<div class="media-item"><strong>${escapeHtml(item.message_type || "媒体")}</strong><span>${escapeHtml(formatTime(item.event_time, { includeDate: true }))}</span><span>${escapeHtml(item.text_content || "媒体引用已保留")}</span></div>`).join("")}</div>`
        : '<div class="detail-callout">当前缓存中没有可关联的图片、表情或其他媒体记录。</div>';
      return;
    }
    root.innerHTML = '<div class="detail-callout"><strong>AI 洞察 · 即将开放</strong><br>Round 1 先把邀请关系、消息与多媒体事实打牢；后续再在本地事实层之上生成购买意向、高频问题、未回复事项和跟进建议。</div>';
  }

  function csvCell(value) {
    const text = String(value ?? "");
    return `"${text.replaceAll('"', '""')}"`;
  }

  function safeFilename(value) {
    return String(value || "export").replace(/[\\/:*?"<>|]+/g, "-").slice(0, 90);
  }

  function downloadCsv(filename, rows) {
    const content = `\uFEFF${rows.map((row) => row.map(csvCell).join(",")).join("\r\n")}`;
    const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function exportDashboard() {
    const dashboard = state.dashboard;
    if (!dashboard) return;
    const rows = [
      ["指标", "数值"],
      ["活动", currentCampaign()?.name || ""],
      ["活动群数", dashboard.room_count],
      ["当前成员", dashboard.current_members],
      ["今日新增", dashboard.today_joined],
      ["今日退群", dashboard.today_left?.available ? dashboard.today_left.value : "暂不可用"],
      ["邀请排行范围", inviteWindowLabel(dashboard.invite_window || "campaign")],
      [],
      ["邀请人", "邀请人数"],
      ...(dashboard.invite_ranking || []).map((item) => [item.name, item.count]),
    ];
    downloadCsv(`${safeFilename(currentCampaign()?.name || "活动")}-总览.csv`, rows);
  }

  function exportInviteRankingData() {
    const rows = [["邀请人", "邀请人数", "统计范围"]];
    const scope = inviteWindowLabel(state.dashboard?.invite_window || "campaign");
    for (const item of state.dashboard?.invite_ranking || []) rows.push([item.name || "", item.count || 0, scope]);
    downloadCsv(`${safeFilename(currentCampaign()?.name || "活动")}-${scope}-邀请人数分布.csv`, rows);
  }

  function exportTrace() {
    const rows = [["成员", "所在群", "入群时间", "邀请人", "方式", "邀请人数", "状态"]];
    for (const item of state.traceItems) {
      rows.push([
        item.member_name || "",
        item.room_name || item.room_id || "",
        formatTime(item.event_time, { includeDate: true }),
        item.inviter_name || "",
        methodLabel(item.method),
        item.invite_count || 0,
        item.status || "状态待确认",
      ]);
    }
    downloadCsv(`${safeFilename(currentCampaign()?.name || "活动")}-邀请溯源.csv`, rows);
  }

  async function openCampaignDialog() {
    const dialog = byId("campaign-dialog");
    const campaign = currentCampaign();
    byId("campaign-current-card").innerHTML = campaign
      ? `<strong>${escapeHtml(campaign.name)}</strong><br><span>${campaign.ended_at ? `已结束 · ${escapeHtml(formatTime(campaign.ended_at, { includeDate: true }))}` : "进行中"} · 当前包含 ${formatNumber(state.campaignRoomIds.size)} 个群</span>`
      : "当前没有活动";
    byId("end-campaign").disabled = !campaign || Boolean(campaign.ended_at);
    await renderCampaignRoomPicker();
    dialog.showModal();
  }

  async function renderCampaignRoomPicker() {
    const data = await apiGet(`${API_PREFIX}/rooms`);
    const rooms = Array.isArray(data.rooms) ? data.rooms : [];
    const available = rooms.filter((room) => !state.campaignRoomIds.has(room.room_id));
    const root = byId("campaign-room-picker");
    root.replaceChildren();
    if (!available.length) {
      root.innerHTML = '<div class="loading-block">所有已发现群都已经在当前活动中。</div>';
      return;
    }
    for (const room of available) {
      const label = document.createElement("label");
      label.className = "room-picker-row";
      label.innerHTML = `<input type="checkbox" value="${escapeHtml(room.room_id)}">${avatarMarkup(room.avatar_url, room.display_name || room.room_id, "group-avatar", { group: true })}<span><strong>${escapeHtml(room.display_name || room.room_id)}</strong><small>${formatNumber(room.member_count ?? 0)} 人 · ${escapeHtml(roomHealth(room).label)}</small></span>`;
      root.appendChild(label);
    }
  }

  async function createCampaign() {
    const input = byId("new-campaign-name");
    const name = input.value.trim();
    if (!name) {
      toast("请输入活动名称。", "error");
      input.focus();
      return;
    }
    const campaign = await apiPost(`${API_PREFIX}/campaigns`, { name });
    input.value = "";
    await loadCampaigns({ preferCampaignId: campaign.campaign_id });
    byId("campaign-select").value = state.activeCampaignId;
    await openCampaignDialog();
    toast(`已创建活动：${campaign.name}`);
    await loadCurrentScreen();
  }

  async function endCurrentCampaign() {
    const campaign = currentCampaign();
    if (!campaign || campaign.ended_at) return;
    if (!window.confirm(`确认结束活动“${campaign.name}”？历史数据会继续保留。`)) return;
    await apiPost(`${API_PREFIX}/campaigns/end`, { campaign_id: campaign.campaign_id });
    await loadCampaigns({ preferCampaignId: campaign.campaign_id });
    await openCampaignDialog();
    toast(`活动已结束：${campaign.name}`);
  }

  async function addSelectedRooms() {
    if (!activeCampaignRequired()) return;
    const campaign = currentCampaign();
    if (campaign?.ended_at) {
      toast("已结束活动不能继续加入群聊。", "error");
      return;
    }
    const roomIds = all('#campaign-room-picker input[type="checkbox"]:checked').map((node) => node.value);
    if (!roomIds.length) {
      toast("请至少选择一个群聊。", "error");
      return;
    }
    await apiPost(`${API_PREFIX}/campaigns/rooms/add`, {
      campaign_id: state.activeCampaignId,
      room_ids: roomIds,
    });
    await loadCampaignRoomSet();
    await renderCampaignRoomPicker();
    toast(`已加入 ${roomIds.length} 个群聊`);
    await loadCurrentScreen();
  }

  async function addSingleRoom(roomId) {
    if (!activeCampaignRequired()) return;
    await apiPost(`${API_PREFIX}/campaigns/rooms/add`, {
      campaign_id: state.activeCampaignId,
      room_ids: [roomId],
    });
    await loadCampaignRoomSet();
    toast("群聊已加入当前活动");
    await loadGroups();
  }

  async function removeRoom(roomId) {
    if (!activeCampaignRequired()) return;
    await apiPost(`${API_PREFIX}/campaigns/rooms/remove`, {
      campaign_id: state.activeCampaignId,
      room_id: roomId,
    });
    await loadCampaignRoomSet();
    toast("群聊已从当前活动移出；历史保护数据不会被删除。", "success");
    await loadGroups();
  }

  async function loadCurrentScreen() {
    if (state.currentScreen === "dashboard") return loadDashboard();
    if (state.currentScreen === "groups") return loadGroups();
    if (state.currentScreen === "trace") return loadTrace();
    return undefined;
  }

  function bindEvents() {
    all(".rail-button[data-screen], #bottom-nav [data-screen]").forEach((button) => {
      button.addEventListener("click", () => showScreen(button.dataset.screen));
    });

    byId("campaign-select").addEventListener("change", (event) => {
      switchCampaign(event.target.value).catch((error) => toast(error.message, "error"));
    });

    byId("health-button").addEventListener("click", () => {
      toast(byId("health-button").dataset.healthMessage || "监听状态正在更新");
    });
    byId("dashboard-export").addEventListener("click", exportDashboard);
    byId("chart-download").addEventListener("click", exportInviteRankingData);
    byId("invite-window").addEventListener("change", () => {
      loadDashboard().catch((error) => toast(error.message, "error"));
    });
    byId("manage-campaign").addEventListener("click", () => openCampaignDialog().catch((error) => toast(error.message, "error")));
    byId("add-groups-button").addEventListener("click", () => openCampaignDialog().catch((error) => toast(error.message, "error")));
    byId("export-all-members").addEventListener("click", () => exportAllMembers().catch((error) => toast(error.message, "error")));
    byId("export-room-members").addEventListener("click", exportCurrentRoomMembers);

    byId("group-filter-apply").addEventListener("click", () => loadGroups().catch((error) => toast(error.message, "error")));
    byId("group-query").addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadGroups().catch((error) => toast(error.message, "error"));
    });
    byId("group-range").addEventListener("change", () => loadGroups().catch((error) => toast(error.message, "error")));
    byId("group-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-room-action]");
      const card = event.target.closest("[data-room-id]");
      if (!button || !card) return;
      const roomId = card.dataset.roomId;
      const action = button.dataset.roomAction;
      if (action === "trace") {
        openTrace({ roomId }).catch((error) => toast(error.message, "error"));
      } else if (action === "members") {
        openRoomMembers(roomId).catch((error) => toast(error.message, "error"));
      } else if (action === "rescan") {
        rescanAndReload().catch((error) => toast(error.message, "error"));
      } else if (action === "archive-refresh") {
        archiveAction(roomId, "refresh").catch((error) => toast(error.message, "error"));
      } else if (action === "archive-start") {
        archiveAction(roomId, "start").catch((error) => toast(error.message, "error"));
      } else if (action === "archive-stop") {
        archiveAction(roomId, "stop").catch((error) => toast(error.message, "error"));
      } else if (action === "remove") {
        removeRoom(roomId).catch((error) => toast(error.message, "error"));
      } else if (action === "add") {
        addSingleRoom(roomId).catch((error) => toast(error.message, "error"));
      }
    });

    byId("invite-ranking").addEventListener("click", (event) => {
      const row = event.target.closest("[data-trace-query]");
      if (!row) return;
      openTrace({ query: row.dataset.traceQuery || "" }).catch((error) => toast(error.message, "error"));
    });
    byId("ranking-open-trace").addEventListener("click", () => showScreen("trace"));
    byId("trace-ranking").addEventListener("click", () => showScreen("dashboard"));
    byId("trace-export").addEventListener("click", exportTrace);
    byId("trace-filter-apply").addEventListener("click", () => loadTrace().catch((error) => toast(error.message, "error")));
    byId("trace-query").addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadTrace().catch((error) => toast(error.message, "error"));
    });
    byId("trace-table-body").addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-member-key]");
      if (!row) return;
      if (event.target.closest("[data-manual-confirm]")) {
        manualConfirmTraceRow(row).catch((error) => toast(error.message, "error"));
        return;
      }
      openMemberDetail(row.dataset.roomId, row.dataset.memberKey, row.dataset.memberName).catch((error) => {
        closeMemberDrawer();
        toast(error.message, "error");
      });
    });

    byId("member-drawer-close").addEventListener("click", closeMemberDrawer);
    byId("drawer-mask").addEventListener("click", closeMemberDrawer);
    all("[data-detail-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        state.currentDetailTab = button.dataset.detailTab;
        all("[data-detail-tab]").forEach((node) => node.classList.toggle("active", node === button));
        renderMemberDetail();
      });
    });

    all("[data-dialog-close]").forEach((button) => {
      button.addEventListener("click", () => byId(button.dataset.dialogClose)?.close());
    });
    byId("create-campaign").addEventListener("click", () => createCampaign().catch((error) => toast(error.message, "error")));
    byId("new-campaign-name").addEventListener("keydown", (event) => {
      if (event.key === "Enter") createCampaign().catch((error) => toast(error.message, "error"));
    });
    byId("end-campaign").addEventListener("click", () => endCurrentCampaign().catch((error) => toast(error.message, "error")));
    byId("add-selected-rooms").addEventListener("click", () => addSelectedRooms().catch((error) => toast(error.message, "error")));
    document.addEventListener("error", (event) => {
      if (event.target?.matches?.("img[data-avatar-image]")) event.target.hidden = true;
    }, true);
  }

  async function initialize() {
    bindEvents();
    try {
      await loadCampaigns();
      await Promise.all([loadHealth(), loadDashboard()]);
      state.healthTimer = window.setInterval(() => {
        loadHealth().catch(() => {});
      }, 5000);
    } catch (error) {
      toast(error.message, "error");
      renderHealth({ status: "error", stale: true, error_message: error.message });
    }
  }

  window.addEventListener("beforeunload", () => {
    if (state.healthTimer) window.clearInterval(state.healthTimer);
  });

  initialize();
})();
