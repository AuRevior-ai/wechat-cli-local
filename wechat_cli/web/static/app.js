const nav = document.querySelector("#nav");
const screens = [...document.querySelectorAll(".screen")];
const title = document.querySelector("#screen-title");
const resultArea = document.querySelector(".result-area");
const result = document.querySelector("#result");
const commandPreview = document.querySelector("#command-preview");
const downloadButton = document.querySelector("#download-result");
const copyButton = document.querySelector("#copy-result");
const copyKeyButton = document.querySelector("#copy-key-result");
const aiPackageButton = document.querySelector("#build-ai-package");
const aiPackageTranscribe = document.querySelector("#ai-package-transcribe");
const initPill = document.querySelector("#init-pill");
const statusList = document.querySelector("#status-list");
const dbDirSelect = document.querySelector("#db-dir-candidates");
const detectDbDirsButton = document.querySelector("#detect-db-dirs");
const setupDbDirInput = document.querySelector("#setup-db-dir");
const profileAvatar = document.querySelector("#profile-avatar");
const profileName = document.querySelector("#profile-name");
const defaultTodayInputs = [...document.querySelectorAll("[data-default-today]")];
const snsForm = document.querySelector("#sns .sns-form");
const snsResetButton = document.querySelector("#sns-reset");
const snsLoadMoreButton = document.querySelector("#sns-load-more");
const snsTimeline = document.querySelector("#sns-timeline");
const snsLocalError = document.querySelector("#sns-local-error");
const snsLoadingIndicator = document.querySelector("#sns-loading");
const snsEmpty = document.querySelector("#sns-empty");

let lastText = "";
let lastKeyText = "";
let lastDownload = null;
let lastCopyData = null;
let dbDirCandidates = [];
let sessions = [];
let sessionsLoaded = false;
let sessionsRequestVersion = 0;
let currentScreenId = document.querySelector(".screen.active")?.id || "dashboard";
let snsPosts = [];
let snsOffset = 0;
let snsHasMore = false;
let snsSeenPostIds = new Set();
let snsLoading = false;
let snsFilters = {};
const snsMediaState = new Map();
const screenResultStates = new Map();
const screenRequestVersions = new Map();
const SUMMARY_PREVIEW_LIMIT = 200;
const sessionPickers = [...document.querySelectorAll("[data-session-picker]")]
  .map(createSessionPicker);

const TYPE_LABELS = {
  text: "文本",
  image: "图片",
  voice: "语音",
  video: "视频",
  sticker: "表情",
  location: "位置",
  link: "链接",
  file: "文件",
  call: "通话",
  system: "系统",
};

