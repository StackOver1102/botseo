const DEFAULTS = { apiBase: "http://localhost:8000", token: "", clientId: "", enabled: { openai: true, gemini: true }, pollSeconds: 30, debug: false, logs: [] };
const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.local.get(DEFAULTS);
  $("apiBase").value = s.apiBase;
  $("token").value = s.token;
  $("pollSeconds").value = s.pollSeconds;
  $("openai").checked = s.enabled.openai;
  $("gemini").checked = s.enabled.gemini;
  $("debug").checked = s.debug;
  $("clientId").textContent = s.clientId || "(tạo khi chạy lần đầu)";
  $("logs").textContent = s.logs.map((l) => `${l.at} ${l.provider || ""} ${l.event} ${l.task || ""} ${l.error || ""}`).join("\n");
}

async function save() {
  const apiBase = $("apiBase").value.trim().replace(/\/$/, "");
  // API không phải localhost cần xin quyền host lúc chạy
  if (!/^http:\/\/(localhost|127\.0\.0\.1)/.test(apiBase)) {
    const granted = await chrome.permissions.request({ origins: [`${new URL(apiBase).origin}/*`] });
    if (!granted) return flash("Chưa được cấp quyền truy cập API");
  }
  await chrome.storage.local.set({
    apiBase,
    token: $("token").value.trim(),
    pollSeconds: Math.max(30, Number($("pollSeconds").value) || 30),
    enabled: { openai: $("openai").checked, gemini: $("gemini").checked },
    debug: $("debug").checked,
  });
  flash("Đã lưu");
}

function flash(msg) {
  $("status").textContent = msg;
  setTimeout(() => ($("status").textContent = ""), 2500);
}

$("save").addEventListener("click", save);
$("poll").addEventListener("click", async () => {
  flash("Đang hỏi…");
  await chrome.runtime.sendMessage({ type: "POLL_NOW" });
  await load();
  flash("Xong");
});
chrome.storage.onChanged.addListener((c) => c.logs && load());
load();
