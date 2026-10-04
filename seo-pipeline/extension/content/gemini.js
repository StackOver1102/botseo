// Adapter cho gemini.google.com. CẦN KIỂM TRA LẠI SELECTOR khi Gemini đổi giao diện.

registerAdapter({
  inputMethod: "paste",  // Quill: insertText không cập nhật state (nút Gửi không hiện); Gemini giữ nội dung dán dài trong ô nhập
  input: () => document.querySelector("rich-textarea .ql-editor, div.ql-editor[contenteditable='true']"),
  // Nút gửi chỉ xuất hiện khi ô nhập có chữ (lúc trống là nút micro)
  sendButton: () =>
    document.querySelector("button.send-button, button[aria-label*='send' i], button[aria-label*='gửi' i]") ||
    [...document.querySelectorAll("button mat-icon")]
      .find((i) => (i.getAttribute("fonticon") || i.getAttribute("data-mat-icon-name") || i.textContent).trim() === "send")
      ?.closest("button") ||
    null,
  isGenerating: () => !!document.querySelector("button.stop, button[aria-label*='Stop'], button[aria-label*='Dừng']"),
  lastResponse: () => {
    const turns = document.querySelectorAll("model-response message-content, model-response .model-response-text");
    return turns[turns.length - 1] || null;
  },
  // Gửi xong Gemini chuyển từ /app sang /app/<id hội thoại>: dấu hiệu đã gửi kể cả khi prompt dài thành tệp đính kèm
  sent: () => /^\/app\/[\w-]+/.test(location.pathname),
  modelName: () => document.querySelector("bard-mode-switcher, [data-test-id='bard-mode-menu-button']")?.innerText?.trim() || "gemini-web",
});
