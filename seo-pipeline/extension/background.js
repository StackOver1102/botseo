// Service worker: long-poll pipeline lấy task → chạy trong tab ChatGPT/Gemini (dùng lại tab từ pool) → gửi kết quả.
// Mỗi hãng chạy song song tối đa `parallel[provider]` tab (mặc định 2), tránh bị web UI giới hạn tốc độ.

const PROVIDERS = {
  openai: { newChatUrl: "https://chatgpt.com/", match: "https://chatgpt.com/*" },
  gemini: { newChatUrl: "https://gemini.google.com/app", match: "https://gemini.google.com/*" },
};
const DEFAULTS = { apiBase: "http://localhost:8000", token: "", clientId: "", enabled: { openai: true, gemini: true }, pollSeconds: 30, debug: false };
const TASK_TIMEOUT_MS = 12 * 60 * 1000; // nhỏ hơn task_lease_seconds (900s) ở server

async function getSettings() {
  const s = await chrome.storage.local.get(DEFAULTS);
  if (!s.clientId) {
    s.clientId = `ext-${crypto.randomUUID()}`;
    await chrome.storage.local.set({ clientId: s.clientId });
  }
  return s;
}

async function api(settings, path, init = {}) {
  const res = await fetch(`${settings.apiBase}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Extension-Token": settings.token,
      "X-Client-Id": settings.clientId,
      ...(init.headers || {}),
    },
  });
  if (res.status === 204) return null;
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status} ${await res.text()}`);
  return res.headers.get("content-type")?.includes("json") ? res.json() : null;
}

async function log(entry) {
  const { logs = [] } = await chrome.storage.local.get("logs");
  logs.unshift({ at: new Date().toISOString(), ...entry });
  await chrome.storage.local.set({ logs: logs.slice(0, 50) });
}

// ---------- tab ----------
// Mọi tab extension mở đều được ghi vào storage.local.ownTabs, để đóng được cả tab mồ côi
// (tab còn mở khi extension bị reload hoặc Chrome tắt giữa chừng).

async function trackTab(tabId, add) {
  const { ownTabs = [] } = await chrome.storage.local.get("ownTabs");
  const next = add ? [...new Set([...ownTabs, tabId])] : ownTabs.filter((id) => id !== tabId);
  await chrome.storage.local.set({ ownTabs: next });
}

async function closeTab(tabId) {
  await chrome.tabs.remove(tabId).catch(() => {}); // tab có thể đã bị người dùng đóng
  await trackTab(tabId, false);
}

async function closeOrphanTabs() {
  const { ownTabs = [], keptTabs = {} } = await chrome.storage.local.get(["ownTabs", "keptTabs"]);
  const kept = new Set(Object.values(keptTabs)); // tab lỗi giữ lại cho debug: để người dùng tự xem/đóng
  for (const id of ownTabs) if (!kept.has(id)) await closeTab(id);
}

// Debug: giữ lại tab của task lỗi để xem, mỗi hãng tối đa 1 tab (giữ tab mới thì đóng tab cũ)
async function keepFailedTab(provider, tabId) {
  const { keptTabs = {} } = await chrome.storage.local.get("keptTabs");
  if (keptTabs[provider] && keptTabs[provider] !== tabId) await closeTab(keptTabs[provider]);
  await chrome.storage.local.set({ keptTabs: { ...keptTabs, [provider]: tabId } });
}