const FIELD_LABELS = {
  chat: "聊天名称",
  username: "账号",
  is_group: "是否群聊",
  unread: "未读数量",
  unread_count: "未读数量",
  last_message: "最后一条消息",
  msg_type: "消息类型",
  sender: "发送者",
  sender_name: "发送者",
  timestamp: "时间戳",
  time: "时间",
  count: "数量",
  type: "类型",
  display_name: "显示名称",
  nick_name: "昵称",
  remark: "备注",
  alias: "微信号",
  description: "描述",
  local_type: "联系人类型",
  verify_flag: "认证标记",
  is_subscription: "是否公众号",
  avatar: "头像",
  avatar_url: "头像地址",
  chat_username: "聊天账号",
  chat_avatar_url: "聊天头像",
  sender_username: "发送者账号",
  sender_avatar_url: "发送者头像",
  is_self: "是否本人",
  line: "消息文本",
  text: "消息内容",
  type_label: "消息类型",
  group: "群聊名称",
  member_count: "成员数量",
  members: "群成员",
  owner: "群主",
  summary: "摘要",
  source_chat: "来源会话",
  from: "来源",
  id: "记录编号",
  first_call: "是否首次读取",
  new_count: "新增数量",
  keyword: "关键词",
  scope: "查询范围",
  results: "查询结果",
  start_time: "开始日期",
  end_time: "结束日期",
  limit: "读取上限",
  offset: "偏移数量",
  messages: "聊天记录",
  message_items: "消息详情",
  saved_media: "已保存媒体",
  save_dir: "保存目录",
  failures: "读取失败",
  total: "消息总数",
  type_breakdown: "类型分布",
  top_senders: "发言排行",
  hourly: "时段分布",
  query: "查询内容",
  name: "名称",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function snsReasonLabel(reason) {
  return {
    remote_deferred: "远程媒体暂不读取",
    local_identity_unproven: "本地媒体身份尚未证明",
    local_identity_ambiguous: "本地媒体存在多个候选",
    local_missing: "本地媒体不存在",
    unsupported_type: "暂不支持的媒体类型",
    key_unavailable: "媒体密钥不可用",
    decode_failed: "媒体解码失败",
    placeholder_only: "仅有占位数据",
    invalid_media_payload: "媒体内容校验失败",
    io_error: "本地媒体读取失败",
  }[String(reason || "")] || String(reason || "");
}

function renderSnsLikes(likes) {
  const items = Array.isArray(likes) ? likes : [];
  if (!items.length) return "";
  return `<div class="sns-likes"><b>赞</b><span>${items.map((item) =>
    escapeHtml(item.display_name || item.username || "未知用户")
  ).join("、")}</span></div>`;
}

function renderSnsComments(comments) {
  const items = Array.isArray(comments) ? comments : [];
  if (!items.length) return "";
  return `<div class="sns-comments">${items.map((comment) => {
    const author = escapeHtml(comment.display_name || comment.username || "未知用户");
    const reply = comment.reply_to_display_name || comment.reply_to_username || "";
    const replyText = reply ? ` <span>回复 ${escapeHtml(reply)}</span>` : "";
    return `<div><b>${author}</b>${replyText}<p>${escapeHtml(comment.content || "")}</p></div>`;
  }).join("")}</div>`;
}

function renderSnsMediaSummary(media) {
  const items = Array.isArray(media) ? media : [];
  if (!items.length) return "";
  return `<div class="sns-media-summary">${items.map((item) => {
    const type = escapeHtml(item.type || "unsupported");
    const status = escapeHtml(item.status || "unsupported");
    const reason = snsReasonLabel(item.reason);
    let components = "";
    if (item.type === "live_photo") {
      components = ["image", "video"].map((name) => {
        const component = item[name] || {};
        return `<span>${name === "image" ? "静态图" : "视频"} · ${escapeHtml(component.status || "metadata_only")} · ${escapeHtml(snsReasonLabel(component.reason))}</span>`;
      }).join("");
    }
    return `<div class="sns-media-chip"><b>${type}</b><span>${status}${reason ? ` · ${escapeHtml(reason)}` : ""}</span>${components}</div>`;
  }).join("")}</div>`;
}

function snsMediaKey(postId, mediaId) {
  return `${postId}:${mediaId}`;
}

function setSnsMediaState(key, nextState) {
  const previous = snsMediaState.get(key);
  if (previous?.objectUrl && previous.objectUrl !== nextState?.objectUrl) {
    URL.revokeObjectURL(previous.objectUrl);
  }
  snsMediaState.set(key, nextState);
}

function clearSnsMediaState() {
  for (const state of snsMediaState.values()) {
    if (state?.objectUrl) URL.revokeObjectURL(state.objectUrl);
  }
  snsMediaState.clear();
}

function snsLivePhotoComponent(media, name) {
  const component = media?.[name] || {};
  return {
    status: component.status || "metadata_only",
    reason: component.reason || "local_identity_unproven",
  };
}

function snsMediaFailureLabel(state) {
  if (state.status === "unsupported") return "本地媒体格式暂不支持";
  if (state.status === "schema_error") return "朋友圈媒体结构暂不兼容";
  if (state.status === "source_error") return "朋友圈本地数据源或密钥不可用";
  if (state.status === "error") return "本地媒体读取发生错误";
  return snsReasonLabel(state.reason) || "本地媒体当前不可用";
}

function renderSnsMedia(post) {
  const items = Array.isArray(post?.media) ? post.media : [];
  if (!items.length) return "";
  return `<div class="sns-media-summary">${items.map((media) => {
    const mediaId = String(media?.media_id || "");
    const mediaType = String(media?.type || "unsupported");
    if (media.type === "live_photo") {
      const still = snsLivePhotoComponent(media, "image");
      const video = snsLivePhotoComponent(media, "video");
      return `<div class="sns-media-chip sns-live-photo"><b>live_photo</b>
        <span>静态图 · ${escapeHtml(still.status)} · ${escapeHtml(snsReasonLabel(still.reason))}</span>
        <span>视频组件 · ${escapeHtml(video.status)} · ${escapeHtml(snsReasonLabel(video.reason))}</span>
      </div>`;
    }
    if (media.type === "image") {
      return `<div class="sns-media-chip"><b>image</b><span>${escapeHtml(media.status || "metadata_only")} · ${escapeHtml(snsReasonLabel(media.reason || "local_identity_unproven"))}</span></div>`;
    }
    if (media.type !== "video") {
      return `<div class="sns-media-chip"><b>${escapeHtml(mediaType)}</b><span>${escapeHtml(media.status || "unsupported")} · ${escapeHtml(snsReasonLabel(media.reason || "unsupported_type"))}</span></div>`;
    }

    const key = snsMediaKey(post.post_id, mediaId);
    const local = snsMediaState.get(key);
    if (local?.status === "resolving") {
      return `<div class="sns-media-chip sns-media-state"><b>video</b><span>正在检查本地视频…</span></div>`;
    }
    if (local?.status === "ready" && local.objectUrl) {
      const previewSrc = escapeHtml(local.objectUrl);
      const downloadUrl = `/api/sns/media/${encodeURIComponent(post.post_id)}/${encodeURIComponent(mediaId)}?variant=original&download=1`;
      return `<div class="sns-media-chip sns-media-preview"><b>video · 本地已验证</b>
        <video controls preload="metadata" src="${previewSrc}"></video>
        <a class="sns-media-download" href="${escapeHtml(downloadUrl)}">下载原视频</a>
      </div>`;
    }
    if (local) {
      return `<div class="sns-media-chip sns-media-state"><b>video</b><span>${escapeHtml(snsMediaFailureLabel(local))}</span></div>`;
    }
    if (media.type === "video" && media.reason === "remote_deferred") {
      return `<div class="sns-media-chip"><b>video</b><span>${escapeHtml(snsReasonLabel(media.reason))}</span>
        <button type="button" class="ghost sns-media-check" data-sns-resolve-post="${escapeHtml(post.post_id || "")}" data-sns-resolve-media="${escapeHtml(mediaId)}">检查本地视频</button>
      </div>`;
    }
    return `<div class="sns-media-chip"><b>video</b><span>${escapeHtml(media.status || "metadata_only")} · ${escapeHtml(snsReasonLabel(media.reason))}</span></div>`;
  }).join("")}</div>`;
}

async function resolveSnsVideo(postId, mediaId) {
  const key = snsMediaKey(postId, mediaId);
  setSnsMediaState(key, { status: "resolving" });
  renderSnsTimelineState();
  const url = `/api/sns/media/${encodeURIComponent(postId)}/${encodeURIComponent(mediaId)}?variant=preview`;
  try {
    const response = await fetch(url, { method: "GET", credentials: "same-origin" });
    if (response.ok) {
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      setSnsMediaState(key, { status: "ready", objectUrl });
      renderSnsTimelineState();
      return;
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
    const error = payload?.error || {};
    let status = "unavailable";
    if (error.code === "SNS_MEDIA_UNSUPPORTED") status = "unsupported";
    if (error.code === "SNS_SCHEMA_UNSUPPORTED") status = "schema_error";
    if (error.code === "SNS_DB_NOT_FOUND" || error.code === "SNS_KEY_NOT_FOUND") {
      status = "source_error";
    }
    if (error.code === "SNS_INTERNAL_ERROR") status = "error";
    setSnsMediaState(key, {
      status,
      code: error.code || "SNS_INTERNAL_ERROR",
      reason: error?.details?.reason || null,
    });
  } catch (_error) {
    setSnsMediaState(key, { status: "error", code: "SNS_INTERNAL_ERROR", reason: null });
  }
  renderSnsTimelineState();
}

function renderSnsLocation(location) {
  if (!location || typeof location !== "object") return "";
  const primary = location.poiName || location.poiAddressName || location.poiAddress || "";
  const region = [location.city, location.country].filter(Boolean).join(" · ");
  if (!primary && !region) return "";
  return `<div class="sns-location"><b>位置</b><span>${escapeHtml(primary)}${primary && region ? " · " : ""}${escapeHtml(region)}</span></div>`;
}

function renderSnsError(error) {
  const code = escapeHtml(error?.code || "SNS_INTERNAL_ERROR");
  const message = escapeHtml(error?.message || "朋友圈读取失败");
  const retryable = error?.retryable === true ? "可重试" : "不可重试";
  return `<div class="sns-error-card"><b>${code}</b><p>${message}</p><span>${retryable}</span></div>`;
}

function renderSnsPost(post) {
  const displayName = escapeHtml(post.display_name || post.username || "未知发布者");
  const username = escapeHtml(post.username || "");
  const time = escapeHtml(post.time || "时间未知");
  const content = escapeHtml(post.content || "");
  const partial = post.parse_status === "partial";
  const warnings = Array.isArray(post.warnings) ? post.warnings : [];
  return `<article class="sns-card${partial ? " sns-partial" : ""}" data-post-id="${escapeHtml(post.post_id || "")}">
    <header class="sns-card-head">
      <div><strong>${displayName}</strong>${username && username !== displayName ? `<code>${username}</code>` : ""}</div>
      <time>${time}</time>
    </header>
    ${content ? `<p class="sns-content">${content}</p>` : '<p class="sns-content sns-content-empty">（无文字内容）</p>'}
    ${renderSnsLocation(post.location)}
    ${renderSnsMedia(post)}
    <div class="sns-interactions">${renderSnsLikes(post.likes)}${renderSnsComments(post.comments)}</div>
    ${partial ? `<div class="sns-partial-note">部分字段无法解析${warnings.length ? ` · ${warnings.map(escapeHtml).join("、")}` : ""}</div>` : ""}
  </article>`;
}

function mergeSnsPage(current, seenIds, payload) {
  const merged = current.slice();
  for (const post of payload.posts || []) {
    const postId = String(post?.post_id || "");
    if (!postId || seenIds.has(postId)) continue;
    seenIds.add(postId);
    merged.push(post);
  }
  return merged;
}

function nextSnsOffset(payload) {
  return Number(payload.offset || 0) + Number(payload.count || 0);
}

function readSnsFilters() {
  if (!snsForm) return {};
  const params = readForm(snsForm).params || {};
  return compactObject({
    query: params.query,
    user: params.user,
    start_time: params.start_time,
    end_time: params.end_time,
  });
}

function renderSnsTimelineState() {
  if (!snsTimeline || !snsEmpty || !snsLoadMoreButton) return;
  snsTimeline.innerHTML = snsPosts.map(renderSnsPost).join("");
  snsEmpty.hidden = snsPosts.length > 0 || snsLoading;
  if (!snsPosts.length && !snsLoading) {
    snsEmpty.textContent = "当前条件下没有朋友圈记录。";
  }
  snsLoadMoreButton.hidden = !snsHasMore;
}

function setSnsLocalError(error) {
  if (!snsLocalError) return;
  if (!error) {
    snsLocalError.hidden = true;
    snsLocalError.innerHTML = "";
    return;
  }
  snsLocalError.innerHTML = renderSnsError(error);
  snsLocalError.hidden = false;
}

async function loadSnsPage({ reset = false } = {}) {
  if (snsLoading) return;
  if (reset) {
    clearSnsMediaState();
    snsPosts = [];
    snsSeenPostIds.clear();
    snsOffset = 0;
    snsHasMore = false;
    renderSnsTimelineState();
  }

  snsLoading = true;
  setSnsLocalError(null);
  if (snsLoadingIndicator) snsLoadingIndicator.hidden = false;
  if (snsEmpty) snsEmpty.hidden = true;
  if (snsLoadMoreButton) {
    snsLoadMoreButton.disabled = true;
    snsLoadMoreButton.hidden = true;
  }

  try {
    const payload = await fetchJson("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command: "sns",
        params: {
          ...snsFilters,
          limit: 20,
          offset: snsOffset,
        },
      }),
    });
    if (!payload?.ok) {
      setSnsLocalError(payload?.error || {
        code: "SNS_INTERNAL_ERROR",
        message: "朋友圈读取失败",
        retryable: false,
      });
      return;
    }

    const data = payload.data || {};
    snsPosts = mergeSnsPage(snsPosts, snsSeenPostIds, data);
    // The live DB can change between requests. De-dup hides repeated cards only;
    // it does not claim exactly-once traversal or guarantee that old rows cannot be skipped.
    snsOffset = nextSnsOffset(data);
    snsHasMore = Boolean(data.has_more);
    renderSnsTimelineState();
  } catch (error) {
    setSnsLocalError({
      code: "SNS_INTERNAL_ERROR",
      message: error?.message || "朋友圈读取失败",
      retryable: true,
    });
  } finally {
    snsLoading = false;
    if (snsLoadingIndicator) snsLoadingIndicator.hidden = true;
    if (snsLoadMoreButton) {
      snsLoadMoreButton.disabled = !snsHasMore;
      snsLoadMoreButton.hidden = !snsHasMore;
    }
    if (snsEmpty) {
      snsEmpty.hidden = snsPosts.length > 0;
      if (!snsPosts.length && snsLocalError?.hidden !== false) {
        snsEmpty.textContent = "当前条件下没有朋友圈记录。";
      }
    }
  }
}

