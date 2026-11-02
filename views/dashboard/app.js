/**
 * B站监控助手仪表盘。
 *
 * 页面跑在 AstrBot 的受限 iframe 里，通过 window.AstrBotPluginView bridge
 * 调用后端注册的 Web API；bridge 不可用时退回同源 fetch（自带登录 Cookie）。
 */

const PLUGIN = "astrbot_plugin_bili_monitor";
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;

const state = {
  data: null,
  dirty: false,
  busy: false,
  editingRoom: null,
  editingPush: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ 通信 */

function endpoint(path) {
  return `/api/v1/plugins/extensions/${PLUGIN}/${path}`;
}

function unwrap(res) {
  if (res && typeof res === "object" && res.status === "ok" && "data" in res) {
    return res.data;
  }
  return res;
}

async function call(method, path, body) {
  if (bridge) {
    try {
      let res;
      if (method === "GET" && typeof bridge.apiGet === "function") {
        res = await bridge.apiGet(path);
      } else if (method === "POST" && typeof bridge.apiPost === "function") {
        res = await bridge.apiPost(path, body || {});
      } else if (typeof bridge.apiRequest === "function") {
        res = await bridge.apiRequest({ method, path, body: body || {} });
      }
      if (res !== undefined && res !== null) return unwrap(res);
    } catch (err) {
      console.warn("[bili] bridge 调用失败，改用 fetch", err);
    }
  }

  const res = await fetch(endpoint(path), {
    method,
    credentials: "include",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const raw = await res.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    data = { error: raw };
  }
  const payload = unwrap(data);
  if (!res.ok) {
    const msg = (payload && (payload.error || payload.message)) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return payload;
}

/* ------------------------------------------------------------------ 工具 */

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

function timeStr(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

let toastTimer = null;
function toast(text, kind = "") {
  const box = $("toast");
  box.textContent = text;
  box.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.className = "toast";
  }, 3200);
}

function markDirty() {
  state.dirty = true;
  $("dirty-hint").textContent = "有未保存的修改";
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["btn-save", "btn-check", "btn-test", "btn-add-uid", "btn-add-target"]) {
    const node = $(id);
    if (node) node.disabled = busy;
  }
}

/* ------------------------------------------------------------------ 渲染 */

function stat(label, value) {
  return `<div class="stat"><div class="k">${escapeHtml(label)}</div><div class="v">${escapeHtml(value)}</div></div>`;
}

function routesOf(uid) {
  const routes = (state.data && state.data.config && state.data.config.routes) || [];
  return routes
    .filter((line) => String(line).split("|")[0] === uid)
    .map((line) => String(line).split("|").slice(1).join("|"));
}

function setRoutes(uid, targets) {
  const cfg = state.data.config;
  const rest = (cfg.routes || []).filter((line) => String(line).split("|")[0] !== uid);
  cfg.routes = rest.concat(targets.map((t) => `${uid}|${t}`));
}

function renderUids(items) {
  const box = $("uid-list");
  $("uid-count").textContent = items.length ? `${items.length} 个` : "";
  if (!items.length) {
    box.innerHTML = '<div class="empty">还没有订阅，先在上面的输入框里添加 UID</div>';
    return;
  }

  box.innerHTML = items.map((it) => {
    const name = it.name || "（未获取昵称）";

    if (state.editingRoom === it.uid) {
      return `<div class="row" data-uid="${it.uid}">
        <div class="meta">
          <div class="title">${escapeHtml(name)}<span class="uid">UID ${it.uid}</span></div>
          <div class="sub">填直播间号；清空保存则恢复自动获取</div>
        </div>
        <input type="text" inputmode="numeric" class="room-input" data-uid="${it.uid}"
               value="${it.room_id || ""}" placeholder="173551" />
        <button class="btn" data-act="room-save" data-uid="${it.uid}" type="button">保存</button>
        <button class="btn ghost" data-act="room-cancel" type="button">取消</button>
      </div>`;
    }

    if (state.editingPush === it.uid) {
      const own = routesOf(it.uid);
      const chips = own.length
        ? own
            .map(
              (t) => `<span class="chip">${escapeHtml(t)}<button class="chip-x" data-act="push-del"
                data-uid="${it.uid}" data-target="${escapeAttr(t)}" type="button" title="移除">×</button></span>`
            )
            .join("")
        : '<span class="chip empty">还没有专属群，这个 UP 主不会推送</span>';
      return `<div class="row editing" data-uid="${it.uid}">
          <div class="meta">
            <div class="title">${escapeHtml(name)} 的推送群<span class="uid">UID ${it.uid}</span></div>
            <div class="sub">只发下面这些群，和别的 UP 主完全隔离</div>
          </div>
          <button class="btn" data-act="push-done" type="button">完成</button>
        </div>
        <div class="chips">${chips}</div>
        <div class="add-row">
          <input type="text" class="push-input" data-uid="${it.uid}"
                 placeholder="aiocqhttp:GroupMessage:123456789" />
          <button class="btn primary" data-act="push-add" data-uid="${it.uid}" type="button">添加群</button>
        </div>`;
    }

    const live = it.live === 1 ? '<span class="badge live">直播中</span>' : "";
    const manual = it.room_manual ? '<span class="badge manual">手填</span>' : "";
    const room = it.room_id ? `房间 ${it.room_id}` : "房间未获取";
    const checked = it.last_check ? timeStr(it.last_check) : "还没检查";
    const sessions = it.sessions || [];
    const pushText = sessions.length
      ? `推送 ${sessions.length} 个群${it.own_targets ? "" : "（默认）"}`
      : "未配推送群";

    return `<div class="row" data-uid="${it.uid}">
      <div class="avatar" ${it.face ? `style="background-image:url('${escapeAttr(it.face)}')"` : ""}></div>
      <div class="meta">
        <div class="title">${escapeHtml(name)}<span class="uid">UID ${it.uid}</span>${live}</div>
        <div class="sub">${escapeHtml(room)} · ${escapeHtml(pushText)} · ${escapeHtml(checked)} · 已记录 ${it.seen || 0} 条</div>
      </div>
      ${manual}
      <button class="btn ghost" data-act="push-edit" data-uid="${it.uid}" type="button">推送</button>
      <button class="btn ghost" data-act="room-edit" data-uid="${it.uid}" type="button">房间号</button>
      <button class="btn danger" data-act="uid-del" data-uid="${it.uid}" type="button">移除</button>
    </div>`;
  }).join("");
}

function renderTargets(targets) {
  const box = $("target-list");
  $("target-count").textContent = targets.length ? `${targets.length} 个` : "";
  if (!targets.length) {
    box.innerHTML = '<div class="empty">还没有推送目标，填一个会话 ID 或到群里发 /bili id 查</div>';
    return;
  }
  box.innerHTML = targets.map((t) => {
    const kind = t.includes("GroupMessage") ? "群聊" : t.includes("FriendMessage") ? "私聊" : "会话";
    return `<div class="row">
      <div class="meta">
        <div class="title">${escapeHtml(t)}</div>
        <div class="sub">${kind}</div>
      </div>
      <button class="btn danger" data-act="target-del" data-target="${escapeAttr(t)}" type="button">移除</button>
    </div>`;
  }).join("");
}

function renderBanner(items, defaults, cfg) {
  const box = $("migrate-banner");
  const missing = items.filter((i) => !i.own_targets);
  const strict = cfg.strict_isolation !== false;
  const show = strict && defaults.length > 0 && missing.length > 0;
  box.hidden = !show;
  if (!show) return;
  box.querySelector("[data-banner-text]").textContent =
    `${missing.length} 个 UP 主还没有专属推送群，严格隔离下它们不会推送。`;
}

function setValueIfIdle(id, value) {
  const node = $(id);
  if (node && document.activeElement !== node) node.value = value;
}

function render() {
  const data = state.data;
  if (!data) return;
  const cfg = data.config || {};
  const items = data.items || [];
  const targets = cfg.push_targets || [];

  const pill = $("run-pill");
  pill.textContent = data.running ? "运行中" : "已停止";
  pill.className = `pill ${data.running ? "on" : "off"}`;

  $("subtitle").textContent =
    `${items.length} 个 UP 主 · ${targets.length} 个推送目标 · 更新于 ${timeStr(data.updated_at)}`;

  $("stats").innerHTML = [
    stat("订阅 UP 主", items.length),
    stat("已配专属群", items.filter((i) => i.own_targets).length),
    stat("正在直播", items.filter((i) => i.live === 1).length),
    stat("轮询间隔", `${cfg.check_interval || 0}s`),
  ].join("");

  renderUids(items);
  renderTargets(targets);
  renderBanner(items, targets, cfg);

  $("cfg-enable").checked = !!cfg.enable;
  $("cfg-live").checked = !!cfg.notify_live;
  $("cfg-image").checked = !!cfg.image_mode;
  $("cfg-strict").checked = cfg.strict_isolation !== false;
  setValueIfIdle("cfg-interval", cfg.check_interval ?? 90);
  setValueIfIdle("cfg-quiet", cfg.quiet_hours || "");

  const parts = [];
  parts.push(cfg.cookie_set ? "已配置 Cookie（留空表示不修改）" : "未配置 Cookie");
  parts.push(cfg.proxy_set ? "已配置代理" : "未配置代理");
  $("cookie-hint").textContent =
    `${parts.join(" · ")}。机房 IP 常见 412 request was banned：先扫码登录，仍不行就配代理。`;

  const account = cfg.account || {};
  if (account.logged_in) {
    $("account-line").textContent =
      `当前登录：${account.name || "未知"}（UID ${account.mid || "?"}）——机器人用这个账号的身份访问 B 站，仅取 Cookie，不做任何写操作。`;
  } else if (cfg.cookie_set) {
    $("account-line").textContent = "已配置 Cookie，但查不到账号信息（可能已失效），建议重新扫码登录。";
  } else {
    $("account-line").textContent =
      "未登录。扫码仅用于获取 Cookie（读公开接口用），不会有发布/点赞/关注等操作；建议用小号。";
  }

  $("dirty-hint").textContent = state.dirty ? "有未保存的修改" : "";
}

/* ------------------------------------------------------------------ 动作 */

function addUid() {
  if (!state.data) return toast("还没加载到数据，先点刷新", "err");
  const input = $("uid-input");
  const uid = (input.value || "").replace(/[^0-9]/g, "");
  if (!uid) return toast("UID 只能是数字", "err");
  const cfg = state.data.config;
  if ((cfg.uids || []).includes(uid)) return toast("这个 UID 已经在列表里了", "err");

  cfg.uids = [...(cfg.uids || []), uid];
  state.data.items = [...(state.data.items || []), {
    uid, name: "", face: "", room_id: 0, room_manual: false,
    live: 0, live_start: 0, peak: 0, last_check: 0, seen: 0,
    sessions: [], own_targets: false,
  }];
  input.value = "";
  markDirty();
  render();
}

function removeUid(uid) {
  if (!state.data) return toast("还没加载到数据，先点刷新", "err");
  const cfg = state.data.config;
  cfg.uids = (cfg.uids || []).filter((x) => x !== uid);
  cfg.room_overrides = (cfg.room_overrides || []).filter((x) => !x.startsWith(`${uid}:`));
  cfg.routes = (cfg.routes || []).filter((line) => String(line).split("|")[0] !== uid);
  state.data.items = (state.data.items || []).filter((i) => i.uid !== uid);
  markDirty();
  render();
}

function saveRoom(uid) {
  if (!state.data) return;
  const input = document.querySelector(`.room-input[data-uid="${uid}"]`);
  const room = (input?.value || "").replace(/[^0-9]/g, "");
  const cfg = state.data.config;

  cfg.room_overrides = (cfg.room_overrides || []).filter((x) => !x.startsWith(`${uid}:`));
  if (room && Number(room) > 0) cfg.room_overrides.push(`${uid}:${Number(room)}`);

  const item = (state.data.items || []).find((i) => i.uid === uid);
  if (item) {
    item.room_id = Number(room) || 0;
    item.room_manual = Boolean(room && Number(room) > 0);
  }
  state.editingRoom = null;
  markDirty();
  render();
}

function addTarget() {
  if (!state.data) return toast("还没加载到数据，先点刷新", "err");
  const input = $("target-input");
  const value = (input.value || "").trim();
  if (!value) return;
  const cfg = state.data.config;
  if ((cfg.push_targets || []).includes(value)) return toast("这个会话已经在列表里了", "err");
  cfg.push_targets = [...(cfg.push_targets || []), value];
  input.value = "";
  markDirty();
  render();
}

async function saveAll() {
  if (!state.data) return toast("还没加载到数据，先点刷新", "err");
  const cfg = state.data.config;
  setBusy(true);
  try {
    const res = await call("POST", "config", {
      uids: cfg.uids || [],
      push_targets: cfg.push_targets || [],
      routes: cfg.routes || [],
      strict_isolation: $("cfg-strict").checked,
      room_overrides: cfg.room_overrides || [],
      enable: $("cfg-enable").checked,
      notify_live: $("cfg-live").checked,
      image_mode: $("cfg-image").checked,
      check_interval: Number($("cfg-interval").value) || 90,
      quiet_hours: $("cfg-quiet").value.trim(),
      cookie: $("cfg-cookie").value.trim(),
      proxy: $("cfg-proxy").value.trim(),
    });
    if (res && res.overview) state.data = res.overview;
    state.dirty = false;
    $("cfg-cookie").value = "";
    $("cfg-proxy").value = "";
    render();
    toast("已保存", "ok");
  } catch (err) {
    toast(err.message || "保存失败", "err");
  } finally {
    setBusy(false);
  }
}

async function checkNow() {
  setBusy(true);
  try {
    const res = await call("POST", "check", {});
    const results = res.results || [];
    const added = results.reduce((n, r) => n + (r.new || 0), 0);
    const failed = results.filter((r) => !r.ok);
    if (res.overview) state.data = res.overview;
    render();
    if (failed.length) {
      toast(`检查完成，${failed.length} 个失败：${failed[0].error || "未知错误"}`, "err");
    } else {
      toast(`检查完成，新记录 ${added} 条`, "ok");
    }
  } catch (err) {
    toast(err.message || "检查失败", "err");
  } finally {
    setBusy(false);
  }
}

async function testPush() {
  setBusy(true);
  try {
    const res = await call("POST", "test-push", {});
    toast(`已发送 ${res.sent || 0} 条测试消息`, "ok");
  } catch (err) {
    toast(err.message || "发送失败", "err");
  } finally {
    setBusy(false);
  }
}

async function refresh(silent) {
  if (state.busy) return;
  if (silent && state.dirty) return; // 别覆盖用户还没保存的修改
  try {
    const data = await call("GET", "overview");
    state.data = data;
    render();
  } catch (err) {
    if (!silent) toast(err.message || "加载失败", "err");
    if (!state.data) {
      $("subtitle").textContent = `加载失败：${err.message || err}`;
      $("stats").innerHTML =
        '<div class="stat"><div class="k">状态</div><div class="v" style="font-size:14px">接口不可用</div></div>';
    }
  }
}

/* ------------------------------------------------------------------ 扫码登录 */

let loginTimer = null;

function stopLogin(message) {
  if (loginTimer) clearTimeout(loginTimer);
  loginTimer = null;
  $("btn-login-cancel").hidden = true;
  if (message) $("login-status").textContent = message;
}

async function startLogin() {
  if (!state.data) return toast("还没加载到数据，先点刷新", "err");
  $("login-panel").hidden = false;
  $("btn-login-cancel").hidden = false;
  $("login-status").textContent = "正在获取二维码…";
  try {
    const res = await call("POST", "login/qr", {});
    if (res.image) {
      $("login-qr").src = res.image;
      $("login-qr").hidden = false;
      $("login-status").textContent = "请用要给机器人使用的账号扫码（建议小号）";
    } else {
      $("login-qr").hidden = true;
      $("login-status").textContent = `二维码组件不可用，请打开：${res.url}`;
    }
    pollLogin(res.key, 0);
  } catch (err) {
    $("login-status").textContent = `获取二维码失败：${err.message}`;
    $("btn-login-cancel").hidden = true;
  }
}

function pollLogin(key, round) {
  if (loginTimer) clearTimeout(loginTimer);
  if (round > 90) {
    stopLogin("二维码已过期，请重新点击扫码登录");
    return;
  }
  loginTimer = setTimeout(async () => {
    try {
      const res = await call("POST", "login/poll", { key });
      if (res.status === 0) {
        const acc = res.account || {};
        const who = acc.name ? `${acc.name}（UID ${acc.mid}）` : "";
        stopLogin(`登录成功${who ? `：${who}` : ""}，Cookie 已保存 ✅`);
        $("login-panel").hidden = true;
        if (res.overview) state.data = res.overview;
        state.dirty = false;
        render();
        toast("登录成功，Cookie 已保存", "ok");
        return;
      }
      if (res.status === 86038) {
        stopLogin("二维码已过期，请重新扫码");
        return;
      }
      $("login-status").textContent =
        res.status === 86090 ? "已扫码，请在手机上点确认" : "等待扫码…";
      pollLogin(key, round + 1);
    } catch (err) {
      $("login-status").textContent = `查询失败：${err.message}`;
      pollLogin(key, round + 1);
    }
  }, 2000);
}

/* ------------------------------------------------------------------ 事件 */

document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;

  if (act === "uid-del") removeUid(btn.dataset.uid);
  else if (act === "push-edit") {
    state.editingRoom = null;
    state.editingPush = btn.dataset.uid;
    render();
    document.querySelector(`.push-input[data-uid="${btn.dataset.uid}"]`)?.focus();
  } else if (act === "push-done") {
    state.editingPush = null;
    render();
  } else if (act === "push-add") {
    const uid = btn.dataset.uid;
    const input = document.querySelector(`.push-input[data-uid="${uid}"]`);
    const target = (input?.value || "").trim();
    if (!target) return toast("先填一个会话 ID", "err");
    const own = routesOf(uid);
    if (own.includes(target)) return toast("这个群已经加过了", "err");
    setRoutes(uid, [...own, target]);
    const item = (state.data.items || []).find((i) => i.uid === uid);
    if (item) {
      item.sessions = [...own, target];
      item.own_targets = true;
    }
    markDirty();
    render();
    document.querySelector(`.push-input[data-uid="${uid}"]`)?.focus();
  } else if (act === "push-del") {
    const uid = btn.dataset.uid;
    const target = btn.dataset.target;
    const left = routesOf(uid).filter((t) => t !== target);
    setRoutes(uid, left);
    const item = (state.data.items || []).find((i) => i.uid === uid);
    if (item) {
      const defaults = state.data.config.push_targets || [];
      const strict = state.data.config.strict_isolation !== false;
      item.own_targets = left.length > 0;
      item.sessions = left.length ? left : strict ? [] : [...defaults];
    }
    markDirty();
    render();
  } else if (act === "apply-defaults") {
    const defaults = state.data.config.push_targets || [];
    if (!defaults.length) return toast("默认推送是空的", "err");
    for (const item of state.data.items || []) {
      if (item.own_targets) continue;
      setRoutes(item.uid, defaults);
      item.sessions = [...defaults];
      item.own_targets = true;
    }
    markDirty();
    render();
    toast("已复制，记得点保存", "ok");
  } else if (act === "room-edit") {
    state.editingRoom = btn.dataset.uid;
    render();
    document.querySelector(`.room-input[data-uid="${btn.dataset.uid}"]`)?.focus();
  } else if (act === "room-save") saveRoom(btn.dataset.uid);
  else if (act === "room-cancel") {
    state.editingRoom = null;
    render();
  } else if (act === "target-del") {
    if (!state.data) return;
    const cfg = state.data.config;
    cfg.push_targets = (cfg.push_targets || []).filter((x) => x !== btn.dataset.target);
    markDirty();
    render();
  }
});

