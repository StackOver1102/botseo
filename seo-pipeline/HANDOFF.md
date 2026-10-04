# SEO Pipeline — bàn giao (cập nhật 2026-10-03)

Tài liệu để chuyển dự án sang máy khác và làm tiếp. Spec gốc: `docs/overview.md` (SEO Content Pipeline v5).
Ghi chú memory của Claude (quyết định/yêu cầu của chủ dự án): `docs/claude-memory/` — trên máy mới copy vào
`~/.claude/projects/<thư-mục-dự-án>/memory/`, hoặc chỉ cần bảo Claude đọc file này + thư mục đó.
File này ghi những gì code **đã làm khác spec** và những gì học được khi chạy thật.

## 1. Hệ thống làm gì

Tạo / viết lại bài SEO tiếng Việt tự động, **không có người duyệt** (mọi chỗ "chờ người" đã được autopilot thay).
Hai luồng (`jobs.flow`):

| Luồng | Dùng khi | Các bước | Chi phí / thời gian đo được |
|---|---|---|---|
| **full** — nhánh A (mặc định cho bài có sẵn) | Làm mới bài của chính site: cập nhật số liệu, thêm IG, viết lại đúng giọng | B0→B12 như B, nhưng outline + writer **thấy bài gốc** (mục 5b) | 0,75 USD / 38 phút đo được (trước tối ưu chi phí); ước ~0,4–0,45 USD sau tối ưu |
| **full** — nhánh B | Bài mới từ keyword (+ `auto_source` tư liệu) | B0→B12 của spec | 0,4–1 USD, 30 phút–2 giờ |
| **rework** (nhánh A, chọn "Chỉ viết lại câu chữ") | Chỉ đổi câu chữ, giữ nguyên nội dung/số liệu | `REWORK → REWORK_CHECK → DONE` | ~0,1 USD, 2 phút / bài ~2.200 chữ |

Cách gọi model (quyết định của chủ dự án): **chỉ Claude gọi API trả tiền**; GPT và Gemini chạy **qua extension Chrome
trên ChatGPT/Gemini web** (miễn phí). Nhánh gọi API cho OpenAI/Gemini vẫn có sẵn (`providers.<hãng>.mode: api`).

## 2. Cài trên máy mới

Yêu cầu: Python 3.12, Docker, Chrome (đăng nhập sẵn chatgpt.com và gemini.google.com), Node (chỉ để chạy test cú pháp JS).

```bash
cd seo-pipeline
docker compose up -d                       # Postgres 16 + pgvector, cổng 55432, db/user/pass = seo
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # hoặc: pip install -e . pytest
cp .env.example .env && chmod 600 .env     # điền key (mục 3)
.venv/bin/python -m pipeline.db            # chạy migrations 001→006
.venv/bin/python -m pytest -q              # 167 test phải pass
.venv/bin/uvicorn app.main:app --port 8000 --timeout-graceful-shutdown 3
```

- UI theo dõi + tạo job: **http://localhost:8000/ui** (chỉ mở từ localhost; mở ra ngoài thì đặt `SEO_UI_TOKEN`).
- uvicorn chạy kèm **worker nhúng** (job tạo từ UI tự chạy). Tắt bằng `SEO_EMBEDDED_WORKER=0` nếu chạy worker riêng
  (`python -m pipeline.worker`).
- `--timeout-graceful-shutdown 3` bắt buộc: không có thì Ctrl+C treo vì kết nối SSE/long-poll.
- Extension: `chrome://extensions` → Developer mode → Load unpacked → thư mục `extension/` → trang Options nhập
  `http://localhost:8000` và token = `SEO_EXTENSION_TOKEN`. (`extension/README.md` đã cũ: giờ là long-poll, pool tab,
  chạy song song 2 tab/hãng.)
- Muốn mang dữ liệu cũ (job, bài đã chạy): `pg_dump` DB + copy `var/blobs/` (artifact, prompt, output nằm ở đây) và `out/`.
  Không mang thì máy mới bắt đầu trống, không sao.

## 3. Biến môi trường (.env)

| Biến | Bắt buộc | Ghi chú |
|---|---|---|
| `ANTHROPIC_API_KEY` | có | Claude: viết bài, rework, sửa |
| `SERPER_API_KEY` | luồng full | SERP + tìm tư liệu (serper.dev) |
| `SEO_EXTENSION_TOKEN` | có | extension gửi ở header `X-Extension-Token` |
| `SEO_ADMIN_TOKEN` | không | API `/jobs` cũ |
| `OPENAI_API_KEY`, `GEMINI_API_KEY` | không | chỉ khi đổi provider sang `mode: api` |
| `SEO_UI_TOKEN` | không | mở UI từ máy khác: `/ui?token=…` |