function emptyResultState(screenId) {
  const screen = document.getElementById(screenId);
  const screenTitle = screen?.dataset.title || "当前功能";
  return {
    className: "result empty",
    html: `请在“${escapeHtml(screenTitle)}”中提交操作，结果只会显示在这里。`,
    commandPreview: "等待操作",
    lastText: "",
    lastKeyText: "",
    lastDownload: null,
    copyData: null,
  };
}

function saveResultState(screenId) {
  if (!screenId) return;
  screenResultStates.set(screenId, {
    className: result.className,
    html: result.innerHTML,
    commandPreview: commandPreview.textContent,
    lastText,
    lastKeyText,
    lastDownload,
    copyData: lastCopyData,
  });
}

function applyResultState(state) {
  result.className = state.className;
  result.innerHTML = state.html;
  commandPreview.textContent = state.commandPreview;
  lastText = state.lastText || "";
  lastKeyText = state.lastKeyText || "";
  lastDownload = state.lastDownload || null;
  lastCopyData = state.copyData || null;
  copyKeyButton.classList.toggle("hidden", !lastKeyText && !lastCopyData);
  downloadButton.classList.toggle("hidden", !lastDownload);
  downloadButton.textContent = lastDownload?.label || "下载";
}

function restoreResultState(screenId) {
  applyResultState(screenResultStates.get(screenId) || emptyResultState(screenId));
}

function setScreen(id) {
  if (currentScreenId && currentScreenId !== id) {
    saveResultState(currentScreenId);
  }
  currentScreenId = id;
  screens.forEach((screen) => screen.classList.toggle("active", screen.id === id));
  nav.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset.target === id);
  });
  const activeScreen = document.getElementById(id);
  title.textContent = activeScreen?.dataset.title || "WeChat CLI Web";
  resultArea.classList.toggle("hidden", ["about-support", "sns"].includes(id));
  if (id === "setup" && dbDirCandidates.length === 0) {
    refreshDbDirs().catch((error) => showTransientError(error, "setup"));
  }
  restoreResultState(id);
  if (activeScreen?.querySelector("[data-session-picker]") && !sessionsLoaded) {
    loadSessions().catch(showSessionsError);
  }
}

function compactObject(obj) {
  return Object.fromEntries(Object.entries(obj).filter(([, value]) => {
    if (value === "" || value === null || value === undefined) return false;
    if (Array.isArray(value) && value.length === 0) return false;
    return true;
  }));
}

function readForm(form) {
  const params = {};
  form.querySelectorAll("[data-param]").forEach((field) => {
    const name = field.dataset.param;
    let value;
    if (field.type === "checkbox") {
      value = field.checked;
    } else if (field.dataset.list === "lines") {
      value = field.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    } else if (field.type === "number") {
      value = field.value === "" ? "" : Number(field.value);
    } else {
      value = field.value.trim();
    }
    params[name] = value;
  });
  if (form.dataset.command === "history") {
    params.media = true;
  }
  if (form.dataset.command === "invite-stats") {
    params.bind_identity = String(params.bind_identity || "")
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
  }
  return { command: form.dataset.command, params: compactObject(params) };
}

function validatePayload(payload) {
  if (payload.command === "init") {
    const params = payload.params || {};
    if (!params.db_dir && dbDirCandidates.length > 1) {
      return "检测到多个微信目录，请先选择一个 db_storage 目录。";
    }
    return "";
  }
  const params = payload.params || {};
  if (payload.command === "members" || payload.command === "invite-stats") {
    if (!params.group_name) return "请先从下拉列表选择一个群聊。";
  }
  if (payload.command === "stats" && !params.chat_name) {
    return "请先从下拉列表选择一个聊天。";
  }
  if (["search", "stats", "invite-stats"].includes(payload.command)) {
    if (params.start_time && params.end_time && params.start_time > params.end_time) {
      return "开始日期不能晚于结束日期。";
    }
  }
  if (payload.command === "invite-stats") {
    return "";
  }
  if (payload.command !== "search") return "";
  if (params.keyword) return "";
  if (params.start_time && params.end_time) return "";
  return "关键词为空时，必须同时填写开始时间和结束时间。";
}

function validateSummaryPayload(params = {}) {
  if (!params.chat_name) return "请先从下拉列表选择一个聊天。";
  if (!params.start_time || !params.end_time) return "请选择完整的开始日期和结束日期。";
  if (params.start_time > params.end_time) return "开始日期不能晚于结束日期。";
  return "";
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok && payload.ok !== false) {
    payload.ok = false;
  }
  return payload;
}

async function refreshStatus(shouldApply = () => true) {
  const status = await fetchJson("/api/status");
  if (!shouldApply()) return status;
  initPill.textContent = status.initialized ? `已初始化 · ${status.keys_count} 个密钥` : "未初始化";
  initPill.className = `pill ${status.initialized ? "ready" : "warn"}`;
  statusList.innerHTML = renderKeyValue({
    "初始化": status.initialized ? "是" : "否",
    "数据目录": status.db_dir || "未设置",
    "目录可访问": status.db_dir_exists ? "是" : "否",
    "密钥数量": status.keys_count,
    "配置文件": status.config_file,
    "状态目录": status.state_dir,
  }, "dl");
  if (setupDbDirInput && status.db_dir && !setupDbDirInput.value) {
    setupDbDirInput.value = status.db_dir;
    if (dbDirCandidates.length) {
      renderDbDirCandidates({ candidates: dbDirCandidates });
    }
  }
}

function renderDbDirCandidates(payload) {
  if (!dbDirSelect) return;
  dbDirCandidates = payload.candidates || [];
  dbDirSelect.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = dbDirCandidates.length ? "请选择微信目录" : "未检测到目录";
  dbDirSelect.appendChild(placeholder);

  dbDirCandidates.forEach((candidate, index) => {
    const option = document.createElement("option");
    option.value = candidate.path;
    option.textContent = `${index + 1}. ${candidate.account} · ${candidate.path}`;
    dbDirSelect.appendChild(option);
  });

  const currentValue = setupDbDirInput?.value || "";
  const matchingCandidate = dbDirCandidates.find((candidate) => candidate.path === currentValue);
  if (matchingCandidate) {
    dbDirSelect.value = matchingCandidate.path;
    return;
  }

  if (dbDirCandidates.length === 1 && setupDbDirInput && !currentValue) {
    setupDbDirInput.value = dbDirCandidates[0].path;
    dbDirSelect.value = dbDirCandidates[0].path;
  }
}

async function refreshDbDirs() {
  if (!dbDirSelect) return null;
  dbDirSelect.innerHTML = '<option value="">检测中</option>';
  const payload = await fetchJson("/api/db-dirs");
  renderDbDirCandidates(payload);
  return payload;
}

function avatarProxyUrl(url) {
  if (/^https:\/\//i.test(url || "")) {
    return `/api/avatar?url=${encodeURIComponent(url)}`;
  }
  if (/^data:image\//i.test(url || "") || /^\/api\/media\?path=/i.test(url || "")) {
    return url;
  }
  return "";
}

function avatarMarkup(url, label, className = "session-picker-option-avatar") {
  const initial = String(label || "W").trim().slice(0, 1).toUpperCase() || "W";
  const src = avatarProxyUrl(url);
  return `<span class="${escapeHtml(className)} avatar-shell">
    <span class="avatar-shell-fallback" aria-hidden="true">${escapeHtml(initial)}</span>
    ${src ? `<img src="${escapeHtml(src)}" alt="${escapeHtml(label || "头像")}" loading="lazy" data-avatar-image>` : ""}
  </span>`;
}

async function loadProfile(shouldApply = () => true) {
  if (!profileAvatar || !profileName) return null;
  const profile = await fetchJson("/api/profile");
  if (!shouldApply()) return profile;
  const displayName = profile.display_name || profile.username || "本机微信";
  profileName.textContent = displayName;
  profileName.title = profile.username || displayName;
  profileAvatar.innerHTML = avatarMarkup(
    profile.avatar_url,
    displayName,
    "brand-avatar",
  );
  return profile;
}