// Chờ tab tải xong, tối đa timeoutMs: sự kiện "complete" có thể không tới (điều hướng tới đúng URL đang mở,
// trang SPA tự đổi route) — không có giới hạn thì extension treo trước khi kịp gửi prompt.
function waitComplete(tabId, timeoutMs = 30000) {
  return new Promise((resolve) => {
    const done = () => { chrome.tabs.onUpdated.removeListener(listener); clearTimeout(timer); resolve(); };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") done(); };
    const timer = setTimeout(done, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ---------- pool tab ----------
// Giữ tab đã mở để dùng lại cho task sau: chỉ điều hướng sang chat mới (trang đã cache, nhanh hơn tạo tab mới),
// tab rảnh quá `idleCloseSeconds` (mặc định 180s) thì đóng để không tốn RAM.
const pool = { openai: [], gemini: [] }; // [{ tabId, busy, lastUsed }]
const DEFAULT_IDLE_CLOSE_S = 180;

async function acquireTab(provider, url, debug) {
  const target = url || PROVIDERS[provider].newChatUrl;
  for (const slot of pool[provider]) {
    if (slot.busy) continue;
    const alive = await chrome.tabs.get(slot.tabId).catch(() => null);
    if (!alive) { pool[provider] = pool[provider].filter((x) => x !== slot); continue; }
    slot.busy = true;
    const done = waitComplete(slot.tabId);
    try {
      // Cùng URL thì update không tải lại trang (hội thoại cũ vẫn còn) → reload; khác URL thì điều hướng
      if (alive.url === target) await chrome.tabs.reload(slot.tabId);
      else await chrome.tabs.update(slot.tabId, { url: target });
    } catch (e) {
      // Tab vừa bị đóng hoặc URL không hợp lệ: bỏ slot (không để kẹt busy mãi) rồi thử slot khác / mở tab mới
      pool[provider] = pool[provider].filter((x) => x !== slot);
      await closeTab(slot.tabId);
      continue;
    }
    await done;
    return slot;
  }
  // Mỗi tab làm việc nằm trong MỘT CỬA SỔ RIÊNG và là tab đang hiện của cửa sổ đó: tab nền (visibilityState = hidden)
  // bị Chrome hạn chế → Gemini (Quill) không nhận nội dung dán, nút Gửi không bật. Cửa sổ mở không lấy focus.
  // Debug: mở như tab thường ở cửa sổ hiện tại để dễ xem.
  let tab;
  if (debug) {
    tab = await chrome.tabs.create({ url: target, active: true });
  } else {
    const win = await chrome.windows.create({ url: target, focused: false, state: "normal", width: 1100, height: 900 });
    tab = win.tabs[0];
  }
  await trackTab(tab.id, true);
  const slot = { tabId: tab.id, busy: true, lastUsed: Date.now() };
  pool[provider].push(slot);
  await waitComplete(tab.id);
  return slot;
}

function releaseTab(slot) {
  slot.busy = false;
  slot.lastUsed = Date.now();
}

async function dropTab(provider, slot, keepForDebug) {
  pool[provider] = pool[provider].filter((x) => x !== slot);
  if (keepForDebug) await keepFailedTab(provider, slot.tabId);
  else await closeTab(slot.tabId);
}

async function reapIdleTabs() {
  const { idleCloseSeconds = DEFAULT_IDLE_CLOSE_S } = await chrome.storage.local.get("idleCloseSeconds");
  for (const provider of Object.keys(pool)) {
    for (const slot of [...pool[provider]]) {
      if (!slot.busy && Date.now() - slot.lastUsed > idleCloseSeconds * 1000) await dropTab(provider, slot, false);
    }
  }
}
setInterval(reapIdleTabs, 30000);

async function sendToTab(tabId, message, timeoutMs) {
  // Content script có thể chưa kịp khởi tạo ngay khi tab "complete": thử lại vài lần
  // Tab chạy nền tải ChatGPT có thể mất >10s: chờ content script tới ~30s
  for (let i = 0; i < 30; i++) {
    try {
      return await Promise.race([
        chrome.tabs.sendMessage(tabId, message),
        new Promise((_, reject) => setTimeout(() => reject(new Error("quá thời gian chờ trả lời")), timeoutMs)),
      ]);
    } catch (e) {
      if (!String(e.message).includes("Receiving end does not exist")) throw e;
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
  throw new Error("content script không phản hồi");
}

// ---------- vòng xử lý ----------
// Mỗi hãng một vòng long-poll: server giữ request tới 25s và trả task ngay khi có, nên task được nhận gần như tức thì
// (trước đây chờ alarm 30s). Mỗi hãng chạy song song tối đa `parallel[provider]` tab (mặc định 2).

const DEFAULT_PARALLEL = 2;
const running = { openai: 0, gemini: 0 };
const looping = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function runTask(settings, provider, task) {
  // Deep Research chờ tới ~40 phút; task thường 12 phút (server gửi timeout_seconds theo loại task)
  const timeoutMs = Math.max(TASK_TIMEOUT_MS, (task.timeout_seconds || 0) * 1000) + 60000;
  await log({ provider, task: task.id, prompt: task.prompt_id, event: "claimed" });
  let slot;
  let failed = false;
  try {
    slot = await acquireTab(provider, task.chat_url, settings.debug);
    const res = await sendToTab(slot.tabId, { type: "RUN_TASK", task }, timeoutMs);
    if (!res?.ok) throw new Error(res?.error || "content script trả lỗi");
    // Câu trả lời đã có (mất tới 10 phút để tạo): lỗi mạng/5xx thoáng qua khi gửi về thì thử lại, không bỏ kết quả.
    // 4xx (vd. 409 = task không còn thuộc client này) thì thử lại cũng vô ích.
    const resultBody = JSON.stringify({ text: res.text, model: res.model || null, source_urls: res.sourceUrls || [] });
    for (let attempt = 0; ; attempt++) {
      try {
        await api(settings, `/external/tasks/${task.id}/result`, { method: "POST", body: resultBody });
        break;
      } catch (e) {
        if (attempt >= 3 || String(e.message || e).includes("HTTP 4")) throw e;
        await sleep(2000 * (attempt + 1));
      }
    }
    await log({ provider, task: task.id, event: "done", chars: res.text.length });
  } catch (e) {
    failed = true;
    await log({ provider, task: task.id, event: "failed", error: String(e.message || e) });
    await api(settings, `/external/tasks/${task.id}/fail`, {
      method: "POST",
      body: JSON.stringify({ error: String(e.message || e).slice(0, 2000) }),
    }).catch(() => {});
  } finally {
    if (slot) {
      // Thành công: trả tab về pool cho task sau (tab rảnh lâu tự đóng). Lỗi: bỏ tab (trang có thể hỏng);
      // bật Debug thì giữ lại để xem.
      if (failed) await dropTab(provider, slot, settings.debug);
      else releaseTab(slot);
    }
  }
}

async function pollLoop(provider) {
  if (looping[provider]) return;
  looping[provider] = true;
  // Gọi API extension định kỳ để service worker MV3 không bị tắt khi đang chờ
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(), 20000);
  try {
    for (;;) {
      const settings = await getSettings();
      if (!settings.token || !settings.enabled[provider]) break;
      const max = settings.parallel?.[provider] ?? DEFAULT_PARALLEL;
      if (running[provider] >= max) { await sleep(1000); continue; }
      let task;
      const asked = Date.now();
      try {
        task = await api(settings, `/external/tasks/next?provider=${provider}&wait=25`);
      } catch (e) {
        await log({ provider, event: "error", error: String(e.message || e) });
        await sleep(10000); // API tắt / token sai: thử lại sau
        continue;
      }
      if (!task) {
        // hết 25s không có việc: hỏi lại ngay; server cũ (không long-poll) trả về tức thì thì nghỉ, tránh hỏi dồn dập
        if (Date.now() - asked < 2000) await sleep(5000);
        continue;
      }
      running[provider]++;
      runTask(settings, provider, task).finally(() => { running[provider]--; });
    }
  } finally {
    clearInterval(keepAlive);
    looping[provider] = false;
  }
}

async function tick() {
  const settings = await getSettings();
  if (!settings.token) return;
  for (const p of Object.keys(PROVIDERS)) if (settings.enabled[p]) pollLoop(p); // không chờ: vòng chạy mãi
}

async function schedule() {
  const { pollSeconds } = await getSettings();
  // Alarm chỉ còn là watchdog: service worker bị Chrome tắt thì lần alarm sau khởi động lại vòng long-poll
  chrome.alarms.create("poll", { periodInMinutes: Math.max(30, pollSeconds) / 60 });
}

// Service worker vừa khởi động (Chrome tắt nó khi rảnh): pool trong bộ nhớ đã mất, đóng các tab cũ còn treo
closeOrphanTabs();
// Reload extension / mở Chrome: task dở dang của lần trước đã mất, đóng tab nó để lại
// ---------- cookie Gemini cho proxy cục bộ ----------
// Server chạy Gemini qua proxy dùng cookie của Gemini web; cookie hết hạn thì proxy ngừng hoạt động. Extension đang ở
// trong Chrome đã đăng nhập nên gửi header Cookie của gemini.google.com về server theo chu kỳ; server chỉ dùng tới khi
// proxy báo hết hạn. Cookie chỉ gửi tới apiBase (máy cục bộ) kèm token của extension.
const COOKIE_SYNC_MINUTES = 10;

async function pushGeminiCookies() {
  const settings = await getSettings();
  if (!settings.token) return;
  // Luôn báo về server, kể cả khi không đọc được cookie: server ghi lý do vào log để biết extension kẹt ở đâu
  let cookies = [];
  let error = null;
  try { cookies = await chrome.cookies.getAll({ url: "https://gemini.google.com/" }); } catch (e) { error = String(e.message || e); }
  const names = new Set(cookies.map((c) => c.name));
  const loggedIn = names.has("__Secure-1PSID") && names.has("__Secure-1PSIDTS");
  if (!error && !loggedIn) error = `đọc được ${cookies.length} cookie nhưng thiếu cookie đăng nhập Google`;
  const header = loggedIn ? cookies.map((c) => `${c.name}=${c.value}`).join("; ") : "";
  // Tài khoản đang dùng: /u/<số>/ trên tab Gemini đang mở. Không có tab nào → null (server giữ số cũ)
  const tabs = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
  let authUser = null;
  for (const t of tabs) {
    const m = /\/u\/(\d+)\//.exec(t.url || "");
    authUser = m ? m[1] : (authUser ?? "");
    if (m) break;
  }
  try {
    const res = await api(settings, "/external/gemini-cookies", {
      method: "POST",
      body: JSON.stringify({ cookie_header: header, auth_user: authUser, error }),
    });
    if (res?.updated || error) await log({ provider: "gemini", event: "cookie_sync", healthy: res?.healthy, detail: error || res.reason });
  } catch (e) {
    // server tắt hoặc chưa có endpoint này: thử lại ở lần alarm sau
  }
}

chrome.runtime.onInstalled.addListener(() => { closeOrphanTabs(); schedule(); pushGeminiCookies(); });
chrome.runtime.onStartup.addListener(() => { closeOrphanTabs(); schedule(); pushGeminiCookies(); });
chrome.alarms.create("gemini-cookies", { periodInMinutes: COOKIE_SYNC_MINUTES });
chrome.alarms.onAlarm.addListener((a) => a.name === "gemini-cookies" && pushGeminiCookies());
chrome.tabs.onRemoved.addListener((tabId) => trackTab(tabId, false)); // người dùng tự đóng tab
chrome.alarms.onAlarm.addListener((a) => a.name === "poll" && tick());
chrome.storage.onChanged.addListener((changes) => changes.pollSeconds && schedule());
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "POLL_NOW") return false;
  tick(); // không chờ: một task có thể chạy nhiều phút, popup đóng sẽ mất phản hồi
  sendResponse({ ok: true });
  return false;
});