Không đưa key vào git / tin nhắn. Email người dùng không gửi ra dịch vụ ngoài.

## 4. Cấu trúc code

```
app/main.py            FastAPI: /external/* cho extension (long-poll qua Postgres NOTIFY), /jobs, gắn UI, worker nhúng
app/ui.py, static/ui.html  UI: danh sách job, timeline, prompt & output từng lượt, task extension, cảnh báo, bài viết,
                       form tạo job (nhánh A: dán bài / link; nhánh B: chủ đề + keyword/file Ahrefs). Realtime qua SSE.
pipeline/states.py     máy trạng thái (State, Outcome, TRANSITIONS, REVISE_COUNTER)
pipeline/executor.py   Worker: lease (+ thread gia hạn lease), gọi handler, ghi transition, gọi autopilot
pipeline/autopilot.py  thay người ở NEEDS_HUMAN (mục 6)
pipeline/steps/m0.py   B0–B8 (triage → uniqueness);  m1.py: B9–B12 (QA → GSC);  rework.py: luồng rework
pipeline/llm/          runner (cache, repair, lưu prompt), validators (+ PRUNERS), anthropic/openai/gemini/external provider
pipeline/autosource.py tự lấy tư liệu nhánh B từ top SERP;  article_io.py: chuẩn hoá bài dán / tải bài từ link
pipeline/keyword_io.py đọc keyword: Ahrefs (UTF-16, tab), CSV ngoặc kép, dán tay
pipeline/notify.py     LISTEN/NOTIFY (kênh ext_task, job_event)
prompts/*.yaml         26 prompt (22 spec + P10-S + RW + RWA + P6-A); _shared/hard_rules.md, repair_v1.md
config/config.yaml     MỌI tham số: providers, models, thresholds, gates, automation, extension selectors…
extension/             MV3: background.js (long-poll, pool tab), content/common.js (nhập/gửi/đọc kết quả), chatgpt.js, gemini.js
scripts/run_job.py     chạy job từ thư mục (examples/*): --resume, --from STATE, --cancel, --dry-run; xuất out/<job>/
```

Thư mục job cho `run_job.py`: `job.yaml` (+ `keywords.csv`, `paa.txt`, `article.md` hoặc `article_url:`,
`auto_source: N`, `flow: rework|full`), xem `examples/`.

## 5. Luồng rework (nhánh A) — phần đang dùng chính

- Đầu vào: bài dán (văn bản thường từ CMS, heading không có `#` được tự đoán) **hoặc link** (trafilatura lấy nội dung chính
  dạng Markdown, tiêu đề tách ra làm tên job). Không cần nhập gì thêm — keyword/YMYL tự xác định.
- `REWORK`:
  1. **Phân tích tự động** (prompt `RWA`, Claude Haiku, ~0,008 USD) thay cho triage + keyword map: `ymyl` + lý do,
     keyword chính + ≤8 keyword phụ **lấy nguyên văn trong bài** (code bỏ cụm không có trong bài). Keyword người dùng nhập
     được ưu tiên. Lưu artifact `ReworkBrief`, ghi `jobs.ymyl`.
  2. Cắt bài theo heading thành phần ≤700 chữ, Claude Sonnet (effort low) viết lại từng phần với prompt `RW`, kèm keyword
     cần giữ của phần đó (phần 1: keyword chính) và lưu ý YMYL. Lý do cắt: viết cả bài một lượt thì model chép gần nguyên
     phần cuối (đo được trùng 45%).
- `REWORK_CHECK` (code, không gọi model): số heading bằng bản gốc; độ dài **100–140%** bản gốc (yêu cầu của chủ dự án: không ngắn hơn bản gốc); không mất/thêm con số
  ("3"→"ba" được chấp nhận); trùng 5-gram ≤25% (không tính heading); keyword có trong phần gốc phải còn trong phần viết
  lại; keyword chính ở đoạn mở đầu hoặc một heading. Lỗi → chỉ viết lại **phần còn lỗi** kèm lỗi cụ thể, tối đa 2 lượt;
  hết lượt thì đi tiếp + Warnings.
- Kết quả đo trên bài kqbd (2.196 chữ, 14 heading, không nhập keyword): AI tự xác định YMYL = có (kèo cược), keyword chính
  "kết quả bóng đá trực tiếp" + 5 keyword phụ; 14/14 heading, 123%, trùng 12%, 0 số sai, 0 keyword thiếu; 1 vòng sửa vì
  lượt đầu dài 133%; 116 s, 0,134 USD.