function createSessionPicker(root) {
  const picker = {
    root,
    filter: root.dataset.filter || "all",
    multiple: root.dataset.multiple === "true",
    search: root.querySelector(".session-picker-search"),
    value: root.querySelector(".session-picker-value"),
    chips: root.querySelector(".session-picker-chips"),
    options: root.querySelector(".session-picker-options"),
    hint: root.querySelector(".session-picker-hint"),
    retry: root.querySelector(".session-picker-retry"),
    selectedUsernames: new Set(),
    visibleSessionIndexes: [],
    activeOption: -1,
  };

  picker.search.addEventListener("focus", () => {
    renderSessionPickerOptions(picker, picker.search.value);
    setSessionPickerOpen(picker, true);
  });
  picker.search.addEventListener("input", () => {
    if (!picker.multiple) {
      picker.selectedUsernames.clear();
      picker.value.value = "";
    }
    picker.hint.textContent = picker.multiple
      ? selectedSessionHint(picker)
      : `请从匹配结果中选择一个${picker.filter === "group" ? "群聊" : "会话"}`;
    renderSessionPickerOptions(picker, picker.search.value);
    setSessionPickerOpen(picker, true);
  });
  picker.search.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setSessionPickerOpen(picker, true);
      moveSessionPickerActiveOption(picker, event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Enter" && picker.activeOption >= 0) {
      event.preventDefault();
      const sessionIndex = picker.visibleSessionIndexes[picker.activeOption];
      selectSessionPickerOption(picker, sessionIndex);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setSessionPickerOpen(picker, false);
    }
  });
  picker.options.addEventListener("mousedown", (event) => {
    event.preventDefault();
  });
  picker.options.addEventListener("click", (event) => {
    const option = event.target.closest("button[data-session-index]");
    if (option) selectSessionPickerOption(picker, Number(option.dataset.sessionIndex));
  });
  picker.chips?.addEventListener("click", (event) => {
    const removeButton = event.target.closest("button[data-remove-username]");
    if (removeButton) {
      removeSessionPickerSelection(picker, removeButton.dataset.removeUsername);
    }
  });
  picker.retry.addEventListener("click", () => {
    loadSessions().catch(showSessionsError);
  });
  return picker;
}

function setSessionPickerOpen(picker, open) {
  picker.options.hidden = !open;
  picker.search.setAttribute("aria-expanded", String(open));
  if (!open) {
    picker.activeOption = -1;
    picker.search.setAttribute("aria-activedescendant", "");
  }
}

function sessionValue(session) {
  return session.username || session.chat || "";
}

function selectedSessionHint(picker) {
  const count = picker.selectedUsernames.size;
  if (count) return `已选择 ${count} 个聊天，可继续搜索添加`;
  return "可不选择；默认搜索全部聊天";
}

function renderSessionPickerChips(picker) {
  if (!picker.multiple || !picker.chips) return;
  const selectedSessions = [...picker.selectedUsernames]
    .map((username) => sessions.find((session) => sessionValue(session) === username))
    .filter(Boolean);
  picker.chips.hidden = selectedSessions.length === 0;
  picker.chips.innerHTML = selectedSessions.map((session) => `
    <span class="session-picker-chip">
      <span>${escapeHtml(session.chat || session.username)}</span>
      <button type="button" data-remove-username="${escapeHtml(sessionValue(session))}"
        aria-label="移除 ${escapeHtml(session.chat)}">×</button>
    </span>
  `).join("");
}

function renderSessionPickerOptions(picker, query = "") {
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
  picker.visibleSessionIndexes = sessions
    .map((session, index) => ({ session, index }))
    .filter(({ session }) => {
      if (picker.filter === "group" && !session.is_group) return false;
      if (!normalizedQuery) return true;
      return [session.chat, session.username]
        .some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(normalizedQuery));
    })
    .slice(0, 80)
    .map(({ index }) => index);
  picker.activeOption = -1;
  picker.search.setAttribute("aria-activedescendant", "");

  if (!picker.visibleSessionIndexes.length) {
    const label = picker.filter === "group" ? "群聊" : "会话";
    picker.options.innerHTML = `<div class="session-picker-option-empty">没有匹配的${label}</div>`;
    return;
  }
  picker.options.innerHTML = picker.visibleSessionIndexes.map((sessionIndex) => {
    const session = sessions[sessionIndex];
    const username = sessionValue(session);
    const selected = picker.selectedUsernames.has(username);
    const meta = [
      session.is_group ? "群聊" : "私聊",
      session.time || "",
      session.unread ? `${session.unread} 条未读` : "",
    ].filter(Boolean).join(" · ");
    return `<button id="${escapeHtml(picker.root.id)}-option-${sessionIndex}" type="button" role="option"
      aria-selected="${selected}" data-session-index="${sessionIndex}">
      ${avatarMarkup(session.avatar_url, session.chat || session.username)}
      <span class="session-picker-option-main">
        <strong>${escapeHtml(session.chat || session.username)}</strong>
        <small>${escapeHtml(meta)}</small>
      </span>
      <code>${escapeHtml(session.username || "")}</code>
    </button>`;
  }).join("");
}

function selectSessionPickerOption(picker, sessionIndex) {
  const session = sessions[sessionIndex];
  if (!session || (picker.filter === "group" && !session.is_group)) return;
  const username = sessionValue(session);
  if (picker.multiple) {
    picker.selectedUsernames.add(session.username);
    picker.value.value = [...picker.selectedUsernames].join("\n");
    picker.search.value = "";
    picker.hint.textContent = selectedSessionHint(picker);
    renderSessionPickerChips(picker);
    renderSessionPickerOptions(picker);
    setSessionPickerOpen(picker, true);
    return;
  }
  picker.selectedUsernames.clear();
  picker.selectedUsernames.add(username);
  picker.search.value = session.chat || session.username;
  picker.value.value = username;
  picker.hint.textContent = `已选择：${session.chat || session.username} · ${session.is_group ? "群聊" : "私聊"}`;
  setSessionPickerOpen(picker, false);
}

function removeSessionPickerSelection(picker, username) {
  picker.selectedUsernames.delete(username);
  picker.value.value = [...picker.selectedUsernames].join("\n");
  picker.hint.textContent = selectedSessionHint(picker);
  renderSessionPickerChips(picker);
  renderSessionPickerOptions(picker, picker.search.value);
}

function moveSessionPickerActiveOption(picker, direction) {
  const optionButtons = [...picker.options.querySelectorAll("button[data-session-index]")];
  if (!optionButtons.length) return;
  if (picker.activeOption < 0) {
    picker.activeOption = direction > 0 ? 0 : optionButtons.length - 1;
  } else {
    picker.activeOption = (
      picker.activeOption + direction + optionButtons.length
    ) % optionButtons.length;
  }
  optionButtons.forEach((button, index) => {
    button.classList.toggle("active", index === picker.activeOption);
  });
  const activeButton = optionButtons[picker.activeOption];
  picker.search.setAttribute("aria-activedescendant", activeButton.id);
  activeButton.scrollIntoView({ block: "nearest" });
}

function resetSessionPickers() {
  sessionPickers.forEach((picker) => {
    picker.search.value = "";
    picker.value.value = "";
    picker.options.innerHTML = "";
    picker.selectedUsernames.clear();
    picker.visibleSessionIndexes = [];
    picker.activeOption = -1;
    picker.hint.textContent = picker.filter === "group"
      ? "正在读取群聊列表…"
      : (picker.multiple ? "可不选择；默认搜索全部聊天" : "正在读取会话列表…");
    picker.retry.classList.add("hidden");
    renderSessionPickerChips(picker);
    setSessionPickerOpen(picker, false);
  });
}

async function refreshAccountData(shouldApply = () => true) {
  if (!shouldApply()) return;
  sessionsLoaded = false;
  sessionsRequestVersion += 1;
  sessions = [];
  resetSessionPickers();
  if (profileName) profileName.textContent = "正在读取账号…";
  if (profileAvatar) {
    profileAvatar.innerHTML = avatarMarkup("", "W", "brand-avatar");
  }
  const [profileResult, sessionsResult] = await Promise.allSettled([
    loadProfile(shouldApply),
    loadSessions(shouldApply),
  ]);
  if (!shouldApply()) return;
  if (profileResult.status === "rejected") {
    if (profileName) profileName.textContent = "本机微信";
    if (profileAvatar) {
      profileAvatar.innerHTML = avatarMarkup("", "W", "brand-avatar");
    }
  }
  if (sessionsResult.status === "rejected") {
    showSessionsError(sessionsResult.reason);
  }
}

