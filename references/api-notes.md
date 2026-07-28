# API 參考筆記

免費學術 API 的細節、限制與踩坑筆記。`scripts/lit_api.py` 已封裝好這些,此文件供除錯與理解回傳欄位用。

## OpenAlex(search 的自動備援)

- 端點:`https://api.openalex.org/works`,免金鑰、額度非常大方(每日 10 萬次)。
- `search` 指令在 Semantic Scholar 持續 429 時**自動**改走 OpenAlex,回傳帶 `"fallback": "openalex"` 標記——結果同樣可信(有摘要、被引數、DOI、OA 連結),照常使用,不必重試 S2。
- 摘要是倒排索引格式,腳本已還原成文字;少數文獻摘要因授權缺失為 null。
- **相關性排序比 S2 噪**(實測某查詢 10 筆僅 2 筆切題):走 OpenAlex 備援的結果要更嚴格用摘要篩選,別因為它排前面就選。
- 2026-07-28 實測:S2 無金鑰壅塞整段時間,五組搜尋有四組靠 OpenAlex 完成。

## Semantic Scholar(找文獻主力)

- 端點:`https://api.semanticscholar.org/graph/v1/`
- 涵蓋:約 2 億篇,CS/AI 領域最全;有摘要、被引數、跨庫 ID(DOI/arXiv/PubMed)、開放取用 PDF 連結。
- 速率:無 key 走共享池,**很容易 429**(腳本已內建 1.5s 間隔 + 退避重試,連續 verify 多筆會慢,屬正常);有 key 約 1 req/s。key 免費申請:https://www.semanticscholar.org/product/api
- `search`(`/paper/search`):關鍵字相關性排序。**查詢詞用英文**;布林語法不支援,就是普通關鍵字。
- `verify` 用的 `/paper/search/match`:標題精確配對,回傳含 `matchScore`;完全查無時回 404(腳本轉為空 candidates)。
- 欄位陷阱:`venue` 常是空字串或縮寫;`abstract` 可能為 null(出版社不給),此時支持度只能標 ❓;`year` 偶爾是線上先行年份,與正式卷期年差 1 年屬常見,不算書目錯誤。
- `batch`(`POST /paper/batch`):一次最多 500 筆,官方建議的高效做法,查整份引用列表先用它。**not_found 陷阱**:S2 的 DOI 對應不完整,實測 `DOI:10.5665/sleep.5552`(SLEEP 期刊,Crossref 存在)在 S2 回 not_found——batch/paper 查無只代表 S2 沒收錄,存在性判定一律走 `verify` 雙源。
- 官方速率(2026-07 教學文件):有 key 後全端點統一 1 req/s,審核後可能調高;腳本有 key 時的 1.0s 間隔即按此設。

## Crossref(書目權威)

- 端點:`https://api.crossref.org/works`
- 這是 DOI 註冊機構的官方資料,**書目欄位以它為準**(期刊名、卷期頁碼、作者拼字)。
- 速率:寬鬆;`.env` 設 `CROSSREF_MAILTO=你的email` 可進 polite pool 更穩。
- `query.bibliographic` 是模糊搜尋,回傳按相關性排序——**第一名不一定是正確配對**,一定要看 `match.title_sim`(腳本已算好,>0.93 才算同一篇)。
- 涵蓋限制:沒有摘要;arXiv 純預印本不在裡面(除非已正式發表);少數華文期刊有 DOI 但欄位是英譯,比對時注意。
- `type` 欄位:`journal-article` / `proceedings-article` / `book-chapter` 等,查核「期刊 vs 會議」寫錯時有用。

## arXiv(預印本)

- 端點:`https://export.arxiv.org/api/query`,回 Atom XML(腳本已轉 JSON)。
- 速率:官方建議 3 秒 1 次(腳本已內建)。
- 查詢語法:`all:keyword`、`ti:"exact title"`、`au:lastname`、`cat:cs.AI`,可用 `AND`/`OR`。腳本對無前綴的查詢自動加 `all:`。
- 注意:同一篇可能有多版本(v1, v2…),腳本回傳已去掉版本號。**引用時優先確認有無正式發表版**(用 verify 查 Crossref):已正式發表卻引 arXiv 版是常見的引用品質問題。
- arXiv 官方無 BibTeX/RIS 端點,`export --arxiv` 是從 metadata 組出來的,型別為 `@misc`/`GEN`(preprint)。
- **`export.arxiv.org` 會不定期整台掛掉**(TCP 連得上但 TLS 後無回應;主站 arxiv.org 正常時也會發生,2026-07-28 實測過一次)。腳本已內建自動備援:`arxiv` 搜尋與 `export --arxiv` 失敗時改走 Semantic Scholar(它完整收錄 arXiv),回傳 JSON 會帶 `"fallback": "semanticscholar"` 標記。看到這個標記照常用結果即可,不用重試 arXiv。