- Model không bám mục tiêu độ dài trong prompt (~108%): thực tế 115–138%/phần. Vì vậy trần nới lên 140% — vòng sửa chỉ
  còn dùng khi bài ngắn hơn bản gốc. Đo trên bài dongsport.vn (dán link, 1.635 chữ, 31 heading): 124%, trùng 13%.

## 5b. Nhánh A trong luồng full (sửa 2026-10-03)

Trước đây luồng full xử lý nhánh A y như B: outline (P4) và writer (P6) **không thấy bài gốc** → bài kqbd 14 heading còn 3,
2.236 → 1.173 chữ. Đã sửa (`pipeline/steps/branch_a.py`):
- Bài gốc tách thành section `O00` (mở bài), `O01…` (mỗi heading). P4-D/P4-G (5.1.0) nhận `<bai_goc_khung>`, mỗi section
  outline ghi `source_sections`; code `cover_original` gán lại section gốc bị bỏ sót (ghi Warnings).
- WRITE nhánh A dùng prompt **P6-A**: viết từng H2 từ đoạn gốc tương ứng + fact đã kiểm chứng (thay số cũ bằng new_value) +
  IG + giọng P3. Code kiểm tra từng section: dài ≥ đoạn gốc, trùng ≤ `rework_containment_max` (25%); sai thì viết lại
  section đó (tối đa `revise_max.rework` lần), hết lượt → Warnings. Section mới (IG) không có bản gốc thì viết từ fact.
- `TARGET_WORDS` = max(target_words, bài gốc × 1,1).
- Không có keyword: KW_NORMALIZE tự lấy keyword từ bài (RWA) — UI/ run_job không bắt nhập.
- QA: nhánh A chỉ có information_gain = 0 thì **không** quay lại viết (dữ liệu bị giới hạn bởi bài gốc); accuracy = 0 vẫn quay.
- UI nhánh A: chọn "Làm mới đầy đủ" (mặc định, flow full) hoặc "Chỉ viết lại câu chữ" (rework). `run_job`: mặc định full.
- Đoạn gốc đưa cho P6-A: heading gốc đổi thành nhãn `[Mục gốc: …]` (để nguyên `## …` thì writer chép lại heading giữa
  section rồi viết "tiếp" bằng "..." + lặp ý — lỗi job ac6bf725 ở S01 = mở bài O00 + O01). Code `dedupe_headings` bỏ
  heading trùng và "..."/"…" đầu đoạn.
- Tối ưu chi phí (sau job ac6bf725, chưa đo lại): mỗi section chỉ nhận fact liên quan (fact outline gán + claim bai_goc có
  câu gốc trong đoạn gốc) thay vì cả bảng fact (~13k token/lượt × 12); prompt caching cho P6-A; P3 (voice) → Haiku.
- `config.default_author` (tên/URL tác giả) — job từ UI lấy làm author JSON-LD; **cần điền tên thật** (để null thì FINAL
  cảnh báo "Article thiếu author", autopilot vẫn cho qua).
- Kết quả đo job `ac6bf725` (bài kqbd, không nhập keyword, trước 2 bản sửa trên): dàn ý phủ 15/15 section gốc → 12 H2;
  2.070 → 2.612 chữ (126%); trùng 4%; rubric 16/18 ĐẠT; fact-check xoá 2 câu không kiểm chứng được; 0,75 USD
  (P6-A 0,52 · P3 0,14 · G 0,04 · RWA 0,01). Bài đó còn lỗi heading lặp + "..." (đã sửa cho job sau; chạy lại
  `--from WRITE` nếu cần bài sạch). So với luồng full cũ trên cùng bài: 3 heading, 1.173 chữ, viết lại 3 lần.

## 6. Tự động 100% (yêu cầu cứng của chủ dự án)

"Hệ thống không có người dùng tay": lỗi thì quay về bước gây lỗi chạy lại rồi đi tiếp, **không dừng job**.
- Mọi gate `mode: auto`. `automation.enabled: true`.
- Autopilot khi job vào NEEDS_HUMAN: lỗi (model sai sau repair, extension lỗi…) → chạy lại sau 1/5/15/30 phút, mỗi lần
  hỏi model **lượt mới** (khoá idempotency có `:r<vòng>`); gate chê → gửi về bước tạo artifact kèm lý do (2 lần) rồi duyệt
  thay + Warnings; UNIQUENESS trượt → về WRITE. **Chỉ dừng thật khi vượt trần chi phí job**.