async function loadSessions(shouldApply = () => true) {
  if (!sessionPickers.length) return [];
  if (!shouldApply()) return [];
  const requestVersion = sessionsRequestVersion + 1;
  sessionsRequestVersion = requestVersion;
  sessionPickers.forEach((picker) => {
    picker.hint.textContent = picker.filter === "group"
      ? "正在读取群聊列表…"
      : "正在读取会话列表…";
    picker.retry.classList.add("hidden");
  });
  let payload;
  try {
    payload = await fetchJson("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command: "sessions",
        params: { limit: 5000 },
      }),
    });
  } catch (error) {
    if (!shouldApply() || requestVersion !== sessionsRequestVersion) return [];
    throw error;
  }
  if (!shouldApply() || requestVersion !== sessionsRequestVersion) return [];
  if (!payload.ok || !Array.isArray(payload.data)) {
    throw new Error(payload.stderr || payload.error || "无法读取会话列表");
  }
  sessions = payload.data;
  sessionsLoaded = true;
  const groupCount = sessions.filter((session) => session.is_group).length;
  sessionPickers.forEach((picker) => {
    renderSessionPickerOptions(picker, picker.search.value);
    picker.hint.textContent = picker.multiple
      ? selectedSessionHint(picker)
      : (picker.filter === "group"
        ? `已载入 ${groupCount} 个群聊，输入名称或账号可快速匹配`
        : `已载入 ${sessions.length} 个会话，输入名称或账号可快速匹配`);
  });
  return sessions;
}

function showSessionsError(error) {
  sessionPickers.forEach((picker) => {
    const label = picker.filter === "group" ? "群聊" : "会话";
    picker.hint.textContent = `${label}读取失败：${error.message}`;
    picker.retry.classList.remove("hidden");
  });
}

function showTransientError(error, screenId = currentScreenId) {
  const state = {
    className: "result error",
    html: `<pre>${escapeHtml(error.message)}</pre>`,
    commandPreview: "操作失败",
    lastText: error.message || "",
    lastKeyText: "",
    lastDownload: null,
    copyData: null,
  };
  screenResultStates.set(screenId, state);
  if (currentScreenId === screenId) {
    applyResultState(state);
  }
}

function fieldLabel(key) {
  return FIELD_LABELS[key] || String(key);
}

function renderKeyValue(obj, mode = "div") {
  const entries = Object.entries(obj || {});
  if (mode === "dl") {
    return entries.map(([key, value]) => `<dt>${escapeHtml(fieldLabel(key))}</dt><dd>${escapeHtml(formatScalar(value))}</dd>`).join("");
  }
  return `<div class="kv">${entries.map(([key, value]) => (
    `<div>${escapeHtml(fieldLabel(key))}</div><div>${escapeHtml(formatScalar(value))}</div>`
  )).join("")}</div>`;
}

function formatScalar(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(localizeStructuredValue(value), null, 2);
  return String(value);
}

function localizeStructuredValue(value) {
  if (Array.isArray(value)) return value.map(localizeStructuredValue);
  if (value && typeof value === "object") {
    const localized = {};
    Object.entries(value).forEach(([key, nestedValue]) => {
      const label = fieldLabel(key);
      const uniqueLabel = Object.prototype.hasOwnProperty.call(localized, label)
        ? `${label}（${key}）`
        : label;
      localized[uniqueLabel] = localizeStructuredValue(nestedValue);
    });
    return localized;
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  return value;
}

function formatMessagesForKeyCopy(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return "";
  return JSON.stringify(messages, null, 2)
    .split("\n")
    .slice(1, -1)
    .map((line) => `  ${line}`)
    .join("\n");
}

function keyCopyText(payload) {
  if (payload?.data && Array.isArray(payload.data.messages)) {
    return formatMessagesForKeyCopy(payload.data.messages);
  }
  return "";
}

function summarySender(item) {
  const sender = item?.sender || (item?.is_self ? "我" : "");
  return sender === "me" ? "我" : (sender || "未知发送者");
}

function summaryMessageLine(item) {
  const type = TYPE_LABELS[item?.type] || item?.type_label || item?.type || "消息";
  const text = String(item?.text || "").trim() || `[${type}]`;
  const lines = [
    `[${item?.time || "时间未知"}] ${summarySender(item)}（${type}）：${text}`,
  ];
  const transcript = String(item?.transcript || "").trim();
  if (transcript) lines.push(`  语音转文字（机器识别）：${transcript}`);
  const assetPath = String(item?.asset_path || "").trim();
  if (assetPath.startsWith("素材/")) lines.push(`  素材：${assetPath}`);
  return lines.join("\n");
}

function formatSummaryCopy(data) {
  const items = Array.isArray(data?.message_items) ? data.message_items : [];
  const lines = items.map(summaryMessageLine);
  return [
    "请总结下面这段微信聊天记录，并使用中文输出：",
    "1. 核心主题与明确结论",
    "2. 已确认的决定、承诺和分工",
    "3. 待办事项（负责人、截止时间、下一步）",
    "4. 重要数字、日期、链接和风险",
    "5. 尚未解决的问题",
    "",
    `会话：${data?.chat || data?.username || "未知会话"}`,
    `日期：${data?.start_time || "最早"} 至 ${data?.end_time || "最新"}`,
    `消息数量：${items.length}`,
    "",
    "—— 聊天记录开始 ——",
    ...(lines.length ? lines : ["（所选日期范围内没有聊天记录）"]),
    "—— 聊天记录结束 ——",
  ].join("\n");
}

function formatSummaryKeyCopy(data) {
  const items = Array.isArray(data?.message_items) ? data.message_items : [];
  return [
    `会话：${data?.chat || data?.username || "未知会话"}`,
    `日期：${data?.start_time || "最早"} 至 ${data?.end_time || "最新"}`,
    `消息数量：${items.length}`,
    "",
    ...(items.length ? items.map(summaryMessageLine) : ["（所选日期范围内没有聊天记录）"]),
  ].join("\n");
}

function renderSummaryResult(data) {
  const items = Array.isArray(data?.message_items) ? data.message_items : [];
  const previewItems = items.slice(0, SUMMARY_PREVIEW_LIMIT);
  const previewNotice = items.length > previewItems.length
    ? `<div class="summary-preview-notice">共 ${items.length} 条，仅在网页预览前 ${previewItems.length} 条；复制仍包含全部记录。</div>`
    : "";
  return `<div class="summary-result">
    <section class="summary-result-hero">
      <div class="summary-chat-identity">
        ${renderAvatar(data, data?.chat || data?.username || "聊天记录", { allowRemote: true })}
        <div>
          <span>已整理，可交给 AI</span>
          <h2>${escapeHtml(data?.chat || data?.username || "聊天记录")}</h2>
          <code>${escapeHtml(data?.username || "")}</code>
        </div>
      </div>
      <div class="summary-range">
        <span>日期范围</span>
        <strong>${escapeHtml(data?.start_time || "最早")} <i>→</i> ${escapeHtml(data?.end_time || "最新")}</strong>
      </div>
      <div class="summary-count">
        <strong>${escapeHtml(items.length)}</strong>
        <span>条消息</span>
      </div>
    </section>
    <section class="summary-copy-guide">
      <div><b>复制</b><span>包含总结要求与完整聊天记录，可直接粘贴给 AI。</span></div>
      <div><b>复制精简信息</b><span>只保留范围、时间、发言人、类型和正文。</span></div>
      <div><b>AI 资料包</b><span>提取真实图片、表情与语音；生成后复制内容会自动引用素材文件名。</span></div>
    </section>
    ${previewNotice}
    ${renderChatMessages(previewItems, {
      allowRemoteAvatars: true,
      allowRemoteMedia: false,
    })}
  </div>`;
}

function renderAiPackageResult(payload) {
  const failures = Array.isArray(payload.failures) ? payload.failures : [];
  const failureHtml = failures.length
    ? `<details class="package-failures"><summary>${failures.length} 项素材未能处理</summary><ul>${
      failures.map((failure) => `<li>${escapeHtml(
        `${failure.time || "时间未知"} · ${failure.phase || "处理"} · ${failure.error || "未知原因"}`
      )}</li>`).join("")
    }</ul></details>`
    : "";
  return `<div class="package-ready">
    <section class="package-ready-hero">
      <span>AI MATERIAL PACKAGE</span>
      <h2>${escapeHtml(payload.chat || "聊天记录")} 已准备完成</h2>
      <p>复制内容中的“素材/文件名”与压缩包内文件一一对应，可将文字和 ZIP 一起交给 AI。</p>
    </section>
    <section class="package-metrics">
      <div><strong>${escapeHtml(payload.message_count || 0)}</strong><span>条消息</span></div>
      <div><strong>${escapeHtml(payload.asset_count || 0)}</strong><span>个素材</span></div>
      <div><strong>${escapeHtml(payload.transcription_count || 0)}</strong><span>段语音文字</span></div>
    </section>
    <section class="package-next-step">
      <b>接下来</b>
      <span>点击右上角“下载 AI 资料包”，再使用“复制”或“复制精简信息”。下载链接为一次性本机链接。</span>
    </section>
    ${failureHtml}
  </div>`;
}

function firstArray(data) {
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== "object") return null;
  for (const value of Object.values(data)) {
    if (Array.isArray(value)) return value;
  }
  return null;
}