$("btn-add-uid").addEventListener("click", addUid);
$("uid-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addUid();
});
$("btn-add-target").addEventListener("click", addTarget);
$("target-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addTarget();
});
$("btn-save").addEventListener("click", saveAll);
$("btn-check").addEventListener("click", checkNow);
$("btn-test").addEventListener("click", testPush);
$("btn-refresh").addEventListener("click", () => refresh(false));
$("btn-login").addEventListener("click", startLogin);
$("btn-login-cancel").addEventListener("click", () => {
  stopLogin();
  $("login-panel").hidden = true;
});

for (const id of ["cfg-enable", "cfg-live", "cfg-image", "cfg-strict"]) {
  $(id).addEventListener("change", markDirty);
}
for (const id of ["cfg-interval", "cfg-quiet", "cfg-cookie", "cfg-proxy"]) {
  $(id).addEventListener("input", markDirty);
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const input = e.target.closest && e.target.closest(".push-input");
  if (!input) return;
  e.preventDefault();
  document.querySelector(`[data-act="push-add"][data-uid="${input.dataset.uid}"]`)?.click();
});

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (!state.busy) saveAll();
  }
});

async function init() {
  if (bridge && typeof bridge.ready === "function") {
    try {
      await bridge.ready();
    } catch (err) {
      console.warn("[bili] bridge.ready 失败", err);
    }
  }
  await refresh(false);
  setInterval(() => refresh(true), 20000);
}

init();