- QA rubric có tiêu chí blocking = 0: đủ fact → về WRITE; thiếu fact → về FACT_VOICE_FANOUT (có nguồn) / GAP_IG_AI.
- Lỗi nhỏ của output được code tự sửa thay vì repair qua web (PRUNERS trong validators): URL coverage ngoài SERP,
  nguồn P10-S sai URL/quote, claim P2a có verbatim không khớp, edit P9 có `find` không khớp. Chỉ repair khi >½ hỏng.
- Runner: output cũ bị loại nhưng hợp lệ với validator mới thì dùng lại (khỏi trả tiền gọi lại).

## 7. Những điều học được khi chạy thật (đừng lặp lại)

**Extension / web UI** (phần dễ vỡ nhất):
- ChatGPT đổi nội dung **dán** dài thành tệp đính kèm → nút Gửi không bật. Phải **gõ** (`execCommand insertText`).
- ChatGPT hiện tạm một `<textarea>` lúc trang đang tải rồi thay bằng `div.ProseMirror` → chữ ghi vào ô tạm bị mất.
  Selector ưu tiên ProseMirror; nhập xong kiểm tra lại trên ô đang hiển thị, mất thì nhập lại.
- Gemini (Quill) ngược lại: phải **dán**; insertText hiện chữ nhưng state không cập nhật. Dán dài xử lý không đồng bộ →
  chờ tới 4 s trước khi thử cách khác (không thì nhập 2 lần). Vẫn **chập chờn với prompt 20–50k ký tự, tab chạy nền**.
- `chrome.tabs.update` tới đúng URL đang mở không tải lại trang → phải `reload`; mọi chờ tải trang có timeout 30 s.
- Nhận câu trả lời theo nội dung (JSON có đủ `expect_keys`), không theo DOM. Model dừng mà không thấy JSON đủ key →
  nộp sau 15 s đứng yên để server sửa, không chờ 12 phút.
- Lỗi extension kèm chẩn đoán (ô nhập là gì, mỗi cách nhập vào được bao nhiêu ký tự) → đọc trên UI tab "Task extension".
- Sửa extension xong phải **Reload** ở chrome://extensions; task đang `claimed` bởi bản cũ phải đặt lại `pending`.
- **Tab nền bị Chrome hạn chế** (`visibilityState = hidden`) → Gemini không nhận nội dung dán (`paste=0`), nút Gửi không
  bật. Nay mỗi tab làm việc mở trong **cửa sổ riêng, không lấy focus** (tab đang hiện của cửa sổ đó). Không minimize các
  cửa sổ này. Debug mode thì mở tab thường.
- Đã gặp: extension ngừng nhận task ~7 phút khi rảnh (lúc job chờ WRITE chạy Claude) rồi tự chạy lại — chưa rõ nguyên nhân,
  cần log console service worker. UI hiện cảnh báo đỏ khi có task chờ >3 phút.
- Server chạy nền trong phiên Claude Code bị dừng sau giới hạn thời gian → chạy uvicorn trong terminal của người dùng.

**Pipeline full**:
- Không có bài nguồn thì writer không có dữ kiện (luật "mọi số liệu phải từ fact") → bài chung chung, thiếu "90 phút".
  Đã thêm `auto_source: N`: lấy N trang top SERP làm tư liệu (lọc nhà cái/lậu theo URL **và nội dung**, lọc trang lệch chủ
  đề, xem thêm trang 2–3 SERP). Text nguồn chỉ vào P2a/P2b, xoá sau FACT_GATE.
- Lệch spec ADR-6 có chủ đích: writer nhận claim bài nguồn dạng **một câu ≤220 ký tự** (`writer_source_facts: sentence`);
  chỉ đưa con số thì claim không số thành rỗng. Quay về spec: `values`.
- Claude từng bịa trải nghiệm "mình từng thử…" → prompt P6 cấm khi `author_evidence` rỗng.
- Gemini Flash chấm P8d theo từng claim thay vì 3 tiêu chí → prompt P8d 4.1.0 có ví dụ format.
- QA P8a–P8d đều chạy Gemini (ChatGPT web treo với prompt dài). `choose_model` khi đổi họ ưu tiên họ chạy web (miễn phí).
- Model rẻ cho bước rẻ: P1 → ChatGPT web, P2a → Gemini web, P7b/P9 → Haiku 4.5 (không gửi `effort` cho Haiku),
  writer Sonnet effort medium.

