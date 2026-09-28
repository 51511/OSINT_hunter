(重要聲明：這東西是無聊用Grok寫的，我嘗試過這東西實戰還滿爛的）

# 🕵️ OSINT Hunter

LLM 驅動的**明網 OSINT 代理**。架構仿 [Robin](https://github.com/apurvsinghgautam/robin)（暗網版），
但整條管線換成明網 + [SearXNG](https://github.com/searxng/searxng)，並加上**多輪自動反查**與**程式層防幻覺**。

你只要說目標，它會：

```
展開查詢 → SearXNG 搜尋 → LLM 篩選 → 抓取頁面 → 程式抽取 ID → LLM 評分 → 驗證引文 → 新線索反查（下一輪）→ 報告
   ▲                                                                                        │
   └────────────────────────────────── 最多 N 輪 ──────────────────────────────────────────┘
```

## 與 Robin 的差異

| | Robin | OSINT Hunter |
|---|---|---|
| 網路 | Tor / .onion | 明網（SearXNG） |
| 查詢 | 單一 ≤5 字查詢 | 每輪 8~12 個，帶 `site:` 語法，多平台 |
| 迴圈 | 單向；pivot 需人工點擊 | **自動多輪**，用上輪發現的新線索反查 |
| 搜尋結果 | 只有 title + link | 另含 **snippet**（留言片段本身就是證據） |
| ID 抽取 | 交給 LLM | **程式抽取**（網址規則 + PTT 推文），LLM 只能評分 |
| 防幻覺 | Prompt 約束 | Prompt + **程式驗證**（見下） |

## 防幻覺機制（程式層，不靠 LLM 自律）

1. **LLM 不能新增 ID**：候選 ID 由程式從網址/內文抽出；LLM 只能對清單內的 ID 評分，自行捏造的一律丟棄。
2. **引文驗證**：LLM 給的每句「逐字引文」都會比對真實頁面內容（NFKC 正規化、支援省略號），對不上標為「未驗證」。
3. **自動降級**：無任何已驗證引文 → `low`；`high` 需 ≥2 則已驗證證據；證據僅來自搜尋摘要 → 最高 `medium`。
4. **線索過濾**：LLM 提出的 `new_leads` 必須逐字出現在已抓頁面中，否則視為杜撰丟棄。
5. **報告必附限制與風險**，並區分事實與推論。

## 安全設計

- **SSRF 防護**：擋 loopback、私網、link-local（含 `169.254.169.254` 雲端 metadata）、非 http(s)、`.onion/.local/.internal`；重新導向後的最終網址也會再檢查。
- **遵守 robots.txt**（可關閉）。
- **snippet-only 網域**：IG / Threads / FB / X / TikTok / LinkedIn 需登入或反爬，**不硬爬**，只用搜尋摘要。
- 下載大小上限 1.5MB；來源網頁內容一律視為資料，prompt 明訂忽略其中的指令。
- Docker 埠只綁 `127.0.0.1`。

## 快速開始

### 1. 起 SearXNG

```bash
# 先改 searxng/settings.yml 的 secret_key：
openssl rand -hex 32

docker compose up -d searxng
curl 'http://localhost:8080/search?q=test&format=json' | head -c 200   # 有 JSON 就成功；403 = 沒開 json
```

### 2. 設定 LLM

```bash
cp .env.example .env
# 填入 ANTHROPIC_API_KEY（或改用 OpenAI / Ollama / Groq…，見 .env.example）
```

### 3. 執行

**網頁介面**
```bash
pip install -r requirements.txt
streamlit run app.py          # http://localhost:8501
```

**命令列**
```bash
python -m osint_hunter.cli --check                       # 連線檢查
python -m osint_hunter.cli "小明" -a "台北 平面設計師" -r 3
python -m osint_hunter.cli "小明" -a "台大 資工" -k "xmdesign,ming_d"
```

**全 Docker**
```bash
docker compose up -d          # 同時起 SearXNG + App
```

### 換模型

```bash
# OpenAI
LLM_PROVIDER=openai LLM_MODEL=gpt-4.1 OPENAI_API_KEY=... python -m osint_hunter.cli "小明"
# 本地 Ollama
LLM_PROVIDER=ollama LLM_MODEL=qwen2.5:14b python -m osint_hunter.cli "小明"
# Groq / LM Studio / OpenRouter（OpenAI 相容）
LLM_PROVIDER=custom OPENAI_BASE_URL=https://api.groq.com/openai/v1 OPENAI_API_KEY=... LLM_MODEL=... python -m osint_hunter.cli "小明"
```

> 本地小模型的 JSON 輸出較不穩，程式已內建容錯解析與「壞 JSON 回饋重試」，但**評分品質仍建議用較強的模型**。

## 專案結構

```
osint_hunter/
  config.py     設定（環境變數）
  models.py     資料模型
  llm.py        LLM 抽象層（Anthropic / OpenAI 相容）+ JSON 容錯解析
  search.py     SearXNG 客戶端（限流退避、去重、健康檢查）
  scrape.py     抓取（SSRF 防護、robots、PTT 感知的文字抽取）
  handles.py    程式抽取 ID（網址規則 + PTT 推文/作者 + @提及）
  prompts.py    所有提示詞
  verify.py     引文驗證 + 信心降級 + 線索過濾
  pipeline.py   多輪調查引擎
  store.py      JSON / Markdown 存取
  cli.py        命令列
app.py          Streamlit 介面
tests/          25 個測試（單元 + 端對端 + 真實 HTTP I/O + UI）
```

```bash
python -m pytest tests/ -q
```

## 已知帳號（`-k`）怎麼用

給了 `-k bumpyyy_1210` 之後，系統會：

1. **強制注入查詢**：第一輪自動加入 `"handle"`、`site:instagram.com handle` 等高優先查詢。
2. **預先 seed 候選池**：把該帳號以多平台 canonical URL 放入候選 ID，讓 LLM 可以評分。
3. **從搜尋結果對上**：若任一 hit 的 URL / 標題 / 摘要出現該 handle，會標成對應平台並帶 profile URL。

因此「已知帳號 + 錨點」比「只給中文名」容易產出有意義的候選。IG/Threads/FB 仍多半只能靠 snippet，信心上限通常是 `medium`。

## 已知限制（請務必閱讀）

1. **IG / Threads / FB 內容基本抓不到**。SearXNG 只能撈到「被搜尋引擎索引的公開頁面」；這幾個平台
   只能靠搜尋摘要判斷，所以最高信心只到 `medium`。這是刻意設計，不是 bug。
2. **同名同姓**是最大風險。錨點（地區、職業、學校、已知暱稱）給越多越準；只給一個常見名字，結果會很雜。
3. **Google 經 SearXNG 常被限流**。預設同時開 Google / Bing / DuckDuckGo / Brave 分散風險，
   並在查詢間插入延遲（`SEARCH_DELAY`）。若大量 429，調大延遲或減少 `QUERIES_PER_ROUND`。
4. **PTT 的 ID 抽取**只認英數 ID；中文暱稱（括號內）不會被當成 ID。
5. 程式無法判斷網頁內容是否過時或被刪除。
6. **驗證只能證明「引文確實存在於來源」，不能證明「這個人就是目標」**。最終判斷請自行交叉比對。

## ⚠️ 使用責任

此工具彙整的是**公開資料**，但把零散公開資料串成個人側寫，本身就可能構成隱私侵害。
台灣《個人資料保護法》對個資的蒐集、處理、利用有明確規範，「資料公開」不等於「可任意利用」。

適當用途：驗證詐騙/冒名帳號、查證交易對象、尋找失聯者（有正當理由）、資安紅隊授權測試、盡職調查。
請勿用於跟蹤、騷擾、肉搜或其他侵害他人權益的行為。**使用者自行承擔法律責任。**