function renderStats(data) {
  const hourly = data.hourly || {};
  const max = Math.max(1, ...Object.values(hourly).map(Number));
  const bars = Object.entries(hourly).map(([hour, count]) => {
    const width = Math.max(2, Number(count) / max * 100);
    return `<div class="bar"><span>${escapeHtml(hour)}:00</span><span><i class="bar-fill" style="display:block;width:${width}%"></i></span><b>${escapeHtml(count)}</b></div>`;
  }).join("");
  return `
    <div class="result-grid">
      <div class="item-row">${renderKeyValue({
        "消息总数": data.total,
        "类型分布": Object.entries(data.type_breakdown || {}).map(([k, v]) => `${TYPE_LABELS[k] || k}: ${v}`).join(" / "),
        "发言排行": (data.top_senders || []).map((item) => `${item.name}: ${item.count}`).join(" / "),
      })}</div>
      <div class="item-row"><strong>24 小时分布</strong><div class="bars">${bars}</div></div>
    </div>`;
}

function inviteCsv(data) {
  const rows = [[
    "邀请者排名", "邀请者", "邀请者账号", "邀请者身份状态",
    "唯一拉人数", "被邀请者", "被邀请者账号",
    "被邀请者身份状态", "入群时间", "邀请方式", "原始提示",
  ]];
  const ranking = new Map(
    (data.ranking || []).map((item) => [item.inviter_key, item])
  );
  const events = [
    ...(data.events || []),
    ...(data.unattributed_events || []),
  ];
  for (const event of events) {
    const inviter = ranking.get(event.inviter_key) || {};
    rows.push([
      inviter.rank || "",
      inviter.inviter_name || event.inviter_name_raw || "",
      inviter.inviter_username || "",
      event.inviter_identity_status || "",
      inviter.unique_invitee_count || 0,
      event.invitee_name_raw || "",
      event.invitee_username || "",
      event.invitee_identity_status || "",
      event.time || "",
      ({
        direct: "直接邀请",
        qr: "二维码",
        unattributed_qr: "来源不明扫码",
      })[event.method] || event.method,
      event.raw_text || "",
    ]);
  }
  const quote = (value) => `"${String(value).replaceAll('"', '""')}"`;
  return "\ufeff" + rows.map((row) => row.map(quote).join(",")).join("\r\n");
}

function renderInviteStats(data) {
  const summary = data.summary || {};
  const scope = data.scope || {};
  const cards = [
    ["邀请事件", summary.invite_event_count || 0],
    ["已归属", summary.attributed_event_count || 0],
    ["唯一成员", summary.unique_invitee_count || 0],
    ["来源不明", summary.unattributed_count || 0],
    ["身份待确认", summary.unresolved_identity_count || 0],
    ["未解析", summary.unparsed_count || 0],
  ];
  const scopeHtml = `
    <div class="invite-scope">
      <div>
        <span>统计群聊</span>
        <strong>${escapeHtml(data.chat || "")}</strong>
        <code>${escapeHtml(data.username || "")}</code>
      </div>
      <div>
        <span>可见系统消息范围</span>
        <strong>${escapeHtml(scope.first_visible_system_time || "无记录")}
          <i>→</i> ${escapeHtml(scope.last_visible_system_time || "无记录")}</strong>
      </div>
    </div>`;
  const cardHtml = `<div class="invite-summary">${
    cards.map(([label, value], index) => `
      <div class="invite-summary-card" style="--card-index:${index}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
      </div>`).join("")
  }</div>`;
  const rows = (data.ranking || []).map((item) => `
    <details class="invite-rank-row">
      <summary>
        <span class="invite-rank">#${escapeHtml(item.rank)}</span>
        <span class="invite-person">
          <strong>${escapeHtml(item.inviter_name)}</strong>
          <code>${escapeHtml(item.inviter_username || "身份待确认")}</code>
        </span>
        <b>${escapeHtml(item.unique_invitee_count)} 人</b>
        <span class="invite-method-counts">
          直接 ${escapeHtml(item.direct_count)} · 二维码 ${escapeHtml(item.qr_count)}
        </span>
      </summary>
      <div class="invitee-list">${
        (item.invitees || []).map((invitee) => `
          <div>
            <strong>${escapeHtml(invitee.name)}</strong>
            ${invitee.username ? `<code>${escapeHtml(invitee.username)}</code>` : ""}
            <span>${escapeHtml(invitee.time)} ·
              ${invitee.method === "direct" ? "直接邀请" : "二维码"}</span>
          </div>
        `).join("") || "没有明细"
      }</div>
    </details>`).join("");
  const tableHtml = `
    <div class="invite-section-heading">
      <span>LEADERBOARD</span><h3>拉新排行榜</h3>
      <details class="invite-rules">
        <summary aria-label="查看邀请统计显示规则">ⓘ 显示说明</summary>
        <div class="invite-rules-card">
          <h4>如何理解这里的邀请统计</h4>
          <p class="invite-rules-lead">昵称是显示信息，微信 ID 才是身份依据。相同微信 ID 的不同昵称会合并统计，但历史明细会尽量保留当时看到的名称。</p>
          <ul>
            <li><b>身份合并</b><span>相同稳定微信 ID 视为同一个人；昵称变化不会拆成多个排行榜身份。</span></li>
            <li><b>历史昵称</b><span>系统消息可能保存当时昵称，Contact/ext_buffer 可能保存当前昵称，所以“全部邀请关系”里同一微信 ID 可能出现不同显示名。</span></li>
            <li><b>唯一人数</b><span>排行榜按唯一被邀请人计数。同一成员重复出现不会重复累计；存在多次入群 episode 时保留完整明细，并以最新有效关系确定唯一归属。</span></li>
            <li><b>时间未知</b><span>只有可靠事件时间才显示具体时间。仅从 Contact/ext_buffer 恢复且没有可靠入群时间的关系会明确标记“时间未知”，不会猜测时间。</span></li>
            <li><b>数据来源</b><span>系统消息属于历史事件证据；Contact/ext_buffer 主要补充当前成员与邀请人关系。archive-ready 时 archive 保持权威，实时 metadata 只用于补充与冲突检查。</span></li>
            <li><b>当前与历史成员</b><span>current 表示当前仍在群内；departed 表示历史上曾在群内但已离开。当前 metadata 不会反向否定已经结束的历史 episode。</span></li>
            <li><b>冲突标记</b><span>如果权威历史记录与当前 metadata 的邀请人不一致，系统保留权威历史结果并明确标记 metadata conflict，而不是静默覆盖。</span></li>
          </ul>
          <p class="invite-rules-note"><b>查询行为：</b>查询不会自动开启持续追踪，也不会仅因为查看统计就把群聊纳入 archive。</p>
        </div>
      </details>
    </div>
    <div class="invite-ranking-table">${
      rows || '<div class="empty">当前可见范围没有邀请记录。</div>'
    }</div>`;
  const eventRows = (data.events || []).map((event) => `
    <tr>
      <td>${escapeHtml(event.time)}</td>
      <td><strong>${escapeHtml(event.inviter_name_raw)}</strong>
        ${event.inviter_username ? `<code>${escapeHtml(event.inviter_username)}</code>` : ""}</td>
      <td><strong>${escapeHtml(event.invitee_name_raw)}</strong>
        ${event.invitee_username ? `<code>${escapeHtml(event.invitee_username)}</code>` : ""}</td>
      <td><span class="method-badge ${event.method}">${
        event.method === "direct" ? "直接邀请" : "二维码"
      }</span></td>
      <td>${event.inviter_identity_status === "resolved" &&
             event.invitee_identity_status === "resolved"
             ? '<span class="identity-ok">已确认</span>'
             : '<span class="identity-pending">身份待确认</span>'}</td>
    </tr>`).join("");
  const detailsHtml = `
    <div class="invite-section-heading">
      <span>AUDIT TRAIL</span><h3>全部邀请关系</h3>
    </div>
    <div class="invite-detail-scroll">
      <table class="invite-detail-table">
        <thead><tr><th>时间</th><th>邀请者</th><th>被邀请者</th><th>方式</th><th>身份</th></tr></thead>
        <tbody>${eventRows || '<tr><td colspan="5">没有明细</td></tr>'}</tbody>
      </table>
    </div>`;
  const issueItems = [
    ...(data.unattributed_events || []).map((event) => ({
      label: "来源不明",
      time: event.time,
      text: event.raw_text,
    })),
    ...(data.unparsed_messages || []).map((item) => ({
      label: "未解析",
      time: item.time,
      text: item.raw_text,
    })),
  ];
  const issuesHtml = issueItems.length ? `
    <div class="invite-section-heading">
      <span>REVIEW QUEUE</span><h3>待核查记录</h3>
    </div>
    <div class="invite-issues">${issueItems.map((item) => `
      <div><b>${escapeHtml(item.label)}</b>
      <span>${escapeHtml(item.time)}</span>
      <p>${escapeHtml(item.text)}</p></div>
    `).join("")}</div>` : "";
  return `<div class="invite-dashboard">${
    scopeHtml + cardHtml + tableHtml + detailsHtml + issuesHtml
  }</div>`;
}