**Vận hành**:
- Restart uvicorn giữa lúc worker đang gọi model từng để lại lease 15 phút → nay lease 2 phút + thread gia hạn mỗi 30 s.
- Máy khởi động lại thì container Postgres tắt: `docker start seo-pipeline-pg`.
- Postgres NOTIFY (migration 005) cho realtime: extension nhận task ~1 s, worker và UI thức dậy ngay.
- Mỗi lượt gọi model lưu toàn văn prompt (`prompt_runs.request_redacted.prompt_uri`) — lưu ý ADR-6: prompt P2a/P2b chứa
  text nguồn và không bị xoá (`debug.save_prompts: false` để tắt). `run_job` xuất `out/<job>/steps/` + `timeline.md`.

## 8. Việc còn mở

0. Nhánh A full: đo lại chi phí/thời gian sau tối ưu; chạy lại bài kqbd `--from WRITE` để kiểm tra hết lỗi "..." / heading
   lặp; điền `default_author`; tìm nguyên nhân extension ngừng nhận task khi rảnh.
1. Rework: nhãn ngắn kiểu "Mục tiêu", "Tầm nhìn" bị nhận thành `##` (siết
   `article_io.normalize_article` nếu không muốn). Model viết dài 115–138% dù prompt nhắm ~108% (trần đã nới lên 140%).
2. Gemini web chập chờn với prompt dài → đề xuất chuyển Gemini sang **API free tier** (`providers.gemini.mode: api`,
   key ở aistudio.google.com), extension chỉ còn cho ChatGPT + Deep Research.
3. Luồng full: phần thông tin mới (information gain) hay = 0 với bài thiếu dữ liệu; nhánh A full chưa tự lấy tư liệu.
4. `extension.chat_urls.openai.fast`: chưa có URL model nhanh cho gate trên ChatGPT.
5. Chưa có CMS publish (chỉ xuất `out/` / FinalPackage), GSC làm tay.

## 9. Gemini qua proxy cục bộ (2026-10-03) — thay extension cho Gemini

`providers.gemini.mode: proxy`: Gemini không còn đi qua extension. Pipeline gọi proxy `gemini-web-to-api` chạy bằng Docker
ở `127.0.0.1:4981` (thư mục `../gemini-web-to-api/`, cookie Gemini web trong `.env` ở đó).

- Chạy proxy: `cd ../gemini-web-to-api && docker compose up -d --build`. Proxy **không có xác thực** — giữ nguyên
  `127.0.0.1` trong compose. Cookie hết hạn: copy lại header `Cookie` của một request `batchexecute` trên
  gemini.google.com vào `GEMINI_COOKIES` (kèm `GEMINI_AUTH_USER` nếu URL có `/u/<số>/`) rồi `docker compose up -d`.
- Code: `pipeline/llm/gemini_proxy_provider.py` (test: `tests/test_gemini_proxy.py`). Schema được chèn vào prompt, output
  parse lỏng như nhánh extension, chi phí = 0. Proxy không có `google_search`/`groundingMetadata`: P4-G và P10 chỉ được
  nhắc "được phép tìm web, ghi URL nguồn"; URL do model tự ghi, code vẫn tải trang kiểm tra như trước.
- **Flash trên web từ chối khi output dài** (HTTP 200 + "Tôi không thể trợ giúp về điều đó…"): provider nhận ra và gọi
  lại bằng `fallback_model` (gemini-3.1-pro). Đo thật: P2a kiểu 113 câu trích từ bài 30k ký tự — Flash từ chối, Pro trả
  113/113 sau ~60 s. `fact_extract` đã chuyển sang Pro để khỏi tốn một lượt gọi hỏng.
- **Deep Research (P4-DR) là Deep Research thật của Gemini.** Proxy gốc chỉ giả lập bằng 3 loại prompt chat (xong
  trong ~1 phút); bản trong `../gemini-web-to-api/src` (nhánh `feat/real-deep-research`, chưa commit, chỉ dùng cục bộ)
  đã thêm luồng thật: gửi câu hỏi → nhận kế hoạch → gửi lượt xác nhận "Start research" → đọc lại hội thoại tới khi có
  báo cáo. Một lượt mất 5–30 phút (đo: 15,6 phút, báo cáo ~15k ký tự, 8 nguồn) và tạo một cuộc hội thoại trong lịch sử
  Gemini. Pipeline gọi `/interactions` chạy nền; trạng thái lưu ở `external_tasks` (`claimed_by = gemini-proxy`), job
  được hẹn chạy lại mỗi `poll_seconds` nên worker không bị giữ. Proxy restart giữa chừng thì lượt nghiên cứu được tạo lại.
