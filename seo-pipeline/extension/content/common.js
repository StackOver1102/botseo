// Chạy task trên trang chat, cố gắng không phụ thuộc cấu trúc DOM:
//  - Ô nhập: selector từ server → selector của adapter → ô soạn thảo lớn nhất đang hiện trên trang.
//  - Gửi: nút gửi nếu tìm được, không thì nhấn Enter. Dấu hiệu đã gửi: ô nhập trống lại / model bắt đầu trả lời.
//  - Câu trả lời JSON: quét trang tìm khối chữ parse được thành JSON có đủ `task.expect_keys` (theo nội dung, không theo DOM);
//    JSON đứng yên vài giây và không còn nút Stop → xong.
//  - Câu trả lời Markdown (expect_keys rỗng): dùng selector `response` và chờ nội dung đứng yên.
// Adapter theo site chỉ còn là lớp dự phòng: { input(), sendButton(), isGenerating(), lastResponse(), sent?(), modelName?() }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, { timeout = 30000, interval = 250, label = "phần tử" } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const v = fn();
    if (v) return v;
    await sleep(interval);
  }
  throw new Error(`không tìm thấy ${label} sau ${timeout / 1000}s`);
}

const visible = (el) => !!el && el.offsetParent !== null && el.getBoundingClientRect().width > 0;

function first(selectors) {
  for (const s of selectors || []) {
    try {
      const nodes = [...document.querySelectorAll(s)].filter(visible);
      if (nodes.length) return nodes[nodes.length - 1];
    } catch (_) { /* selector sai cú pháp: bỏ qua */ }
  }
  return null;
}

// Ô soạn thảo lớn nhất đang hiện (contenteditable hoặc textarea) — hầu như site chat nào cũng chỉ có một ô như vậy
function genericInput() {
  const editable = [...document.querySelectorAll("[contenteditable='true']")].filter(visible);
  const nodes = editable.length ? editable : [...document.querySelectorAll("textarea")].filter(visible);
  nodes.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return rb.width * Math.max(rb.height, 20) - ra.width * Math.max(ra.height, 20);
  });
  return nodes[0] || null;
}

function makeFinder(adapter, sel) {
  return {
    // Chỉ nhận ô đang hiển thị: giao diện mới có thể còn ô cũ bị ẩn cùng id/selector
    input: () => first(sel.input) || [adapter.input?.()].find(visible) || genericInput(),
    send: () => {
      const b = first(sel.send) || adapter.sendButton?.();
      return b && !b.disabled ? b : null;
    },
    generating: () => !!first(sel.stop) || !!adapter.isGenerating?.(),
    response: () => first(sel.response) || adapter.lastResponse?.(),
    report: () => first(sel.report),
  };
}

// ---------- bấm nút theo chữ hiển thị (ít phụ thuộc class/DOM hơn selector) ----------

const CLICKABLE = "button, [role='button'], [role='menuitem'], [role='menuitemcheckbox'], [role='menuitemradio'], [role='option'], [role='tab'], a";

function findByText(texts) {
  const wanted = (texts || []).map((t) => t.toLowerCase());
  const hits = [...document.querySelectorAll(CLICKABLE)].filter(visible).filter((el) => {
    const label = `${el.innerText || ""} ${el.getAttribute("aria-label") || ""}`.toLowerCase();
    return wanted.some((t) => label.includes(t));
  });
  // Chữ ngắn nhất = phần tử cụ thể nhất (tránh bấm nhầm khung lớn chứa cả menu)
  hits.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
  return hits[0] || null;
}

// Mô phỏng rê chuột vào giữa phần tử (menu con kiểu "More tools" chỉ mở khi hover, click không mở)
function hover(el) {
  const r = el.getBoundingClientRect();
  const at = { bubbles: true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, view: window };
  el.dispatchEvent(new PointerEvent("pointerover", at));
  el.dispatchEvent(new PointerEvent("pointerenter", { ...at, bubbles: false }));
  el.dispatchEvent(new MouseEvent("mouseover", at));
  el.dispatchEvent(new MouseEvent("mouseenter", { ...at, bubbles: false }));
  el.dispatchEvent(new PointerEvent("pointermove", at));
  el.dispatchEvent(new MouseEvent("mousemove", at));
}

