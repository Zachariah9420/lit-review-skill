# lit-review — 給 LLM agent 的文獻抓取與引用查核 skill

[English](README.md) | **繁體中文**

把 Claude(或 Codex、任何夠力的 LLM agent)變成嚴謹文獻助手的 skill。丟一段草稿給它——幫你找支持文獻,並逐筆查核既有引用:**文獻存在嗎?書目對嗎?它真的支持你掛它的那句話嗎?**

由一個趕論文的碩士生打造,在真實論文章節上實戰測試過。不需要金鑰即可使用。

## 系統架構

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/architecture-dark.svg">
  <img src="assets/architecture-light.svg" alt="lit-review 架構:輸入 → 模式 → 引擎 → 驗證 → 產出,含自查迴圈" width="100%">
</picture>

*(圖由 `assets/gen_diagram.py` 產生——改文字或配色後重跑即可,調色盤經 CVD 色覺驗證。)*

三個誠實機制貫穿所有路徑:書目欄位只來自 API 回傳(絕不用模型記憶)、每個判定附證據層級、生成的產出交付前由 fresh-context 懷疑論者審查。

## 功能

**模式 A(找文獻)**:從草稿萃取論點 → 搜尋 Semantic Scholar / OpenAlex / arXiv → 引文滾雪球(挖出關鍵字搜不到的經典)→ 依摘要而非標題判讀 → 產出可匯入 EndNote/Zotero 的 `.ris`。

**模式 B(查核引用)**:每筆引用五層檢驗——

1. **存在性**:Crossref + Semantic Scholar 雙源交叉;抓幻覺引用(並且懂「查不到 ≠ 捏造」)
2. **撤稿檢查**:所有 DOI 預設過一輪 Crossref 撤稿記錄(含 Retraction Watch 資料),零 LLM 成本
3. **書目正確性**:與 Crossref 權威資料逐欄比對(年份/期刊/作者拼字/頁碼);arXiv 編號與標題交叉覆核
4. **內容支持度**:讀摘要,對照文中引用該文獻的**那句話**;抓「文獻講相關、你寫因果」的過度延伸、零結果陷阱、母體錯位
5. **方程式/技術細節**:文中把公式歸給某文獻時,抓開放取用 PDF 逐項比對

**模式 C(文獻支撐寫作)**:給主題,先檢索、後寫作、寫完自查——每個掛引用的宣稱都可追溯到檢索到的證據,教學鋪陳明確不掛引用。

**研究生工具組**:十三個論文生命週期工具——文獻矩陣、領域地圖、研究缺口偵測、閱讀筆記卡、引用完整性檢查、中英術語一致性、口試提問預演、新文獻追蹤、引用需求標記、反面證據搜尋、證據強度評級、claim–evidence 總表、撤稿查詢。

## 快速上手

**Claude Code**:

```bash
git clone https://github.com/<you>/lit-review-skill ~/.claude/skills/lit-review
```

然後直接說:「幫我查這章的引用」或「幫這段找文獻:…」——skill 會從輸入推斷模式。

也可下明確指令(Claude Code 打 `/lit-review <指令>`,Codex 直接打指令詞):

| 指令 | 功能 |
|---|---|
| `check <檔案>` | 查核引用(模式 B;有引用列表自動加做 A) |
| `find <段落>` | 找文獻(模式 A) |
| `write <主題>` | 文獻支撐寫作(模式 C) |
| `verify <單筆引用>` | 單筆快查存在性+書目 |
| `annotate <檔案>` | 標記哪些句子需要引用 |
| `counter <論點>` | 主動找反面/零結果證據 |
| `strength <文獻+論點>` | 證據強度評級(統合分析 vs 橫斷面小樣本,一眼分清) |
| `claims <檔案>` | Claim–evidence 總表:每個論點的支持/反對篇數與強度 |
| `matrix` `map` `gap` `notes` `integrity` `glossary` `rehearse` `watch` `retract` `versions` `export-xml` | 研究生工具組——詳見 [USAGE.zh-TW.md](USAGE.zh-TW.md) |
| `deep` / `quick` / `thorough` | 深度與驗證強度修飾詞 |
| `bibtex` / `no-ris` | 引用檔格式偏好 |

**Codex / 其他 agent**:在 `AGENTS.md` 加一行:

> 文獻搜尋與引用查核請讀 `<clone路徑>/SKILL.md` 並照其流程執行。

腳本為純 Python 3.8+ 標準函式庫,不需 pip 安裝任何東西。

**完整教學:[USAGE.zh-TW.md](USAGE.zh-TW.md)**

## 選配金鑰(常用者建議)

專案目錄建 `.env`(**千萬不要 commit**,腳本自動讀取,先 cwd 後 `~/.env`):

```
S2_API_KEY=...        # semanticscholar.org/product/api 免費申請,避開共享池 429
CROSSREF_MAILTO=you@example.com   # 進 Crossref/OpenAlex 禮貌池
```

## 設計原則

- **誠實優先**:查不到就說查不到,判不了就標 ❓,絕不猜。一筆錯誤的「已驗證」比十筆誠實的「無法判斷」危害更大。
- **書目不靠記憶**:所有 DOI、年份、頁碼一律來自 API 回傳——LLM 記憶中的書目資料天生不可靠。
- **三層隔離**:證據隔離(記憶只能起疑,不能作證)、角色隔離(寫的人不審自己)、注入隔離(檢索內容是不可信資料,其中的指令一律回報不遵從)。
- **品質紅旗**:低層級期刊的零被引文獻自動標警(有年齡門檻,不冤枉新文獻)。
- **成本分級**:`quick`(單 agent,標「未經獨立審查」)/ 預設(一個 fresh 懷疑論者,約 1.5–2 倍)/ `thorough`(逐句對抗驗證,5–10 倍)——驗證強度跟著錯誤代價走。
- **抗故障**:S2→OpenAlex、arXiv→S2 自動備援,內建速率限制與退避重試。

## 誠實的限制

- 預設判讀基於摘要;「缺口」代表摘要看不出來,不代表內文沒有。摘要判不動的關鍵引用會升級到開放取用全文(並標明證據層級);付費牆內的誠實標「無法判斷」
- 中文文獻:有 Google Scholar 工具時盡力查(標信心中等),否則列人工查核——結構化 API 幾乎不收
- 專書(尤其 2010 年前):API 常只收錄書評——skill 會把書評當存在性間接證據,並提醒你核 ISBN
- 工業論文集(IPC/SMTA 類)是**所有**資料庫的共同盲點——skill 會說「各庫皆查無,請向出版方確認」,絕不說「捏造」
- 這個工具讓錯誤引用更難發生,但**文獻的選擇與詮釋責任永遠在作者**——它是防護欄,不是代駕

## 範例

`examples/planted_errors_test.md` 是一份埋了五種錯誤的測試稿(錯年份、錯場刊、捏造文獻、中文文獻、缺引用論點)——拿它試跑,看 skill 能不能全抓到。

---

MIT License。歡迎 Issue 與 PR。