function isSafeImageUrl(url) {
  return /^https?:\/\//i.test(url) || /^data:image\//i.test(url) || /^\/api\/media\?path=/i.test(url);
}

function mediaUrl(media, { allowRemote = true } = {}) {
  if (!media || typeof media !== "object") return "";
  const isRemote = /^https?:\/\//i.test(media.url || "");
  if (media.url && isSafeImageUrl(media.url) && (allowRemote || !isRemote)) return media.url;
  if (media.path) return `/api/media?path=${encodeURIComponent(media.path)}`;
  return "";
}

function renderAvatar(item, label, { allowRemote = true } = {}) {
  const url = item?.sender_avatar_url || item?.avatar_url || item?.chat_avatar_url || "";
  const isRemote = /^https?:\/\//i.test(url);
  const initial = String(label || "W").trim().slice(0, 1).toUpperCase() || "W";
  const src = url && isSafeImageUrl(url) && (allowRemote || !isRemote)
    ? avatarProxyUrl(url)
    : "";
  return `<span class="message-avatar avatar-shell">
    <span class="avatar-fallback avatar-shell-fallback" aria-hidden="true">${escapeHtml(initial)}</span>
    ${src ? `<img class="avatar-img" src="${escapeHtml(src)}" alt="${escapeHtml(label || "头像")}" loading="lazy" data-avatar-image>` : ""}
  </span>`;
}

function renderMessageMedia(item, { allowRemote = true } = {}) {
  const media = item?.media;
  if (!media || typeof media !== "object") return "";
  const src = mediaUrl(media, { allowRemote });
  const label = media.kind === "sticker" ? "表情" : (TYPE_LABELS[media.kind] || "媒体");
  const meta = [
    media.filename,
    media.md5 ? `md5: ${media.md5}` : "",
    media.exists === false ? "文件不存在" : "",
  ].filter(Boolean).join(" · ");
  if ((media.kind === "image" || media.kind === "sticker") && src) {
    return `<figure class="message-media">
      <img src="${escapeHtml(src)}" alt="${escapeHtml(label)}" loading="lazy" referrerpolicy="no-referrer">
      ${meta ? `<figcaption>${escapeHtml(meta)}</figcaption>` : ""}
    </figure>`;
  }
  return `<div class="media-chip">${escapeHtml(label)}${meta ? ` · ${escapeHtml(meta)}` : ""}</div>`;
}

function renderChatMessages(items, {
  allowRemoteAvatars = true,
  allowRemoteMedia = true,
} = {}) {
  if (!items.length) return `<div class="empty">没有消息。</div>`;
  return `<div class="chat-list">${items.map((item) => {
    const sender = item.sender === "me" ? "我" : (item.sender || item.chat || "消息");
    const type = TYPE_LABELS[item.type] || item.type_label || item.type || "消息";
    return `<article class="message-row ${item.is_self ? "mine" : ""}">
      ${renderAvatar(item, sender, { allowRemote: allowRemoteAvatars })}
      <div class="message-main">
        <div class="message-meta">
          <strong>${escapeHtml(sender)}</strong>
          <span>${escapeHtml(item.time || "")}</span>
          <em>${escapeHtml(type)}</em>
        </div>
        <div class="message-bubble">
          <p>${escapeHtml(item.text || "")}</p>
          ${renderMessageMedia(item, { allowRemote: allowRemoteMedia })}
        </div>
      </div>
    </article>`;
  }).join("")}</div>`;
}

function renderArray(items) {
  if (items.length === 0) return `<div class="empty">没有结果。</div>`;
  return `<div class="result-grid">${items.map((item) => {
    if (typeof item === "string") {
      return `<div class="item-row">${escapeHtml(item)}</div>`;
    }
    if (!item || typeof item !== "object") {
      return `<div class="item-row">${escapeHtml(formatScalar(item))}</div>`;
    }
    const primary = item.chat || item.display_name || item.nick_name || item.summary || item.name || item.username || item.id || "记录";
    const typeMeta = TYPE_LABELS[item.type] || item.msg_type || item.type || "";
    const meta = [item.time, typeMeta, item.is_group ? "群聊" : "", item.unread ? `${item.unread} 条未读` : ""]
      .filter(Boolean).join(" · ");
    return `<article class="item-row">
      <strong>${escapeHtml(primary)}</strong>
      ${meta ? `<p class="meta">${escapeHtml(meta)}</p>` : ""}
      ${renderKeyValue(item)}
    </article>`;
  }).join("")}</div>`;
}

function renderData(data) {
  if (data && typeof data === "object" &&
      Array.isArray(data.ranking) && data.summary &&
      Array.isArray(data.events)) {
    return renderInviteStats(data);
  }
  if (data && typeof data === "object" && "hourly" in data && "total" in data) {
    return renderStats(data);
  }
  if (data && typeof data === "object" && Array.isArray(data.message_items)) {
    const rest = Object.fromEntries(Object.entries(data).filter(([key]) => key !== "messages" && key !== "message_items"));
    return `${Object.keys(rest).length ? `<div class="item-row">${renderKeyValue(rest)}</div>` : ""}${renderChatMessages(data.message_items)}`;
  }
  const array = firstArray(data);
  if (array) {
    const rest = Array.isArray(data) ? null : Object.fromEntries(Object.entries(data).filter(([, value]) => !Array.isArray(value)));
    return `${rest && Object.keys(rest).length ? `<div class="item-row">${renderKeyValue(rest)}</div>` : ""}${renderArray(array)}`;
  }
  if (data && typeof data === "object") {
    return `<div class="item-row">${renderKeyValue(data)}</div>`;
  }
  return `<pre>${escapeHtml(formatScalar(data))}</pre>`;
}

function buildResultState(payload, resultMode = "") {
  const output = payload.stdout || payload.stderr || payload.error || "";
  const isSummaryData = resultMode === "summary" &&
    payload.data && Array.isArray(payload.data.message_items);
  const text = isSummaryData ? "" : (payload.data ? JSON.stringify(payload.data, null, 2) : output);
  const state = {
    className: `result ${payload.ok ? "" : "error"}`.trim(),
    html: "",
    commandPreview: payload.command ? payload.command.join(" ") : "请求失败",
    lastText: isSummaryData ? "" : (text || JSON.stringify(payload, null, 2)),
    lastKeyText: keyCopyText(payload),
    lastDownload: null,
    copyData: null,
  };

  if (!payload.ok) {
    state.html = `<pre>${escapeHtml(output || payload.error || "操作失败")}</pre>`;
    return state;
  }

  if (payload.data) {
    if (isSummaryData) {
      const summaryData = {
        chat: payload.data.chat || "",
        username: payload.data.username || "",
        avatar_url: payload.data.avatar_url || "",
        start_time: payload.data.start_time || null,
        end_time: payload.data.end_time || null,
        message_items: payload.data.message_items,
      };
      state.html = renderSummaryResult(summaryData);
      state.lastText = "";
      state.lastKeyText = "";
      state.copyData = summaryData;
      state.commandPreview = `聊天记录 · ${summaryData.chat || summaryData.username || "会话"} · ${
        summaryData.start_time || "最早"
      } 至 ${summaryData.end_time || "最新"}`;
      return state;
    }
    state.html = renderData(payload.data);
    if (payload.command?.[1] === "invite-stats") {
      state.lastDownload = {
        text: inviteCsv(payload.data),
        filename: "wechat-invite-stats.csv",
      };
    }
    return state;
  }

  state.html = `<pre>${escapeHtml(output)}</pre>`;
  if (payload.command?.[1] === "export" && output) {
    state.lastDownload = {
      text: output,
      filename: payload.command.includes("txt") ? "wechat-export.txt" : "wechat-export.md",
    };
  }
  return state;
}