async function clickByText(texts, { timeout = 5000, label } = {}) {
  const el = await waitFor(() => findByText(texts), { timeout, label: label || (texts || []).join("/") });
  hover(el);
  await sleep(300);
  el.click();
  await sleep(700);
  return el;
}

// Bật Deep Research theo đường đi menu trong config (texts.menu_path).
// Bước giữa (mở menu con): rê chuột trước, chỉ click nếu rê xong mà bước sau vẫn chưa hiện — click lại có thể đóng menu.
async function enableDeepResearch(t) {
  const path = t.menu_path || [t.tools, t.item].filter(Boolean);
  const target = path[path.length - 1];
  for (let i = 0; i < path.length - 1 && !findByText(target); i++) {
    const step = path[i], next = path[i + 1];
    if (i === 0) {
      await clickByText(step, { label: `nút "${step[0]}"` });  // nút mở menu gốc: phải click
      continue;
    }
    const el = await waitFor(() => findByText(step), { timeout: 5000, label: `mục menu "${step[0]}"` });
    hover(el);
    const opened = await waitFor(() => findByText(next), { timeout: 2000 }).catch(() => null);
    if (!opened) {
      el.click();
      await sleep(700);
    }
  }
  await clickByText(target, { label: `mục "${target[0]}"` });
  console.info("[seo-bridge] đã bật Deep Research");
}

// Dán văn bản dài vào ô soạn thảo (ProseMirror / Quill / textarea)
function describeEl(el) {
  if (!el) return "không có ô nhập";
  return `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).slice(0, 2).join(".") : ""}` +
    ` editable=${el.isContentEditable} visible=${visible(el)}`;
}

// Đưa prompt vào ô nhập, thử lần lượt nhiều cách và kiểm tra ngay sau mỗi cách (chữ đã vào ô chưa).
// Thứ tự theo adapter.inputMethod: "paste" (Gemini/Quill) dán trước; "type" (ChatGPT/ProseMirror) tránh dán vì ChatGPT
// đổi nội dung dán dài thành tệp đính kèm. Trả về nhật ký để đưa vào thông báo lỗi khi mọi cách đều không ăn.
async function pasteText(el, text, method = "type") {
  const log = [];
  const filled = () => inputText(el).length > Math.min(20, text.length / 2);
  el.focus();
  if (el.tagName === "TEXTAREA") {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    setter.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return [`textarea=${inputText(el).length}`];
  }
  const ways = {
    insertText: () => {
      const sel = window.getSelection();
      sel.selectAllChildren(el);
      sel.collapseToEnd();
      document.execCommand("insertText", false, text);
    },
    // ProseMirror theo dõi DOM (MutationObserver): ghi các <p> rồi phát input là editor đồng bộ state
    html: () => {
      const esc = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      el.innerHTML = text.split("\n").map((line) => `<p>${line ? esc(line) : "<br>"}</p>`).join("");
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    },
    paste: () => {
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    },
  };
  // Site xử lý nội dung DÁN dài không đồng bộ (Gemini: chữ hiện sau vài giây, hoặc thành tệp đính kèm) — chờ tới 4s
  // trước khi thử cách khác, không thì prompt bị nhập hai lần (tệp + chữ) và site không cho gửi.
  const attachment = () => !!document.querySelector(
    "uploader-file-preview, [data-test-id*='file-preview' i], [aria-label*='remove file' i], [aria-label*='xoá tệp' i], [aria-label*='xóa tệp' i]");
  const order = method === "paste" ? ["paste", "insertText", "html"] : ["insertText", "html", "paste"];
  for (const name of order) {
    el.focus();
    try { ways[name](); } catch (e) { log.push(`${name}: lỗi ${e.message}`); continue; }
    const wait = name === "paste" ? 4000 : 600;
    const ok = await waitFor(() => filled() || (name === "paste" && attachment()), { timeout: wait, interval: 200 })
      .catch(() => false);
    log.push(`${name}=${inputText(el).length}${attachment() ? "+tệp" : ""}`);
    if (ok) return log;
  }
  return log;
}