- Response thô của các lượt Deep Research được lưu ở `../gemini-web-to-api/debug/` (bật bằng
  `GEMINI_DEEP_RESEARCH_DEBUG_DIR` trong compose) — dùng để đối chiếu vị trí trường khi Google đổi định dạng. Luồng port
  từ thư viện `gemini_webapi` 2.1.1 (`components/research_mixin.py`, `utils/research.py`): khi hỏng, xem bản mới của
  thư viện đó trước.
- Quay về cách cũ: `providers.gemini.mode: external` (extension) hoặc `api` (cần `GEMINI_API_KEY`).
- ChatGPT (`providers.openai`) vẫn `mode: external` qua extension.

## 10. Danh sách tác giả trong DB (2026-10-03)

Bảng `authors` (migration 007): mỗi tác giả có mã, tên, URL, **hồ sơ giọng văn** dùng lại cho mọi bài và **kho dữ liệu**
riêng (`author_knowledge.author_id` = mã tác giả). Form tạo job trên UI có ô chọn tác giả (+ nút thêm); `run_job.py` lấy
theo `author_id` trong job.yaml. Tác giả được chọn → tên/URL vào JSON-LD (hết cảnh báo "Article thiếu author"), hồ sơ
giọng văn vào `JobInput.voice_profile` (nhánh B hết lỗi "thiếu mẫu giọng văn", không chạy P3 mỗi job).

```
python scripts/author_tool.py list
python scripts/author_tool.py add "Tên tác giả" --url https://site.vn/tac-gia/x [--id ma-tac-gia]
python scripts/author_tool.py voice <mã> <url hoặc file .md> …     # 2–5 bài do chính tác giả viết → chạy P3 một lần
python scripts/author_tool.py knowledge <mã> kho.yaml              # mẫu: examples/vay-tien-online/knowledge.yaml
```

Tên tác giả, bài mẫu và nội dung kho phải là thật: hồ sơ giọng văn gồm câu trích nguyên văn (code kiểm tra có trong mẫu),
kho dữ liệu là thứ duy nhất writer được dùng làm "trải nghiệm tác giả". Mã không có trong bảng thì job dùng
`default_author` của config như trước.

**Tự học giọng văn (migration 008, `voice_learning` trong config).** Tác giả chưa có hồ sơ dựng từ bài của chính họ thì
mỗi job nhánh B có nguồn SERP (`auto_source`) chạy P3 trên các trang nguồn ngay ở FACT_VOICE_FANOUT (trước khi text
nguồn bị xoá) và cập nhật hồ sơ của tác giả; lượt sau nhận hồ sơ hiện tại để điều chỉnh. Học tối đa `max_jobs` (5) job
rồi giữ nguyên. Hồ sơ loại này (`voice_origin = serp`) chỉ giữ mô tả phong cách + checklist, **bỏ mọi câu trích nguyên
văn** của site khác (`authors.style_only`). Chạy `author_tool.py voice` với bài của chính tác giả sẽ thay hồ sơ và tắt
việc tự học cho tác giả đó. Job không có giọng văn nào không còn bị FAIL: viết giọng trung tính + cảnh báo.
Tác giả có `kind`: person | organization (`add --org`) → `@type` của author trong JSON-LD; tên/URL trong JSON-LD lấy từ
tác giả đã chọn, không theo chữ P7a chép lại. Tác giả mặc định đã tạo: `ban-bien-tap` ("Ban biên tập", tổ chức).

## 11. FACTCHECK tìm nguồn bằng code (2026-10-04)

`factcheck.mode: search` (config): bước FACTCHECK không còn nhờ model tự tra web. Lý do: qua proxy/extension Gemini
không tra cứu gì, trả "không xác minh được" cho mọi claim (đo: 34 claim trong 23 s, 0 nguồn) và FACT_APPLY xoá sạch số
liệu khỏi bài. Cách mới (`m1._factcheck_by_search`), cùng mẫu với NEW_FACT_CHECK:
1. Mỗi claim một lượt tìm Serper bằng chính câu claim (tối đa `max_searches`/bài, claim số liệu/giá/luật/ngày trước).
   Bỏ trang bị chặn (`adapters.auto_source`) và **chính các trang job lấy làm tư liệu** (tránh xác minh vòng tròn).
   Kết quả lưu ở artifact `FactSearch`: bước chạy lại không tốn thêm lượt tìm.
