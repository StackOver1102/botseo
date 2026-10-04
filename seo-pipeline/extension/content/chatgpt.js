// Adapter cho chatgpt.com. CẦN KIỂM TRA LẠI SELECTOR khi ChatGPT đổi giao diện.

// Giao diện 2026-09: câu trả lời nằm trong div[data-markdown-text-style="assistant-message"];
// giao diện cũ dùng [data-message-author-role="assistant"] .markdown
function lastAssistant() {
  const nodes = document.querySelectorAll(
    "[data-markdown-text-style='assistant-message'], [data-message-author-role='assistant'] .markdown",
  );
  return nodes[nodes.length - 1] || null;
}

registerAdapter({
  inputMethod: "type",  // ChatGPT đổi nội dung DÁN dài thành tệp đính kèm → gõ thẳng bằng insertText
  input: () =>
    document.querySelector("#prompt-textarea, div[contenteditable='true'][id='prompt-textarea']") ||
    document.querySelector("form div.ProseMirror[contenteditable='true'], textarea[name='prompt-textarea']"),
  // Giao diện 2026-09: <button type="submit" aria-label="Send"> (không còn data-testid)
  sendButton: () =>
    document.querySelector("button[data-testid='send-button'], #composer-submit-button") ||
    document.querySelector("button[type='submit'][aria-label='Send'], button[aria-label*='send' i], button[aria-label*='gửi' i]") ||
    null,
  // Chỉ dựa vào nút Stop. Không suy từ các nút dưới câu trả lời (Regenerate…): ChatGPT ẩn/hiện chúng tuỳ lượt,
  // làm extension tưởng model còn đang chạy. Việc "xong" chủ yếu do nội dung đứng yên (common.js) quyết định.
  isGenerating: () =>
    !!document.querySelector("button[data-testid='stop-button'], button[aria-label*='stop' i], button[aria-label*='dừng' i]"),
  lastResponse: () => lastAssistant(),
  // Gửi xong ChatGPT chuyển sang /c/<id>: dấu hiệu gửi thành công kể cả khi model còn đang "nghĩ"
  sent: () => /\/c\//.test(location.pathname),
  modelName: () => document.querySelector("[data-testid='model-switcher-dropdown-button']")?.innerText?.trim() || "chatgpt-web",
});