function pressEnter(el) {
  el.focus();
  for (const type of ["keydown", "keypress", "keyup"]) {
    el.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
  }
}

const inputText = (el) => (el ? (el.value ?? el.innerText ?? "").trim() : "");

// ---------- nhận ra câu trả lời theo nội dung ----------

function stripFences(t) {
  t = t.trim();
  const m = t.match(/^```[a-zA-Z]*\s*\n?([\s\S]*?)\n?```$/);
  return m ? m[1].trim() : t;
}

function parseCandidate(text, keys) {
  const t = stripFences(text);
  if (!t.startsWith("{")) return null;
  try {
    const obj = JSON.parse(t);
    return obj && typeof obj === "object" && keys.every((k) => k in obj) ? t : null;
  } catch (_) {
    // JSON có lỗi cú pháp nhỏ (dấu " chưa escape…) vẫn nhận nếu có đủ tên key và ngoặc đã đóng đủ
    // (đang stream dở thì ngoặc chưa cân bằng); server sẽ sửa và validate
    const balanced = (t.match(/\{/g) || []).length === (t.match(/\}/g) || []).length;
    return balanced && keys.every((k) => t.includes(`"${k}"`)) && t.trimEnd().endsWith("}") ? t : null;
  }
}

// Khối chữ cuối cùng trên trang là JSON có đủ key bắt buộc. Prompt của người dùng (bắt đầu bằng <system>)
// không bị nhận nhầm vì nó không bắt đầu bằng "{"; khối <output_schema> trong prompt không có các key này.
function findJsonAnswer(keys) {
  const nodes = document.querySelectorAll("pre, code, p, div, span, message-content");
  let best = null;
  for (const el of nodes) {
    if (el.childElementCount > 40) continue;
    const text = el.innerText;
    if (!text || text.length < 10 || !/^\s*(```|\{)/.test(text)) continue;
    const ok = parseCandidate(text, keys);
    if (ok) best = { el, text: ok };
  }
  return best;
}

function extractLinks(node) {
  // Deep Research: danh sách nguồn nằm trong khung báo cáo, ngoài phần nội dung markdown
  const scope = node?.closest?.("deep-research-immersive-panel, [data-turn-key], model-response, article") || node;
  if (!scope) return [];
  return [...new Set([...scope.querySelectorAll("a[href^='http']")].map((a) => a.href))];
}

// Dự phòng khi không nhận ra JSON đủ key (JSON bị cắt, có lời dẫn, hiển thị lạ): lấy từ "{" đầu tới "}" cuối
function jsonish(text) {
  const a = text.indexOf("{"), b = text.lastIndexOf("}");
  return a >= 0 && b > a ? text.slice(a, b + 1) : text;
}

async function waitForJson(find, keys, { timeout = 11 * 60 * 1000, stableMs = 3000, fallbackStableMs = 15000 } = {}) {
  const start = Date.now();
  let last = null, stableSince = 0, resp = "", respSince = Date.now();
  while (Date.now() - start < timeout) {
    const hit = findJsonAnswer(keys);
    if (hit && hit.text !== last?.text) { last = hit; stableSince = Date.now(); }
    if (last && !find.generating() && Date.now() - stableSince >= stableMs) return last;
    // Model đã dừng mà vẫn không thấy JSON đủ key: câu trả lời đứng yên fallbackStableMs thì nộp luôn,
    // server sửa JSON / chạy vòng repair — thay vì chờ hết 12 phút rồi lỗi rồi chạy lại y hệt.
    const el = find.response();
    const text = el ? (el.innerText || "").trim() : "";
    if (text !== resp) { resp = text; respSince = Date.now(); }
    if (!last && text.length > 50 && !find.generating() && Date.now() - respSince >= fallbackStableMs) {
      console.warn("[seo-bridge] không nhận ra JSON đủ key, nộp câu trả lời để server sửa/repair");
      return { el, text: jsonish(text) };
    }
    await sleep(700);
  }
  throw new Error(`không thấy JSON có đủ key [${keys.join(", ")}] sau ${timeout / 60000} phút — ${pageState()}`);
}

// target: hàm trả phần tử chứa câu trả lời. minChars: Deep Research — bỏ qua các đoạn ngắn (kế hoạch, "Đang nghiên cứu…")
async function waitForMarkdown(find, { timeout = 11 * 60 * 1000, stableMs = 4000, minChars = 1, target } = {}) {
  const get = target || find.response;
  await waitFor(() => find.generating() || get(), { timeout: 180000, label: "câu trả lời" })
    .catch((e) => { throw new Error(`${e.message} — ${pageState()}`); });
  const start = Date.now();
  let last = "", stableSince = Date.now();
  while (Date.now() - start < timeout) {
    const node = get();
    const text = node ? node.innerText.trim() : "";
    if (text !== last) { last = text; stableSince = Date.now(); }
    if (!find.generating() && text.length >= minChars && Date.now() - stableSince >= stableMs) return { el: node, text };
    await sleep(1000);
  }
  throw new Error(`model trả lời quá lâu (${Math.round(timeout / 60000)} phút) — ${pageState()}`);
}

// Khối chữ dài nhất đang hiện trên trang, không nằm trong khung chat (khung chứa ô nhập).
// Dùng cho báo cáo Deep Research: Gemini mở báo cáo ở khung riêng, là khối chữ dài nhất — không cần biết selector.
function largestTextBlock(exclude) {
  let best = null, bestLen = 0;
  for (const el of document.querySelectorAll("article, section, div, message-content")) {
    if (!visible(el) || el === document.body) continue;
    if (exclude && el.contains(exclude)) continue;  // khối bao cả ô nhập = khung chat hoặc cả trang
    const len = (el.innerText || "").length;
    // Ưu tiên khối chặt nhất: dài hơn hẳn, hoặc gần bằng nhưng nằm bên trong khối đang chọn
    if (len > bestLen * 1.05 || (best && best.contains(el) && len >= bestLen * 0.95)) {
      best = el;
      bestLen = len;
    }
  }
  return best;
}

// Text của khối, bỏ chữ trên nút bấm/menu (thanh công cụ "Contents", "Share & Export", "Create" của khung báo cáo)
function textWithoutControls(el) {
  const clone = el.cloneNode(true);
  clone.querySelectorAll("button, [role='button'], [role='menu'], [role='toolbar'], mat-icon, svg").forEach((n) => n.remove());
  // cloneNode không có layout nên innerText trả như textContent; giữ xuống dòng giữa các khối
  clone.querySelectorAll("p, li, h1, h2, h3, h4, h5, h6, tr, div, br").forEach((n) => n.append("\n"));
  return clone.textContent.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

// Mô tả trang hiện tại để đưa vào thông báo lỗi (trang đăng nhập, Cloudflare, hết lượt…)
function pageState() {
  const text = (document.body?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 160);
  return `trang: ${location.href} | tiêu đề: ${document.title} | tab ${document.visibilityState} | nội dung: ${text}`;
}

// Gửi prompt: dán MỘT lần (prompt dài có thể bị site đổi thành tệp đính kèm, ô nhập vẫn trống — dán lại sẽ gửi trùng),
// rồi bấm nút / Enter nhiều lượt: editor đôi khi chưa sẵn sàng (tab chạy nền, trang vừa tải) nên cú bấm đầu bị bỏ qua.
async function submitPrompt(adapter, find, input, text) {
  // Trang vừa tải có thể hiện tạm một <textarea> rồi mới thay bằng editor thật (ChatGPT: div.ProseMirror) — chữ ghi vào
  // ô tạm sẽ mất. Nhập vào ô ĐANG hiển thị, kiểm tra lại sau khi nhập, mất thì nhập lại (tối đa 3 lần).
  let filledLog = [];
  for (let round = 0; round < 3; round++) {
    let el = find.input() || input;
    if (el.tagName === "TEXTAREA") {
      await sleep(1500);
      el = find.input() || el;
    }
    filledLog = filledLog.concat(round ? [`lần ${round + 1}:`] : [], await pasteText(el, text, adapter.inputMethod || "type"));
    await sleep(700);
    const now = find.input();
    if (now && inputText(now).length > Math.min(20, text.length / 2)) break;
    filledLog.push(`ô hiện tại ${describeEl(now)} trống`);
  }
  await sleep(300);
  const before = inputText(find.input()).length;
  const diag = `ô nhập: ${describeEl(find.input() || input)}; cách nhập: ${filledLog.join(", ")}`;
  console.info("[seo-bridge]", diag);
  // Đã gửi: model bắt đầu trả lời, site chuyển sang trang hội thoại, hoặc ô nhập đang có chữ thì trống lại
  const sent = () => find.generating() || adapter.sent?.() || (before > 0 && !inputText(find.input()));
  const attempts = ["click", "enter", "click", "enter", "click", "enter"];
  for (const [i, how] of attempts.entries()) {
    // Prompt dài bị đổi thành tệp đính kèm (ô nhập trống): nút Gửi chỉ bật khi tải tệp xong — lượt đầu chờ lâu hơn
    const wait = before > 0 ? 4000 : i === 0 ? 20000 : 8000;
    const btn = how === "click" ? await waitFor(find.send, { timeout: wait, label: "nút gửi" }).catch(() => null) : null;
    if (btn) btn.click();
    else pressEnter(find.input() || input);
    const ok = await waitFor(sent, { timeout: before > 0 ? 6000 : 10000, label: "dấu hiệu đã gửi" }).catch(() => null);
    if (ok) {
      if (i) console.info(`[seo-bridge] gửi được ở lượt ${i + 1} (${how})`);
      return;
    }
    console.warn(`[seo-bridge] lượt gửi ${i + 1} (${how}) chưa ăn`);
  }
  throw new Error(`không tìm thấy dấu hiệu đã gửi sau ${attempts.length} lượt bấm nút/Enter (ô nhập ${before} ký tự; ${diag})`);
}

async function runTask(adapter, task) {
  console.info("[seo-bridge] nhận task", task.id, task.prompt_id, "keys:", task.expect_keys);
  const find = makeFinder(adapter, task.selectors || {});
  const input = await waitFor(find.input, { timeout: 60000, label: "ô nhập prompt" })
    .catch((e) => { throw new Error(`${e.message} — ${pageState()}`); });
  if (adapter.beforeSend) await adapter.beforeSend(task);
  const deep = task.tool === "deep_research";
  if (deep) {
    await enableDeepResearch(task.tool_texts || {})
      .catch((e) => { throw new Error(`không bật được Deep Research: ${e.message} — ${pageState()}`); });
  }
  await submitPrompt(adapter, find, input, task.prompt_text)
    .catch((e) => { throw new Error(`${e.message} — ${pageState()}`); });
  console.info("[seo-bridge] đã gửi, chờ trả lời…");

  const keys = task.expect_keys || [];
  const timeout = (task.timeout_seconds || 660) * 1000;
  let answer;
  if (deep) {
    // Gemini trình bày kế hoạch nghiên cứu rồi chờ bấm "Start research"; không thấy nút thì coi như tự chạy
    await clickByText(task.tool_texts?.start, { timeout: 300000, label: "nút Start research" })
      .then(() => console.info("[seo-bridge] đã bấm Start research"))
      .catch(() => console.warn("[seo-bridge] không thấy nút Start research, chờ tiếp"));
    // Báo cáo dài; nghiên cứu có lúc đứng yên vài chục giây nên chờ nội dung yên 90s mới coi là xong
    answer = await waitForMarkdown(find, {
      timeout, stableMs: 90000, minChars: 2500,
      target: () => find.report() || largestTextBlock(find.input()) || find.response(),
    });
    answer = { el: answer.el, text: textWithoutControls(answer.el) || answer.text };
  } else {
    answer = keys.length ? await waitForJson(find, keys, { timeout }) : await waitForMarkdown(find, { timeout });
  }
  console.info("[seo-bridge] xong", answer.text.length, "ký tự");
  return {
    text: answer.text,
    sourceUrls: extractLinks(answer.el),
    model: adapter.modelName ? adapter.modelName() : null,
  };
}

function registerAdapter(adapter) {
  globalThis.seoBridgeCheck = (keys = []) => checkAdapter(adapter, keys);
  // Thử riêng bước bật Deep Research (không gửi prompt). Chữ mặc định giống config.yaml extension.deep_research.texts
  globalThis.seoBridgeDR = () => enableDeepResearch({
    menu_path: [["Upload & tools", "Tải lên và công cụ", "Công cụ"], ["More tools", "Thêm công cụ"],
                ["Deep research", "Nghiên cứu chuyên sâu"]],
  }).then(() => "OK: đã bật Deep Research").catch((e) => `LỖI: ${e.message}`);
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type !== "RUN_TASK") return false;
    runTask(adapter, msg.task)
      .then((res) => sendResponse({ ok: true, ...res }))
      .catch((e) => sendResponse({ ok: false, error: String(e.message || e) }));
    return true; // trả lời bất đồng bộ
  });
}

// DevTools console của tab chat (context của extension): seoBridgeCheck() hoặc seoBridgeCheck(["branch_suggest"])
function checkAdapter(adapter, keys) {
  const find = makeFinder(adapter, {});
  const show = (v) => (v instanceof Element ? `✓ <${v.tagName.toLowerCase()}>` : v);
  const r = {
    input: show(find.input()),
    "input (chung)": show(genericInput()),
    sendButton: show(find.send()),
    isGenerating: find.generating(),
    lastResponse: show(find.response()),
    sent: adapter.sent?.() ?? null,
    modelName: adapter.modelName?.() ?? null,
  };
  if (keys.length) r[`JSON có key ${keys.join(",")}`] = findJsonAnswer(keys)?.text?.slice(0, 80) ?? null;
  // Deep Research (chữ mặc định; chữ thật lấy từ config.yaml extension.deep_research.texts)
  const txt = (el) => (el ? `✓ ${el.tagName.toLowerCase()} "${(el.innerText || el.getAttribute("aria-label") || "").trim().slice(0, 30)}"` : null);
  r["DR: nút + (Upload & tools)"] = txt(findByText(["Upload & tools", "Tải lên và công cụ"]));
  r["DR: More tools (khi menu mở)"] = txt(findByText(["More tools", "Thêm công cụ"]));
  r["DR: Deep research (khi menu con mở)"] = txt(findByText(["Deep research", "Nghiên cứu chuyên sâu"]));
  r["DR: nút Start research"] = txt(findByText(["Start research", "Bắt đầu nghiên cứu"]));
  r["DR: khung báo cáo"] = show(find.report());
  const big = largestTextBlock(find.input());
  r["DR: khối chữ dài nhất"] = big ? `${big.tagName.toLowerCase()}.${String(big.className).slice(0, 40)} (${big.innerText.length} ký tự): ${big.innerText.slice(0, 60)}` : null;
  console.table(r);
  return r;
}