## snowball(引文滾雪球)

- `citations`(誰引它):id 為 DOI 時**優先走 OpenAlex**(伺服器端按被引數排序,能挖到高影響力的後續研究);OpenAlex 失敗才退 S2——S2 此端點按時間序,只會回傳最新的引用者,選不到高被引的。
- `references`(它引誰):走 S2(多抓一頁後按被引數重排取前 N),實測能直接挖出千次被引的奠基文獻。
- 用途:對已驗證的高相關文獻補洞——`references` 往回追經典,`citations` 往前追最新重要進展。關鍵字搜不到的主題(太新、用詞特殊)靠這招。
- 注意:被引上千的文獻其 citations/references 都只回傳前 N 筆,不是全集。

## quality_warnings(品質紅旗)

`search`/`snowball` 每筆結果可能帶 `quality_warnings` 陣列,規則:
- `0被引(發表已滿一年)`、`低被引(≤3)且發表逾3年`——新文獻 0 被引正常,所以有年齡門檻
- `期刊不在DOAJ也非CORE收錄,品質需人工確認`——僅 OpenAlex 結果有此判斷(S2 無期刊層資料);DOAJ/CORE 都不在不必然是掠奪性期刊,但值得起疑
- `無DOI`、`無期刊/會議名`

紅旗不是否決,是「只能當佐證+報告中必須明示」的信號。2026-07 實戰教訓:兩篇短影音文獻(0被引/非主流期刊)若有此機制會自動被標,不必靠驗證 agent 起疑。

## verify 的判定邏輯

`verdict_hint` 只是初篩,最終判定由你(LLM)綜合:

| verdict_hint | 條件 | 你該做的 |
|---|---|---|
| `found` | title_sim ≥ 0.93 且年差 ≤ 1 | 直接進書目比對 |
| `similar_found` | title_sim ≥ 0.8 | 人工比對:副標題省略?preprint/正式版?還是真的寫錯? |
| `not_found` | 其他 | 換詞重查一次;仍無 → 報告寫「三庫皆查無」 |

- `match.author_overlap`:查詢作者姓氏出現在候選作者中的比例;低於 0.5 且 title_sim 又不高時,幾乎可斷定不是同一篇。
- `match.year_diff`:1 年差常見(online-first vs 紙本),≥2 年才視為書目錯誤。

## Google Scholar 工具(中文文獻用)

- 會**間歇性限流回 0 結果**(實測同一查詢稍後重試即命中):GS 回 0 的證據力遠低於結構化 API 的查無——先重試一次;仍 0 就標「GS 查證失敗」而非「查無此文獻」。
- 精確查核用「"完整標題" 作者」的組合查詢;回傳的 cited_by 與 authors_and_publication 可做粗略書目比對,但欄位不完整,結論一律標「信心中等」。

## PDF 文字抽取(方程式查核用)

- Claude 的 Read 工具讀 PDF 需要 poppler;Windows 常沒裝。降級路徑:PyMuPDF(`import fitz`)的 `page.get_text()` 抽文字,實測公式周邊的**敘述句**(如 "divide each by √dk")比公式本身的字元轉換可靠——比對時優先找敘述句證據。

## 書籍類文獻的驗證盲點

- Crossref/S2 對專書(尤其 2010 年前)收錄很差,`verify` 常只命中**同名書評**(title_sim=1.0 但作者是書評人)——這不是配對錯誤,是資料庫特性。
- 判讀方式:書評條目本身就是**存在性的間接證據**(有期刊書評=書存在);但書目細節(出版社/年份/版次)書評不能證實,報告標「間接確認,建議人工核 ISBN」。
- OpenLibrary/Google Books API 可補查,但實測(2026-07)命中率與可用性都不穩,失敗就直接標人工,不要空轉。

## 常見錯誤處理

| 症狀 | 原因 | 處理 |
|---|---|---|
| 大量 429(重試後仍失敗) | S2 共享池尖峰 | 等 1–2 分鐘再跑,或申請 S2_API_KEY |
| export 回 404 | DOI 打錯或是 DataCite 的 DOI | 用 crossref-doi 先確認 DOI 存在 |
| export 的 RIS 有空 TI 或截斷 T2 | doi.org 對部分出版社(如 ACL Anthology)的 metadata 品質差 | **匯出後不能只數 TY/ER,要 grep 檢查 `TI  - ` 後非空**;缺欄用 S2/Crossref 回傳值手補 |
| verify 候選全是無關文獻 | 標題太短/太泛 | 加上副標題或第一作者再查 |
| arXiv 查無但確定存在 | 查詢詞含特殊符號 | 改用 `ti:"部分標題"` 精確查 |