function setResult(payload, screenId = currentScreenId, resultMode = "") {
  const state = buildResultState(payload, resultMode);
  screenResultStates.set(screenId, state);
  if (currentScreenId === screenId) {
    applyResultState(state);
  }
}

async function runForm(form) {
  if (!form.reportValidity()) return;
  const screenId = form.closest(".screen")?.id || currentScreenId;
  const resultMode = form.dataset.resultMode || "";
  const requestVersion = (screenRequestVersions.get(screenId) || 0) + 1;
  screenRequestVersions.set(screenId, requestVersion);
  const payload = readForm(form);
  const validationError = resultMode === "summary"
    ? validateSummaryPayload(payload.params)
    : validatePayload(payload);
  if (validationError) {
    const errorState = {
      className: "result error",
      html: `<pre>${escapeHtml(validationError)}</pre>`,
      commandPreview: "表单校验失败",
      lastText: validationError,
      lastKeyText: "",
      lastDownload: null,
      copyData: null,
    };
    screenResultStates.set(screenId, errorState);
    if (currentScreenId === screenId) applyResultState(errorState);
    return;
  }
  const loadingState = {
    className: "result empty",
    html: "执行中...",
    commandPreview: `${payload.command} ${JSON.stringify(payload.params)}`,
    lastText: "",
    lastKeyText: "",
    lastDownload: null,
    copyData: null,
  };
  screenResultStates.set(screenId, loadingState);
  if (currentScreenId === screenId) applyResultState(loadingState);
  const requestPayload = resultMode === "summary"
    ? { ...payload, response_mode: "summary" }
    : payload;
  let response;
  try {
    response = await fetchJson("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPayload),
    });
  } catch (error) {
    if (screenRequestVersions.get(screenId) !== requestVersion) return;
    showTransientError(error, screenId);
    return;
  }
  if (screenRequestVersions.get(screenId) !== requestVersion) return;
  setResult(response, screenId, resultMode);
  if (payload.command === "init") {
    try {
      await refreshStatus(() => screenRequestVersions.get(screenId) === requestVersion);
      if (screenRequestVersions.get(screenId) !== requestVersion) return;
      await refreshAccountData(() => screenRequestVersions.get(screenId) === requestVersion);
      if (screenRequestVersions.get(screenId) !== requestVersion) return;
    } catch (error) {
      if (screenRequestVersions.get(screenId) !== requestVersion) return;
      showTransientError(error, screenId);
    }
  }
}

async function runAiPackage() {
  const form = document.querySelector("#history .summary-form");
  if (!form || !form.reportValidity()) return;
  const screenId = "history";
  const params = readForm(form).params;
  const validationError = validateSummaryPayload(params);
  if (validationError) {
    showTransientError(new Error(validationError), screenId);
    return;
  }
  const requestVersion = (screenRequestVersions.get(screenId) || 0) + 1;
  screenRequestVersions.set(screenId, requestVersion);
  const originalHtml = aiPackageButton.innerHTML;
  aiPackageButton.disabled = true;
  aiPackageButton.innerHTML = "<span>正在准备资料包…</span><small>首次语音识别会下载离线模型，请稍候</small>";
  const loadingState = {
    className: "result empty package-loading",
    html: "<div class=\"package-loading-card\"><b>正在整理聊天与素材</b><span>合并转发会递归展开，图片与表情会转为真实文件，语音会解码并离线识别。</span></div>",
    commandPreview: `AI 资料包 · ${params.chat_name} · ${params.start_time} 至 ${params.end_time}`,
    lastText: "",
    lastKeyText: "",
    lastDownload: null,
    copyData: null,
  };
  screenResultStates.set(screenId, loadingState);
  if (currentScreenId === screenId) applyResultState(loadingState);
  try {
    const payload = await fetchJson("/api/ai-package", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_name: params.chat_name,
        start_time: params.start_time,
        end_time: params.end_time,
        transcribe_voice: aiPackageTranscribe?.checked !== false,
      }),
    });
    if (screenRequestVersions.get(screenId) !== requestVersion) return;
    if (!payload.ok) {
      throw new Error(payload.error || "AI 资料包生成失败");
    }
    const readyState = {
      className: "result",
      html: renderAiPackageResult(payload),
      commandPreview: `AI 资料包 · ${payload.chat || params.chat_name} · 已准备完成`,
      lastText: payload.copy_text || "",
      lastKeyText: payload.key_copy_text || "",
      lastDownload: {
        url: payload.download_url,
        filename: payload.filename || "微信聊天-AI资料包.zip",
        label: "下载 AI 资料包",
      },
      copyData: null,
    };
    screenResultStates.set(screenId, readyState);
    if (currentScreenId === screenId) applyResultState(readyState);
  } catch (error) {
    if (screenRequestVersions.get(screenId) !== requestVersion) return;
    showTransientError(error, screenId);
  } finally {
    aiPackageButton.disabled = false;
    aiPackageButton.innerHTML = originalHtml;
  }
}

nav.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-target]");
  if (button) setScreen(button.dataset.target);
});

document.querySelectorAll(".tool-form").forEach((form) => {
  if (form.classList.contains("sns-form")) return;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runForm(form);
  });
});

snsForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!snsForm.reportValidity()) return;
  snsFilters = readSnsFilters();
  loadSnsPage({ reset: true });
});

snsResetButton?.addEventListener("click", () => {
  snsForm?.reset();
  snsFilters = {};
  loadSnsPage({ reset: true });
});

snsLoadMoreButton?.addEventListener("click", () => {
  loadSnsPage();
});

snsTimeline?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-sns-resolve-post][data-sns-resolve-media]");
  if (!button) return;
  resolveSnsVideo(button.dataset.snsResolvePost, button.dataset.snsResolveMedia);
});

aiPackageButton?.addEventListener("click", runAiPackage);

document.querySelectorAll("[data-action='refresh-status'], #refresh-status").forEach((button) => {
  button.addEventListener("click", () => {
    refreshStatus().catch((error) => showTransientError(error, "dashboard"));
  });
});

if (detectDbDirsButton) {
  detectDbDirsButton.addEventListener("click", () => {
    refreshDbDirs().catch((error) => showTransientError(error, "setup"));
  });
}

if (dbDirSelect && setupDbDirInput) {
  dbDirSelect.addEventListener("change", () => {
    if (dbDirSelect.value) {
      setupDbDirInput.value = dbDirSelect.value;
    }
  });
}

document.addEventListener("click", (event) => {
  sessionPickers.forEach((picker) => {
    if (!picker.root.contains(event.target)) {
      setSessionPickerOpen(picker, false);
    }
  });
});

const localDate = new Date();
const localToday = [
  localDate.getFullYear(),
  String(localDate.getMonth() + 1).padStart(2, "0"),
  String(localDate.getDate()).padStart(2, "0"),
].join("-");
defaultTodayInputs.forEach((input) => {
  if (!input.value) input.value = localToday;
});

document.addEventListener("error", (event) => {
  if (event.target.matches?.("img[data-avatar-image]")) {
    event.target.remove();
  }
}, true);

copyButton.addEventListener("click", async () => {
  const copyText = lastText || (lastCopyData ? formatSummaryCopy(lastCopyData) : "");
  if (!copyText) return;
  await navigator.clipboard.writeText(copyText);
  copyButton.textContent = "已复制";
  setTimeout(() => { copyButton.textContent = "复制"; }, 1000);
});

copyKeyButton.addEventListener("click", async () => {
  const copyText = lastKeyText || (lastCopyData ? formatSummaryKeyCopy(lastCopyData) : "");
  if (!copyText) return;
  await navigator.clipboard.writeText(copyText);
  copyKeyButton.textContent = "已复制精简信息";
  setTimeout(() => { copyKeyButton.textContent = "复制精简信息"; }, 1000);
});

downloadButton.addEventListener("click", () => {
  if (!lastDownload) return;
  if (lastDownload.url) {
    const link = document.createElement("a");
    link.href = lastDownload.url;
    link.download = lastDownload.filename;
    link.click();
    lastDownload = null;
    downloadButton.classList.add("hidden");
    return;
  }
  const blob = new Blob([lastDownload.text], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = lastDownload.filename;
  link.click();
  URL.revokeObjectURL(link.href);
});

refreshStatus().catch((error) => {
  initPill.textContent = "状态错误";
  initPill.className = "pill warn";
  showTransientError(error, "dashboard");
});

refreshDbDirs().catch(() => {});
loadProfile().catch(() => {
  if (profileName) profileName.textContent = "本机微信";
});