2. Tải `results_per_claim` trang, cắt đoạn liên quan (`excerpt_chars`), đưa cho P10-S theo lô `claims_per_call` claim.
3. Claim model bỏ sót được hỏi lại một lượt; claim_id lạ bị bỏ. Claim không tìm/tải được nguồn → KHONG_XAC_MINH_DUOC
   kèm lý do trong `conflict_note`.
Quote lệch vài ký tự so với nguồn (đo thật: "Đorgu"/"Dorgu", khớp 198/199) được thay bằng câu nguyên văn trong nguồn
thay vì bỏ nguồn (`validators._near_quote`, ngưỡng 90%, quote ≥ 40 ký tự).
Đo trên 10 claim thật của job f5defc55: tìm + tải + đối chiếu 50 s; "MU thắng 3-2" và "Hong Kong vô địch FIFA ASEAN
Cup 2026" được xác minh (VietNamNet, Báo Mới); số liệu tự công bố của một site ("độ chính xác 99,7%") vẫn không xác
minh được — đúng. `factcheck.mode: model` = cách cũ (chỉ hợp với Gemini API chính thức có google_search).

## 12. Sửa lỗi sau khi chạy job thật (2026-10-04)

Test: 225 pass. Các bản sửa của đợt rà soát 2026-10-01 đã được áp lại lên bản code này (`tests/test_review_fixes.py`).

- **Xoá dữ kiện cho sạch** (`vnseo/purge.py`, gọi ở FACT_APPLY sau P9): code xoá câu / bullet / dòng bảng còn số %, tiền,
  ngày không khớp fact đã duyệt (đúng tiêu chí FACT_RECHECK), dòng bảng của claim bị xoá, và số thường "đủ đặc trưng"
  (≥ 100 hoặc có phần lẻ) của claim bị xoá. Bảng không còn dòng dữ liệu bị bỏ cả bảng kèm câu dẫn kết thúc bằng ":".
  Không xoá heading. Nội dung đã xoá ghi ở artifact `FactPurge` + Warnings. Lý do: P9 xoá dòng bảng nhưng để lại tiêu
  đề bảng, và sót 11 con số của claim đã bị đánh dấu xoá (job f5defc55).
- **P9 vượt ngưỡng sửa** (`fix_edit_ratio_max`): không bỏ cả lô nữa; áp các edit theo thứ tự lỗi nặng trước
  (chan → cao → trung_binh → thap) cho tới khi chạm ngưỡng, phần còn lại ghi `over_edit_ratio` (cảnh báo `fix_partial`).
- **Autopilot** chỉ duyệt thay ở state gate/kiểm tra (`autopilot.APPROVABLE`); bước tạo artifact lỗi (lý do có tiền tố
  `STEP_FAIL`, SERP thiếu…) thì chạy lại tối đa `automation.rerun_max` (6) lần liên tiếp rồi dừng ở NEEDS_HUMAN
  (`jobs.last_error` = "autopilot dừng…"; chạy tiếp: `run_job.py --resume <job_id>`). Lý do bắt đầu bằng "YMYL" không
  bị duyệt thay. `rerun_max: null` = chạy lại không giới hạn như cũ.
- **Chi phí**: lượt gọi Claude bị cắt (`max_tokens`) / bị từ chối được ghi vào `prompt_runs` (status `truncated` /
  `refused`) và cộng vào `jobs.cost_usd`; `cost.monthly_cap_usd` được thực thi.
- **Khác**: PUBLISH cần FinalPackage; TRIAGE_GATE luôn ghi branch/mode/ymyl; tải URL chặn địa chỉ nội bộ
  (`SEO_ALLOW_PRIVATE_URLS=1` để tắt khi thử), không cache lỗi tải, URL sai định dạng không làm bước đổ vỡ; regex số
  liệu ("5 triệu chứng", "2 đồng hồ" không còn là tiền; nhận "50K", "2.5%", "1,000,000 đồng"); P2b quét số trên text
  chưa datamark; P5 so khớp excerpt như validator; FACT_APPLY bỏ qua claim_id lạ; rework từ chối bài rỗng; output cache
  không còn đạt validator bị hạ thành `invalid`; blob đọc được sau khi chuyển máy; Postgres chỉ nghe ở 127.0.0.1 và
  DSN mặc định dùng `127.0.0.1`; extension thử gửi kết quả lại 3 lần và không kẹt slot tab khi reload lỗi.

## 13. Nhiều website, nghiên cứu cho P4, PII, chi phí API (2026-10-04)

Test: 259 pass.

