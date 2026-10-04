"""Lỗi cú pháp JS làm trắng cả trang UI / hỏng extension mà không test Python nào bắt được."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="không có node")
@pytest.mark.parametrize("path", ["extension/background.js", "extension/content/common.js",
                                  "extension/content/chatgpt.js", "extension/content/gemini.js", "app/static/ui.html"])
def test_js_parses(path, tmp_path):
    src = (ROOT / path).read_text(encoding="utf-8")
    if path.endswith(".html"):
        src = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", src))
    f = tmp_path / "x.mjs"
    f.write_text(src, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