**Site (migration 009, `pipeline/sites.py`, `scripts/site_tool.py`).** Mỗi website có hồ sơ: tên, địa chỉ, tác giả mặc định,
trang nội bộ (chèn link), kho dữ liệu riêng. Form tạo job có ô chọn Site (+ thêm); `job.yaml` dùng `site_id:`.
- Kho hai tầng trong `author_knowledge`: **tầng site** (`site_id` có giá trị, `reusable = true`, `author_id = site:<id>`)
  dùng cho mọi bài của site, không giới hạn số bài; **tầng tác giả** (`site_id` NULL) như cũ, mỗi mục tối đa
  `knowledge.max_reuse_per_entry` bài. Job chỉ thấy kho của tác giả mình + site mình: dữ liệu site A không lọt sang site B.
- Tác giả có `site_id` (NULL = dùng chung). Giọng văn tự học gắn theo tác giả nên mỗi site nên có tác giả riêng.
- Dựng kho tầng site, hai bước có người duyệt ở giữa:
  `site_tool.py draft-kho <site> <url trang của site>…` → model chỉ chép câu nguyên văn trên site, code so khớp từng
  câu (không khớp thì loại), tự khai `facts` cho số %/tiền/ngày → `sites/<site>/kho.draft.yaml`;
  người duyệt xoá/sửa; `site_tool.py kho <site> <file>` để nạp. Số liệu trong kho được coi là dữ kiện đã duyệt của
  site (không qua fact-check web): chỉ giữ con số site chịu trách nhiệm.
- `site_tool.py pages <site> --sitemap` (hoặc CSV url,title) nạp trang nội bộ.

**Nghiên cứu cho P4 (`gap_research` trong config, `m0._gap_research`).** GAP_IG_AI (standard/heavy) tự tìm bằng Serper theo
PAA + keyword dạng câu hỏi + chủ đề (tối đa `max_searches`), tải trang (bỏ trang bị chặn và trang job đã lấy làm tư
liệu) và đưa các khối `[Nguồn nghiên cứu | URL]` vào prompt; P4 chỉ được ghi `new_claims` với URL của các khối đó,
NEW_FACT_CHECK kiểm lại từng câu. Kết quả lưu ở artifact `GapResearch` (vòng revise dùng lại). Lưu ý đo thật: Serper không
trả PAA tiếng Việt; job không có keyword dạng câu hỏi thì chỉ tìm theo chủ đề và dễ ra trang danh mục (ít giá trị) —
nhập câu hỏi vào ô PAA khi tạo job để nghiên cứu có trọng tâm.

**PII (`vnseo/pii.py`).** Số điện thoại nhận theo hình dạng nhóm số rồi đếm chữ số: thêm `(+84) 912 345 678`,
`84 912…`, `0084…`, `0912 - 345 - 678`, `(028) 3822 1234`, hai số cách nhau dấu phẩy; không còn nuốt số đứng cạnh
("0912 345 678 2 lần") hay bắt nhầm `0.123456789`. CCCD/CMND có nhãn và STK kèm tên ngân hàng cũng được che.

**Chi phí.** Lượt gọi bị cắt / bị từ chối của OpenAI và Gemini qua API cũng mang usage để runner cộng vào chi phí job
(trước đó mới có Anthropic).

## 14. Cookie Gemini tự đồng bộ từ extension (2026-10-04)

Proxy Gemini ngừng hoạt động khi cookie hết hạn (xảy ra khi Docker tắt lâu: proxy chỉ tự gia hạn cookie lúc đang chạy).
Extension (bản 0.2.0, thêm quyền `cookies`) đọc header Cookie của `gemini.google.com` trong Chrome đã đăng nhập và gửi về
`POST /external/gemini-cookies` mỗi 10 phút và khi Chrome khởi động, kèm số tài khoản `/u/<số>/` của tab Gemini đang mở.
Server (`pipeline/gemini_cookies.py`, config `providers.gemini.cookie_sync`): proxy còn liệt kê được model thì bỏ qua;
proxy báo hết hạn thì ghi cookie mới vào `<proxy_dir>/.env` (giá trị bọc nháy đơn) và chạy `docker compose up -d
--force-recreate` trong thư mục đó. Cùng một cookie không được áp lại trong `min_interval_seconds` (tránh khởi động lại
liên tục khi lỗi không phải do cookie). Giá trị cookie không vào log, DB hay response.
Cần: Docker đang chạy; Chrome đăng nhập đúng tài khoản Gemini; web app chỉ nghe ở 127.0.0.1 và `SEO_EXTENSION_TOKEN`
không để mặc định `devtoken` (ai gọi được endpoint này là thay được cookie của proxy). Sau khi sửa extension phải Reload ở
`chrome://extensions` và chấp nhận quyền mới.
