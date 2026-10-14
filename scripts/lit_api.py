#!/usr/bin/env python3
"""lit_api.py — Semantic Scholar / Crossref / arXiv 統一查詢 CLI(純標準函式庫，Python 3.8+)

子命令：
  search   <query>            Semantic Scholar 關鍵字搜尋(找相關文獻)
  arxiv    <query>            arXiv 搜尋(預印本)
  verify   --title "..."      跨 Crossref + Semantic Scholar 驗證一筆文獻是否存在、書目是否正確
  paper    <id>               取單篇詳細資料含摘要(id 可為 DOI:10.x/y、ARXIV:2301.12345、S2 paperId)
  crossref-doi <doi>          Crossref 權威書目資料(DOI → 完整欄位)
  export   --doi 10.x/y --format ris|bibtex     產生引用檔(doi.org content negotiation)
  export   --arxiv 2301.12345 --format ris|bibtex

環境變數(自動讀取 cwd 的 .env；金鑰只放 .env，不進程式碼):
  S2_API_KEY       選填，Semantic Scholar API key(沒有也能跑，只是速率較低)
  CROSSREF_MAILTO  選填，你的 email(進 Crossref polite pool，速率較寬鬆)

所有輸出皆為 UTF-8 JSON(export 除外，輸出純文字 RIS/BibTeX)。
"""
import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "title,abstract,year,authors,venue,externalIds,citationCount,openAccessPdf"
CROSSREF_BASE = "https://api.crossref.org"
ARXIV_BASE = "https://export.arxiv.org/api/query"
OPENALEX_BASE = "https://api.openalex.org"

_last_call = {}
_rate_lock = threading.Lock()


def load_dotenv():
    # 先讀 cwd(專案層)，再讀 HOME(全域備援)；已設定的環境變數不覆蓋。
    # 注意：刻意不讀 skill 目錄自己的 .env——金鑰放在 skill 資料夾會隨打包外流。
    candidates = [os.path.join(os.getcwd(), ".env"),
                  os.path.join(os.path.expanduser("~"), ".env")]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass


def _reserve_slot(bucket, min_interval):
    """在鎖內預約下一個發送時段，回傳需要等待的秒數（實際 sleep 在鎖外做）。

    多執行緒同時呼叫時，若只是「讀 _last_call → sleep → 更新」，兩條線會讀到
    同一個舊值而同時發出請求，等於沒有節流。預約制讓每條線各自拿到不重疊的時段。
    """
    with _rate_lock:
        now = time.time()
        earliest = max(now, _last_call.get(bucket, 0.0) + min_interval)
        _last_call[bucket] = earliest
        return earliest - now


# 單次連線的 socket 逾時、重試次數,以及「一次 http_get 最久會花多久」。
# 最後那個數字必須是**推導**出來的:呼叫端(verify_one)要等這些執行緒,而它等的
# 時間如果比這裡短,執行緒會在還沒回報成功或失敗之前就被丟掉——那正是本檔曾經
# 發生的事(join 等 90 秒,而這裡最壞要 162 秒),結果是「來源沒回答」被讀成
# 「來源說沒有」。兩個數字之間沒有任何東西綁著,所以它們各自漂走了。
SOCKET_TIMEOUT = 30
HTTP_RETRIES = 3
# 4 次嘗試各自可能耗掉整個 socket 逾時,加上 429 路徑的退避 6+12+24。
HTTP_WORST_CASE = ((HTTP_RETRIES + 1) * SOCKET_TIMEOUT
                   + sum((2 ** i) * 6 for i in range(HTTP_RETRIES)))

# 可重試的 5xx／暫時性狀態碼。504(gateway timeout)、408(request timeout)與
# Cloudflare 的 522／524 都是「再試一次很可能就好」,漏掉它們等於把一次暫時性
# 失敗變成一次永久性的「查無」。
RETRYABLE_STATUS = (500, 502, 503, 504, 408, 522, 524)

# Semantic Scholar 沒金鑰時共用一個很擠的池子，429 是常態。指引要在**第一次** 429 就給、
# 而且要能照著做：去哪申請、.env 要寫哪一行、程式從哪讀 .env。以前只在重試全數用完、
# 使用者已經等了一分多鐘之後才說「長期解法是申請 key」，卻沒說去哪申請、怎麼放。
S2_API_PAGE = "https://www.semanticscholar.org/product/api"
S2_KEY_HINT = ("免費金鑰申請頁（Semantic Scholar 官方）：" + S2_API_PAGE + " ；"
               "取得後在 .env 加一行：S2_API_KEY=<your key> ；"
               "程式先讀目前資料夾的 .env，再讀 ~/.env。")
_s2_429_warned = False
_s2_429_seen = False     # 這個行程裡 Semantic Scholar 回過 429（不論有沒有金鑰）：main() 的 hint 靠它歸因


def _warn_s2_429(url, will_retry=True):
    """對 Semantic Scholar 的第一次 429，立刻在 stderr 印出申請金鑰的指引（每個行程只印一次）。
    已設 S2_API_KEY 還遇到 429 就不是缺金鑰，不印。stdout 的 JSON 不受影響。"""
    global _s2_429_warned, _s2_429_seen
    if "semanticscholar.org" not in url:
        return
    _s2_429_seen = True          # 先記下來，有沒有金鑰都記：search 退到 OpenAlex 之後，最後拋出的
    if _s2_429_warned or os.environ.get("S2_API_KEY"):   # 例外是 OpenAlex 的，光看它看不出 S2 曾經 429
        return
    _s2_429_warned = True
    # 只在真的還會重試時才說「會自動退避重試」：最後一次嘗試的 429 接著就是 raise，
    # 說會重試是承諾程式不會做的事。
    tail = "程式會自動退避重試。" if will_retry else "重試已用完，這次指令以 429 結束。"
    print("Semantic Scholar 回 429（無金鑰時共享池很擠）；" + tail + S2_KEY_HINT, file=sys.stderr)


def http_get(url, headers=None, min_interval=0.0, bucket="default",
             retries=HTTP_RETRIES, data=None):
    """帶速率限制與 429/5xx 退避重試的 GET(data 給值時為 POST)。執行緒安全。"""
    wait = _reserve_slot(bucket, min_interval)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, data=data,
                                 headers=headers or {"User-Agent": "lit-review-skill/1.0"})
    last_err = None
    for attempt in range(retries + 1):
        try:
            if attempt:      # 重試也要重新預約時段，否則退避後可能與別的執行緒撞在一起
                w = _reserve_slot(bucket, min_interval)
                if w > 0:
                    time.sleep(w)
            with urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                raise
            if e.code == 429:
                _warn_s2_429(url, will_retry=attempt < retries)
            if e.code == 429 and attempt < retries:
                time.sleep((2 ** attempt) * 6)  # S2 無 key 時共享池很擠，退避要夠長
                continue
            if e.code in RETRYABLE_STATUS and attempt < retries:
                time.sleep((2 ** attempt) * 2)
                continue
            raise
        except OSError as e:  # 含 URLError、socket TimeoutError、連線重置
            last_err = e
            if attempt < retries:
                time.sleep((2 ** attempt) * 2)
                continue
            raise
    raise last_err


# ---------- Semantic Scholar ----------

def _unesc(v):
    """解 HTML entity：Crossref 的期刊名常帶 &amp;（如 Soldering &amp; Surface Mount
    Technology），原樣傳遞會寫進 RIS，使用者匯入 EndNote 後期刊名字面帶 &amp;。"""
    return html.unescape(v) if isinstance(v, str) else v


def s2_headers():
    key = os.environ.get("S2_API_KEY")
    return {"x-api-key": key, "User-Agent": "lit-review-skill/1.0"} if key else {"User-Agent": "lit-review-skill/1.0"}


def s2_interval():
    return 1.0 if os.environ.get("S2_API_KEY") else 1.5


def norm_s2(p):
    # 摘要不在取得層截斷：token 控制由 brief/pick 漏斗負責，截斷會切掉
    # Results/Conclusion，對 counter(零結果證據)與因果判讀特別危險
    # S2 的書目欄位同樣帶 HTML entity（如 Group Processes &amp; Intergroup Relations）。
    # 先前只在 Crossref 路徑解碼，S2 這條漏掉：&amp; 會一路流到 export-xml 被二次
    # 逸出，匯入 EndNote 後期刊名字面顯示 &amp;。
    ext = p.get("externalIds") or {}
    abstract = p.get("abstract")
    return {
        "source": "semanticscholar",
        "paperId": p.get("paperId"),
        "title": _unesc(p.get("title")),
        "year": p.get("year"),
        "authors": [_unesc(a.get("name")) for a in (p.get("authors") or [])],
        "venue": _unesc(p.get("venue")) or None,
        "doi": ext.get("DOI"),
        "arxiv": ext.get("ArXiv"),
        "citationCount": p.get("citationCount"),
        "openAccessPdf": (p.get("openAccessPdf") or {}).get("url"),
        "abstract": _unesc(abstract),
        "matchScore": p.get("matchScore"),
    }


def deinvert(inv):
    """OpenAlex 的摘要是倒排索引，還原成文字(不截斷，同 norm_s2 的理由)。"""
    if not inv:
        return None
    pos = {}
    for w, idxs in inv.items():
        for i in idxs:
            pos[i] = w
    return " ".join(pos[i] for i in sorted(pos))


def norm_openalex(w):
    src = (w.get("primary_location") or {}).get("source") or {}
    return {
        "source": "openalex",
        "title": w.get("title") or w.get("display_name"),
        "year": w.get("publication_year"),
        "authors": [(a.get("author") or {}).get("display_name")
                    for a in w.get("authorships") or []],
        "venue": src.get("display_name"),
        "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
        "citationCount": w.get("cited_by_count"),
        "openAccessPdf": (w.get("open_access") or {}).get("oa_url"),
        "abstract": deinvert(w.get("abstract_inverted_index")),
        "venue_meta": {"type": src.get("type"), "in_doaj": src.get("is_in_doaj"),
                       "is_core": src.get("is_core")},
    }


def add_quality(e):
    """為單筆結果附加 quality_warnings(紅旗)：供 LLM 判斷是否只能當佐證。"""
    warns = []
    now = time.localtime().tm_year
    year, cites = e.get("year"), e.get("citationCount")
    if not e.get("doi") and not e.get("arxiv"):
        warns.append("無DOI")
    if cites is not None and year:
        age = now - year
        if cites == 0 and age >= 1:
            warns.append("0被引(發表已滿一年)")
        elif cites <= 3 and age >= 3:
            warns.append("低被引(≤3)且發表逾3年")
    if not e.get("venue"):
        warns.append("無期刊/會議名")
    vm = e.get("venue_meta") or {}
    if vm.get("type") == "journal" and vm.get("in_doaj") is False and vm.get("is_core") is False:
        warns.append("期刊不在DOAJ也非CORE收錄，品質需人工確認")
    if warns:
        e["quality_warnings"] = warns
    return e


def openalex_search(query, limit):
    params = {"search": query, "per-page": str(limit)}
    mailto = os.environ.get("CROSSREF_MAILTO")
    if mailto:
        params["mailto"] = mailto
    url = OPENALEX_BASE + "/works?" + urllib.parse.urlencode(params)
    data = json.loads(http_get(url, min_interval=0.2, bucket="openalex"))
    return [norm_openalex(w) for w in data.get("results") or []]


def cmd_search(args):
    params = {"query": args.query, "limit": str(args.limit), "fields": S2_FIELDS}
    if args.year:
        params["year"] = args.year
    url = S2_BASE + "/paper/search?" + urllib.parse.urlencode(params)
    try:
        data = json.loads(http_get(url, s2_headers(), s2_interval(), "s2"))
        out = {"total": data.get("total"),
               "results": [add_quality(norm_s2(p)) for p in data.get("data") or []]}
    except urllib.error.HTTPError as e:
        if e.code != 429:
            raise
        # S2 共享池壅塞時自動改用 OpenAlex(免費、額度大方、同樣有摘要/被引數)
        results = [add_quality(r) for r in openalex_search(args.query, args.limit)]
        out = {"fallback": "openalex",
               "note": "Semantic Scholar 持續 429，已改用 OpenAlex；結果同樣可信，照常使用",
               "results": results}
    print(json.dumps(out, ensure_ascii=False, indent=1))


def s2_get_paper(pid):
    url = S2_BASE + "/paper/" + urllib.parse.quote(pid, safe=":/") + "?" + urllib.parse.urlencode(
        {"fields": S2_FIELDS})
    return norm_s2(json.loads(http_get(url, s2_headers(), s2_interval(), "s2")))


def cmd_paper(args):
    try:
        print(json.dumps(s2_get_paper(args.id), ensure_ascii=False, indent=1))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(json.dumps({"error": "not_found", "id": args.id}, ensure_ascii=False))
            return
        raise


# ---------- Crossref ----------

def crossref_headers():
    mailto = os.environ.get("CROSSREF_MAILTO")
    ua = "lit-review-skill/1.0" + (f" (mailto:{mailto})" if mailto else "")
    return {"User-Agent": ua}


def norm_crossref(it):
    issued = (it.get("issued") or {}).get("date-parts") or [[None]]
    return {
        "source": "crossref",
        "title": _unesc((it.get("title") or [None])[0]),
        "year": issued[0][0] if issued and issued[0] else None,
        "authors": [", ".join(filter(None, [a.get("family"), a.get("given")]))
                    for a in it.get("author") or []],
        "container": _unesc((it.get("container-title") or [None])[0]),
        "doi": it.get("DOI"),
        "type": it.get("type"),
        "volume": it.get("volume"),
        "issue": it.get("issue"),
        "page": it.get("page"),
        "publisher": _unesc(it.get("publisher")),
    }


def crossref_search(title, rows=5, author=None):
    params = {"query.bibliographic": title, "rows": str(rows)}
    if author:
        params["query.author"] = author      # versions 用作者找改名後的正式版
    mailto = os.environ.get("CROSSREF_MAILTO")
    if mailto:
        params["mailto"] = mailto
    url = CROSSREF_BASE + "/works?" + urllib.parse.urlencode(params)
    data = json.loads(http_get(url, crossref_headers(), crossref_interval(), "crossref"))
    return [norm_crossref(it) for it in data.get("message", {}).get("items", [])]


def cmd_crossref_doi(args):
    url = CROSSREF_BASE + "/works/" + urllib.parse.quote(args.doi, safe="/")
    try:
        data = json.loads(http_get(url, crossref_headers(), crossref_interval(), "crossref"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(json.dumps({"error": "not_found", "doi": args.doi}, ensure_ascii=False))
            return
        raise
    print(json.dumps(norm_crossref(data.get("message", {})), ensure_ascii=False, indent=1))


# ---------- arXiv ----------

ATOM = "{http://www.w3.org/2005/Atom}"


def arxiv_query(query, limit=10, id_list=None):
    params = {"max_results": str(limit)}
    if id_list:
        params["id_list"] = id_list
    else:
        params["search_query"] = query
    url = ARXIV_BASE + "?" + urllib.parse.urlencode(params)
    # retries=1:export.arxiv.org 掛掉時盡快失敗，讓呼叫端切換到 S2 備援
    xml_text = http_get(url, min_interval=3.0, bucket="arxiv", retries=1)
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall(ATOM + "entry"):
        raw_id = e.findtext(ATOM + "id") or ""
        # arXiv 查詢有問題時,API 不回 HTTP 錯誤碼,而是回一個「長得像論文」的
        # entry:id 指向 arxiv.org/api/errors、標題就叫 Error、作者 arXiv api core。
        # 照單全收的話,它會一路變成一筆格式完整、可以匯進 EndNote 的引用。
        # 真論文的 id 一定含 /abs/,錯誤 entry 沒有——用這個把它擋掉。
        if "/abs/" not in raw_id:
            continue
        arxiv_id = raw_id.rsplit("/abs/", 1)[-1]
        out.append({
            "source": "arxiv",
            "arxiv": re.sub(r"v\d+$", "", arxiv_id),
            "title": re.sub(r"\s+", " ", e.findtext(ATOM + "title") or "").strip(),
            "year": int((e.findtext(ATOM + "published") or "0000")[:4]) or None,
            "authors": [a.findtext(ATOM + "name") for a in e.findall(ATOM + "author")],
            "abstract": re.sub(r"\s+", " ", e.findtext(ATOM + "summary") or "").strip(),
            "doi": e.findtext("{http://arxiv.org/schemas/atom}doi"),
            "pdf": next((l.get("href") for l in e.findall(ATOM + "link")
                         if l.get("title") == "pdf"), None),
        })
    return out


def cmd_arxiv(args):
    q = args.query
    if not re.search(r"^(all|ti|au|abs|cat):", q):
        q = "all:" + q
    try:
        print(json.dumps({"results": arxiv_query(q, args.limit)}, ensure_ascii=False, indent=1))
    except OSError as e:
        # export.arxiv.org 不時整台掛掉；S2 也收錄 arXiv，自動退到 S2 搜尋
        plain = re.sub(r'\b(all|ti|au|abs|cat):', "", args.query).replace('"', " ").strip()
        params = {"query": plain, "limit": str(args.limit), "fields": S2_FIELDS}
        data = json.loads(http_get(S2_BASE + "/paper/search?" + urllib.parse.urlencode(params),
                                   s2_headers(), s2_interval(), "s2"))
        print(json.dumps({
            "fallback": "semanticscholar",
            "note": f"export.arxiv.org 無法連線({e})，已改用 Semantic Scholar(含 arXiv 收錄)",
            "results": [norm_s2(p) for p in data.get("data") or []],
        }, ensure_ascii=False, indent=1))


ENDNOTE_TYPE = {"journal-article": ("Journal Article", "17"),
                "proceedings-article": ("Conference Paper", "47"),
                "book": ("Book", "6"), "book-chapter": ("Book Section", "5"),
                "posted-content": ("Electronic Article", "43"),
                "report": ("Report", "27")}


def cmd_export_xml(args):
    """EndNote XML 匯出：可把查核結果(entry 的 research_notes 欄)與品質紅旗
    一起帶進 EndNote 的 Research Notes。輸入為本工具任何存檔 JSON(或 pick 輸出)。"""
    if args.file.strip().startswith("["):
        try:
            entries = json.loads(args.file)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": "INVALID_INPUT",
                              "message": f"inline JSON 不合法：{e}"}, ensure_ascii=False))
            sys.exit(1)
    else:
        try:
            entries = json.load(open(args.file, encoding="utf-8"))
            if isinstance(entries, dict):
                _, entries = _load_entries(args.file)
        except (OSError, json.JSONDecodeError):
            _, entries = _load_entries(args.file)
    bad = [i for i in (args.indices or []) if not 0 <= i < len(entries)]
    if bad:
        print(json.dumps({"error": "INVALID_INPUT", "message": f"index 越界：{bad}",
                          "available_range": f"0..{len(entries) - 1}"}, ensure_ascii=False))
        sys.exit(1)
    if args.indices:
        entries = [entries[i] for i in args.indices]

    entries = [e for e in entries if isinstance(e, dict)]   # 濾掉 null/字串等雜項
    if not entries:
        print(json.dumps({"error": "INVALID_INPUT",
                          "message": "沒有可匯出的文獻物件——拒絕產生空的 XML(靜默產空檔會讓引用悄悄消失)"},
                         ensure_ascii=False))
        sys.exit(1)

    root = ET.Element("xml")
    records = ET.SubElement(root, "records")

    def styled(parent, tag, text):
        el = ET.SubElement(parent, tag)
        st = ET.SubElement(el, "style")
        st.set("face", "normal"); st.set("font", "default"); st.set("size", "100%")
        # 移除 XML 1.0 不允許的控制字元，否則產出「號稱 XML 但無法解析」的檔案
        st.text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text))

    for e in entries:
        rec = ET.SubElement(records, "record")
        etype = e.get("type") or ("posted-content" if e.get("arxiv") and not e.get("doi") else "journal-article")
        name, num = ENDNOTE_TYPE.get(etype, ("Generic", "13"))
        rt = ET.SubElement(rec, "ref-type"); rt.set("name", name); rt.text = num
        auths = ET.SubElement(ET.SubElement(rec, "contributors"), "authors")
        for a in e.get("authors") or []:
            if a:
                styled(auths, "author", a)
        titles = ET.SubElement(rec, "titles")
        styled(titles, "title", e.get("title") or "")
        venue = e.get("venue") or e.get("container")
        if venue:
            styled(titles, "secondary-title", venue)
        for src, tag in (("volume", "volume"), ("issue", "number"), ("page", "pages")):
            if e.get(src):
                styled(rec, tag, e[src])
        dates = ET.SubElement(rec, "dates")
        if e.get("year"):
            styled(dates, "year", e["year"])
        if e.get("doi"):
            styled(rec, "electronic-resource-num", e["doi"])
        if e.get("abstract"):
            styled(rec, "abstract", e["abstract"])
        notes = e.get("research_notes") or ""
        if e.get("quality_warnings"):
            notes = (notes + "\n" if notes else "") + "品質紅旗：" + "; ".join(e["quality_warnings"])
        if e.get("arxiv"):
            notes = (notes + "\n" if notes else "") + f"arXiv:{e['arxiv']}"
        if notes:
            styled(rec, "research-notes", notes)
        if e.get("doi") or e.get("arxiv"):
            url = f"https://doi.org/{e['doi']}" if e.get("doi") else f"https://arxiv.org/abs/{e['arxiv']}"
            styled(ET.SubElement(ET.SubElement(rec, "urls"), "related-urls"), "url", url)

    print('<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode"))


def _clean_doi(d):
    """「https://doi.org/10.x/y」「doi:10.x/y」都正規化成「10.x/y」。arXiv 的 doi 欄位與 S2 的
    externalIds 偶爾帶網址前綴；不清掉的話，「是不是 arXiv 自己的 DOI」這個判斷會被繞過。"""
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", "", (d or "").strip(), flags=re.I)


def _is_preprint(c):
    """arXiv 自己的 DataCite DOI（10.48550），以及 Crossref 標成 posted-content 的紀錄
    （SSRN、Research Square、bioRxiv、medRxiv 等預印本伺服器）都不是正式發表。把它們當成
    「正式版」建議改引，等於叫使用者把一份預印本換成另一份預印本——很多期刊兩份都不收。"""
    return (_clean_doi(c.get("doi")).lower().startswith("10.48550")
            or (c.get("type") or "") == "posted-content")


def _fetch_work(doi, out):
    """取回一個 DOI 的 Crossref 書目。回（記錄，狀態）：狀態是 "ok"、"not_found"（404：Crossref
    沒有這個 DOI，DataCite 的 Zenodo／OSF／PsyArXiv 都會這樣）或 "error"（逾時、5xx，已記進
    crossref_error）。三種要分開：查無不是失敗，失敗不是查無。"""
    try:
        cr = json.loads(http_get(CROSSREF_BASE + "/works/" + urllib.parse.quote(doi, safe="/"),
                                 crossref_headers(), 1.0, "crossref"))
        return norm_crossref(cr.get("message", {})), "ok"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "not_found"
        out["crossref_error"] = str(e)
        return None, "error"
    except OSError as e:
        out["crossref_error"] = str(e)
        return None, "error"


def _fits_preprint(cand, title, pre_authors):
    """候選跟預印本是不是同一研究：作者至少一半對得上、題目相近。只有一位作者時題目門檻
    拉高——比對只靠一個姓氏，常見姓氏下同一人的不同論文很多。回 (是否符合, 作者重疊, 題目相似)。"""
    ts = title_sim(title, cand.get("title")) if title else 0.0
    ov = author_overlap(pre_authors, cand.get("authors"))
    named = [a for a in (pre_authors or []) if _name_key(a)]
    floor = 0.3 if len(named) > 1 else 0.5
    return (ov is not None and ov >= 0.5 and ts >= floor), ov, ts


def _resolve_declared_doi(out, jdoi, title, pre_authors, possible):
    """作者在 arXiv 自填的期刊 DOI。它等於作者說「後來發表在這裡」，但這個欄位實務上也常填成
    伴隨論文、資料集、整本論文集的 DOI。所以：取回 Crossref 書目，作者與題目對得上才當成
    正式版；對不上只列為「可能」；Crossref 查無此 DOI 就記下來，不推薦改引。"""
    cand, status = _fetch_work(jdoi, out)
    if status == "not_found":
        out["arxiv_record"]["journal_doi_status"] = (
            "Crossref 查無此 DOI（可能打錯，或不是 Crossref 註冊的 DOI）：未當成正式版")
        return
    if status == "error":
        out["arxiv_record"]["journal_doi_status"] = "Crossref 查詢失敗，未能核對：見 crossref_error"
        return
    fits, ov, ts = _fits_preprint(cand, title, pre_authors)
    if _is_preprint(cand):
        out["arxiv_record"]["journal_doi_status"] = "作者自填的 DOI 指向另一份預印本，不是正式發表"
    elif fits:
        out["published_version"] = cand
        out["version_link"] = "arxiv_journal_ref"
    else:
        possible.append(dict(cand, basis="arxiv_declared_doi_mismatch",
                             author_overlap=ov, title_sim=ts,
                             note="作者在 arXiv 自填的 DOI，但作者或題目與預印本對不上："
                                  "可能是相關論文、資料集或整本論文集"))


def _describe(p):
    """「已有正式發表版(…)」括號裡的那段：欄位缺的時候寫「未知」，不要印 None。"""
    return "%s, %s, DOI %s" % (p.get("container") or p.get("type") or "來源未知",
                                p.get("year") or "年份未知", p.get("doi"))


def cmd_versions(args):
    """preprint ↔ 正式版解析：回報同一研究的版本鏈與引用建議。

    找正式版的訊號由強到弱：① S2 版本合併（其記錄帶非 arXiv 的 DOI，且 Crossref 查得到、不是
    預印本）；② 作者在 arXiv 自填的期刊 DOI（取回 Crossref 書目、作者與題目對得上才算）；
    ③ Crossref 標題搜尋（標題近乎相同，年份不早於預印本，取最像的一筆）；④ Crossref 作者搜尋
    （第一與第二作者各查一次、依 DOI 合併；改了標題才發表的版本，只列為「可能」，不自動改引）。
    arXiv 自己的 DOI 與預印本伺服器（posted-content）在每一層都不算正式版。
    """
    ident = args.id.strip()
    is_arxiv = ident.upper().startswith("ARXIV:")
    out = {"query": ident, "arxiv": None, "s2_record": None, "published_version": None}
    title = None
    pre_authors, pre_year = [], None   # 預印本的作者與年份：用來找改了標題才正式發表的版本
    # 存在性與訊號來源都用明確的旗標記，不從欄位有沒有值去推：DOI 路徑的 s2_record 是輸入 DOI
    # 自己的紀錄，拿它的 DOI 當「S2 合併訊號」會把純靠標題推定的版本說成有合併佐證。
    confirmed = False                  # 有任何來源回了這個識別碼的記錄
    via_s2_merge = False               # 正式版是 S2 版本合併給的
    s2_not_found = arxiv_not_found = False
    possible = []
    if is_arxiv:
        # 前導空白與版本後綴（2410.01234v2）都不是編號的一部分：帶著送出去，S2 會回 404，
        # 接著就被說成「編號不存在，請確認是否打錯」——其實是我們自己沒清乾淨。
        out["arxiv"] = re.sub(r"v\d+$", "", ident[6:].strip(), flags=re.I)
        try:
            sp = s2_get_paper("ARXIV:" + out["arxiv"])
            confirmed = True
            title = sp.get("title")
            pre_authors, pre_year = sp.get("authors") or [], sp.get("year")
            out["s2_record"] = {k: sp.get(k) for k in ("title", "year", "venue", "doi")}
            pub_doi = _clean_doi(sp.get("doi"))
            if pub_doi and not pub_doi.lower().startswith("10.48550"):
                # S2 會合併同一研究的版本：其記錄帶非 arXiv DOI 通常就是正式版。但仍要 Crossref
                # 查得到、而且不是預印本伺服器——S2 也會把 SSRN、bioRxiv 合併進來，那不是正式版；
                # Crossref 查無（DataCite 的 Zenodo／OSF 之類）也不能原樣當成「已有正式發表版」。
                cand, status = _fetch_work(pub_doi, out)
                if status == "ok" and _is_preprint(cand):
                    out["s2_note"] = ("S2 合併的 DOI %s 指向預印本伺服器（%s），不算正式版；改以標題與作者搜尋"
                                      % (pub_doi, cand.get("type")))
                elif status == "ok":
                    out["published_version"] = cand
                    via_s2_merge = True
                elif status == "not_found":
                    out["s2_note"] = ("S2 合併的 DOI %s 在 Crossref 查無（可能是 DataCite 的 DOI），"
                                      "未當成正式版；改以標題與作者搜尋" % pub_doi)
                else:
                    # 覆核失敗（逾時、5xx）：S2 的訊號仍在，採用但明說未經覆核、信心降為 medium
                    out["published_version"] = {"doi": pub_doi, "source": "semanticscholar（未經 Crossref 覆核）"}
                    via_s2_merge = True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                s2_not_found = True
                # S2 查無此 arXiv 記錄（新文獻的索引延遲很常見）。這是「沒有記錄」不是
                # 「查詢失敗」：以前一併記成 s2_error，下游就把它說成「請稍後重試」並回非零；
                # 版本鏈仍可由下面的 arXiv 標題 + Crossref 標題搜尋推定（DOI 分支早就這樣分）。
                out["s2_note"] = ("Semantic Scholar 查無此 arXiv 記錄（HTTP 404，非查詢失敗）；"
                                  "版本鏈改由 arXiv 標題經 Crossref 推定，無 S2 版本合併訊號佐證")
            else:
                out["s2_error"] = str(e)
        except OSError as e:
            out["s2_error"] = str(e)
        # 還沒有正式版就一律查一次 arXiv。以前只在 S2 失敗時才查，於是 S2 有記錄、只是還沒合併
        # 出正式版——最常見的情況——作者在 arXiv 自填的期刊 DOI 永遠讀不到。
        if not out["published_version"]:
            entries = None
            try:
                entries = arxiv_query(None, 1, id_list=out["arxiv"])
            except OSError as e:
                out["arxiv_error"] = str(e)
            if entries:
                confirmed = True
                e0 = entries[0]
                out["arxiv_record"] = {"title": e0.get("title"), "year": e0.get("year")}
                # 標題、作者、年份各自回填：S2 有題目沒作者（很常見）時，作者要拿 arXiv 的，
                # 否則作者搜尋整個跳過、自填 DOI 也因為「對不上作者」永遠只能降成「可能」。
                title = title or e0.get("title")
                pre_authors = pre_authors or e0.get("authors") or []
                pre_year = pre_year or e0.get("year")
                jdoi = _clean_doi(e0.get("doi"))
                if jdoi and not jdoi.lower().startswith("10.48550"):
                    out["arxiv_record"]["journal_doi"] = jdoi
                    _resolve_declared_doi(out, jdoi, title, pre_authors, possible)
            elif entries is not None:
                arxiv_not_found = True
    elif looks_like_doi(ident):
        # 只認 DOI: 前綴(不分大小寫)或真正的 10.xxxx/ 格式——先前「含斜線就當 DOI」
        # 會把「Risk/Benefit Analysis」這種正常標題誤送 DOI 端點
        doi = _clean_doi(ident)
        n, status = _fetch_work(doi, out)
        if status == "ok":
            confirmed = True
            title = n.get("title")
            pre_authors, pre_year = n.get("authors") or [], n.get("year")
            out["s2_record"] = n
            if not _is_preprint(n):
                # 輸入的 DOI 本身就是正式發表：它就是正式版，不必再去搜一個同名的別篇來「改引」
                out["published_version"] = n
                out["version_link"] = "input_doi_is_published"
        try:
            sp = s2_get_paper("DOI:" + doi)
            confirmed = True
            out["arxiv"] = sp.get("arxiv")
            title = title or sp.get("title")
            pre_authors = pre_authors or sp.get("authors") or []
            pre_year = pre_year or sp.get("year")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                out["s2_error"] = str(e)
        except OSError as e:
            out["s2_error"] = str(e)
    else:
        title = ident
        if not has_latin(ident):
            print(json.dumps({
                "query": ident,
                "error": "UNSUPPORTED_IDENTIFIER",
                "recommendation": ("輸入無可比對的拉丁字元(中文標題或純符號)，且不是 DOI/arXiv 編號："
                                  "版本解析僅支援拉丁字母標題與識別碼，請改用 DOI 或 arXiv ID。"),
            }, ensure_ascii=False, indent=1))
            sys.exit(1)

    title_searched = False
    if title and not out["published_version"]:
        title_searched = True
        pubs = []
        try:
            for c in crossref_search(title, rows=5):
                if _is_preprint(c):
                    continue  # arXiv 自己的 DOI、SSRN／Research Square 等預印本伺服器都不算正式版
                ts = title_sim(title, c.get("title")) if c.get("title") else 0.0
                if ts < 0.93:
                    continue
                if pre_year and c.get("year") and c["year"] < pre_year - 1:
                    continue  # 比預印本早兩年以上的同名論文不是它的正式版
                pubs.append(dict(c, title_sim=ts, author_overlap=author_overlap(pre_authors, c.get("authors"))))
        except Exception as e:
            out["crossref_error"] = str(e)
        if pubs:
            # 取最像的一筆，不是 Crossref 排第一的那筆；作者對得上的優先
            pubs.sort(key=lambda c: (c["title_sim"], c["author_overlap"] or 0.0), reverse=True)
            out["published_version"] = pubs[0]

    # 正式版常常改標題：預印本「X: A Y Approach」發表時可能變成「Y for X」。上面那段只認
    # 標題相似度 ≥ 0.93，改名後的正式版一定漏掉，接著的結論就是「查無正式版」——而很多期刊
    # 不收 arXiv，照這個結論引了預印本，審稿時才被退。人工查證的作法是用作者找：同一組作者、
    # 年份不早於預印本、題目相近。這裡照做，但只列為「可能的正式版」：同一組作者的不同論文
    # 也長這樣，所以不自動改引、一律要人工核對。
    named = [a for a in pre_authors if _name_key(a)]
    author_searched = False
    if title and named and not out["published_version"]:
        author_searched = True
        try:
            # 發表時第一、第二作者對調很常見（貢獻排序重談）：只查第一作者的姓氏，對調後的正式版
            # 在 Crossref 的作者查詢裡就排不進前十筆。兩位各查一次，依 DOI 合併後再套下面同一組篩選。
            who = [surname(named[0])] + ([surname(named[1])] if len(named) > 1 else [])
            pool, fetch_err = {}, None
            for sn in dict.fromkeys(s for s in who if s):
                try:
                    for c in crossref_search(title, rows=10, author=sn):
                        if c.get("doi"):
                            pool.setdefault(c["doi"].lower(), c)
                except Exception as e:      # 一位查失敗不丟掉另一位已經拿到的結果，但最後要如實回報
                    fetch_err = fetch_err or e
            for c in pool.values():
                if not c.get("doi") or _is_preprint(c):
                    continue                          # 沒 DOI，或本身就是預印本
                if pre_year and (not c.get("year") or c["year"] < pre_year - 1):
                    continue                          # 比預印本早兩年以上：不會是它的正式版
                fits, ov, ts = _fits_preprint(c, title, named)
                if not fits:
                    continue
                item = dict(c, basis="author_search", author_overlap=ov, title_sim=ts)
                if pre_year and c["year"] < pre_year:
                    # 期刊先發表、之後才上傳 arXiv 的情形不少：放寬一年，但標出來
                    item["year_before_preprint"] = True
                possible.append(item)
            if fetch_err:
                raise fetch_err
        except Exception as e:
            out.setdefault("crossref_error", str(e))
    # 整體排序再截斷：自填 DOI 對不上的那筆不該只因為先進來就佔第一格
    possible.sort(key=lambda c: (c.get("author_overlap") or 0.0, c.get("title_sim") or 0.0), reverse=True)
    seen, merged = set(), []
    for c in possible:
        if c.get("doi") and c["doi"] not in seen:
            seen.add(c["doi"])
            merged.append(c)
    if merged:
        out["possible_published_versions"] = merged[:3]

    errors = [k for k in ("crossref_error", "s2_error", "arxiv_error") if out.get(k)]
    if out["published_version"]:
        p = out["published_version"]
        out["recommendation"] = "已有正式發表版(%s)，學術慣例應引正式版" % _describe(p)
        if out.get("version_link") == "input_doi_is_published":
            out["recommendation"] = "輸入的 DOI 本身就是正式發表版(%s)，可直接引用" % _describe(p)
        elif out.get("version_link") == "arxiv_journal_ref":
            out["confidence_note"] = ("版本連結來自 arXiv 上作者自填的期刊 DOI，且作者與題目對得上——"
                                      "作者自己標明的正式版，不是由標題推定")
        elif via_s2_merge and p.get("source"):
            out["confidence"] = "medium"
            out["confidence_note"] = "S2 版本合併訊號給的 DOI，但 Crossref 覆核失敗：書目未經核對，引用前請自行確認"
        elif not via_s2_merge:
            out["confidence"] = "medium"
            out["confidence_note"] = ("版本鏈由標題相似度推定(未經 S2 版本合併訊號佐證):"
                                      "同名標題的不同論文可能被誤配，請核對作者與年份後再改引")
        if errors and out.get("version_link") != "input_doi_is_published":
            # 較強的訊號沒查到（arXiv 掛了、自填 DOI 覆核失敗）而較弱的訊號找到了：結果可用，
            # 但信心不能高於 medium，而且要說有一條路沒走完——即使信心本來就是 medium 也要說
            out["confidence"] = "medium"
            out["confidence_note"] = ((out.get("confidence_note") or "") +
                                      "；另有來源查詢失敗（%s），更強的版本訊號未能核對" % "、".join(errors)).lstrip("；")
    elif errors:
        out["recommendation"] = ("版本解析未完成——來源查詢失敗(見 *_error 欄位),"
                                 "這不代表沒有正式發表版；請稍後重試")
        if out.get("possible_published_versions"):
            out["recommendation"] += "；已找到的「可能的正式版」見 possible_published_versions"
        print(json.dumps(out, ensure_ascii=False, indent=1))
        sys.exit(1)
    elif out.get("possible_published_versions"):
        k = len(out["possible_published_versions"])
        out["recommendation"] = ("標題相似度沒有找到正式版，但找到 %d 筆「可能」是正式版的紀錄"
                                 "（依據見各筆的 basis：作者自填 DOI 或作者搜尋）：請逐筆核對作者、摘要與方法，"
                                 "確認是同一研究就改引正式版——很多期刊不接受引用 arXiv 預印本" % k)
        out["confidence"] = "low"
    elif confirmed and not title:
        # 來源證實這篇存在，但沒拿到標題，什麼都沒搜。沒搜就不能寫「搜尋查無」。
        out["recommendation"] = ("來源有這筆記錄但沒有標題可供比對，未做任何版本搜尋："
                                 "請改用 DOI 或自行提供完整標題再查")
        print(json.dumps(out, ensure_ascii=False, indent=1))
        sys.exit(1)
    elif confirmed and out["arxiv"]:
        # 以前這裡說「目前引 arXiv 版合理」。但很多期刊不收 arXiv，而「查無正式版」只是
        # 這次搜尋的結果：改了標題的正式版、不註冊 DOI 的會議論文都可能沒搜到。
        how = "標題與作者搜尋都" if author_searched else "標題搜尋"
        out["recommendation"] = ("以%s查無正式發表版（查無 ≠ 不存在：會議論文集常不註冊 DOI，"
                                 "改了標題的正式版也可能沒搜到）。注意很多期刊不接受引用 arXiv 預印本——"
                                 "引用前請再確認一次，例如在 Google Scholar 看這篇的「所有版本」" % how)
        if (out.get("arxiv_record") or {}).get("journal_doi_status"):
            out["recommendation"] += "；arXiv 上作者自填的期刊 DOI 未能採用，原因見 arxiv_record.journal_doi_status"
    elif is_arxiv and s2_not_found and arxiv_not_found:
        # S2 與 arXiv 都回「查無」(查詢失敗會走上面的 *_error 分支)：沒有任何來源證實這個編號
        # 存在。以前會落到上一支、說「目前引 arXiv 版合理」，等於替一個可能打錯的編號背書。
        # 無法判定就不回 0——與 UNSUPPORTED_IDENTIFIER 同一條原則。
        out["error"] = "ARXIV_ID_NOT_FOUND"
        out["recommendation"] = ("Semantic Scholar 與 arXiv API 皆查無此編號（兩者都回查無，不是查詢失敗）："
                                 "沒有任何來源證實這篇存在，請先確認編號是否打錯；確認之前不要引用")
        print(json.dumps(out, ensure_ascii=False, indent=1))
        sys.exit(1)
    elif title_searched:
        # 標題輸入（或 DOI 輸入、Crossref 查無但 S2 給了標題）：搜過了、沒找到。不能把問題推給使用者的輸入格式。
        out["recommendation"] = ("以標題搜尋查無正式發表版（查無 ≠ 不存在）；若這是預印本，"
                                 "注意很多期刊不接受引用預印本，請改以 DOI 或 arXiv 編號再查一次")
    else:
        out["recommendation"] = "無法解析版本鏈，請確認輸入為 DOI:x / ARXIV:x / 完整標題"
    print(json.dumps(out, ensure_ascii=False, indent=1))


def cmd_fulltext(args):
    """查合法可取得的全文位置（Unpaywall + S2 的 OA 欄位；目前沒有 OpenAlex 路徑）。

    刻意**不**嘗試繞過付費牆：付費牆內的文獻請走機構訂閱(Google Scholar Button
    的「大學圖書館找全文」或你所在單位的 link resolver)，那是正當授權路徑。
    """
    mailto = os.environ.get("CROSSREF_MAILTO")
    out = []
    for doi in args.dois:
        doi = re.sub(r"^doi:", "", doi.strip(), flags=re.I)
        rec = {"doi": doi, "oa_locations": []}
        if not mailto:
            rec["note"] = ("Unpaywall 要求提供聯絡 email：請在 .env 設 CROSSREF_MAILTO "
                           "(同一個值兩邊共用)，否則只能用 S2 的 OA 欄位")
        else:
            try:
                u = (f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi, safe='/')}"
                     f"?email={urllib.parse.quote(mailto)}")
                d = json.loads(http_get(u, min_interval=0.2, bucket="unpaywall"))
                rec["is_oa"] = d.get("is_oa")
                rec["oa_status"] = d.get("oa_status")   # gold/green/hybrid/bronze/closed
                for loc in d.get("oa_locations") or []:
                    rec["oa_locations"].append({
                        "url": loc.get("url_for_pdf") or loc.get("url"),
                        "host_type": loc.get("host_type"),      # publisher / repository
                        "version": loc.get("version"),          # publishedVersion 等
                        "license": loc.get("license"),
                    })
            except urllib.error.HTTPError as e:
                rec["error"] = ("Unpaywall 查無此 DOI" if e.code == 404
                                else f"Unpaywall 查詢失敗 HTTP {e.code}")
            except OSError as e:
                rec["error"] = f"Unpaywall 查詢失敗：{e}"
        # 補 S2 的 OA 欄位（Unpaywall 沒收錄時仍可能有；目前沒有 OpenAlex 路徑）
        if not rec["oa_locations"]:
            try:
                sp = s2_get_paper("DOI:" + doi)
                if sp.get("openAccessPdf"):
                    rec["oa_locations"].append({"url": sp["openAccessPdf"],
                                                "host_type": "semanticscholar_oa_field",
                                                "version": None, "license": None})
            except (urllib.error.HTTPError, OSError) as e:
                # 這裡以前是 pass。備援查詢失敗被吞掉之後,下面的 else 會印出
                # 「🔒 未找到合法免費版」並教使用者去跑機構圖書館——一個根據從未
                # 完成的查詢做出的結論。查不到和沒查到,不是同一件事。
                rec.setdefault("error", "S2 OA 欄位備援查詢失敗：%s" % e)
        if rec["oa_locations"]:
            rec["status"] = "✅ 有合法免費全文"
            # Unpaywall 判 closed 但備援找到 OA 版：兩者並列會看起來矛盾，明說來源差異
            if rec.get("is_oa") is False and all(
                    l["host_type"] == "semanticscholar_oa_field" for l in rec["oa_locations"]):
                rec["source_discrepancy"] = (
                    "Unpaywall 回報 closed，但 Semantic Scholar 的 OA 欄位找到免費版"
                    "(常見於機構典藏的 green OA 未被 Unpaywall 收錄)。連結可用，"
                    "但版本可能是作者稿而非出版社排版版，引用頁碼請以出版社版為準")
        elif rec.get("error"):
            rec["status"] = "❓ 查詢失敗(不代表沒有免費版)"
        else:
            rec["status"] = "🔒 未找到合法免費版"
            rec["how_to_get"] = [
                f"https://doi.org/{doi}(出版社頁面，可能需訂閱)",
                "透過你所在機構的圖書館取得(Google Scholar Button 的"
                "「find full text in your university library」或圖書館 link resolver)",
                "館際合作/文獻傳遞服務",
            ]
        out.append(rec)
    out.append({"_note": "只回報合法可取得的位置；本工具不繞過付費牆。"
                         "「未找到免費版」不代表拿不到全文——機構訂閱是正當且通常更完整的途徑。"})
    print(json.dumps(out, ensure_ascii=False, indent=1))
    checked = [r for r in out if "doi" in r]
    if checked and all(r.get("error") for r in checked):
        sys.exit(1)


def cmd_retract(args):
    """查撤稿/更正記錄：Crossref 的 update notices(含 Retraction Watch 資料)。"""
    out = []
    for doi in args.dois:
        doi = doi.replace("DOI:", "").strip()
        params = {"filter": f"updates:{doi}", "rows": "5"}
        mailto = os.environ.get("CROSSREF_MAILTO")
        if mailto:
            params["mailto"] = mailto
        url = CROSSREF_BASE + "/works?" + urllib.parse.urlencode(params)
        try:
            data = json.loads(http_get(url, crossref_headers(), crossref_interval(), "crossref"))
            notices = []
            for it in data.get("message", {}).get("items", []):
                for u in it.get("update-to", []):
                    if (u.get("DOI") or "").lower() == doi.lower():
                        notices.append({
                            "type": u.get("type"),
                            "label": u.get("label"),
                            "notice_doi": it.get("DOI"),
                            "notice_title": (it.get("title") or [None])[0],
                            "date": (u.get("updated") or {}).get("date-parts", [[None]])[0],
                        })
            # updates 關聯之外，再用標題搜尋補一路：2010 年前的通知（以及某些
            # 出版社的習慣）常把撤稿/關切聲明當成獨立文章發表而不設 updates 關聯。
            # VIGOR/rofecoxib（NEJM 2000）就是這樣：兩份 Expression of Concern
            # 明明在 Crossref，updates 查詢卻一無所獲，回傳誤導性的「乾淨」。
            fallback_err = None
            if not notices:
                try:
                    meta = json.loads(http_get(
                        CROSSREF_BASE + "/works/" + urllib.parse.quote(doi, safe="/"),
                        crossref_headers(), crossref_interval(), "crossref"))
                    orig_title = ((meta.get("message", {}).get("title") or [""])[0] or "")
                    if orig_title:
                        q = {"query.bibliographic": orig_title[:120], "rows": "8"}
                        if mailto:
                            q["mailto"] = mailto
                        cand = json.loads(http_get(
                            CROSSREF_BASE + "/works?" + urllib.parse.urlencode(q),
                            crossref_headers(), crossref_interval(), "crossref"))
                        for it in cand.get("message", {}).get("items", []):
                            t = ((it.get("title") or [""])[0] or "")
                            tl = t.lower()
                            if not re.match(r"^\s*(retraction|expression of concern|"
                                            r"withdrawn|editorial expression)", tl):
                                continue
                            # 通知的標題必須確實提到原文（避免撈到別篇的撤稿通知）
                            stripped = re.sub(r"^[^:：]{0,60}[:：]\s*", "", t)
                            if title_sim(stripped, orig_title) < 0.5 and \
                                    norm_title(orig_title)[:40] not in norm_title(t):
                                continue
                            notices.append({
                                "type": ("expression_of_concern" if "concern" in tl
                                         else "retraction"),
                                "label": "由標題搜尋發現（未經 Crossref updates 關聯確認）",
                                "notice_doi": it.get("DOI"), "notice_title": t,
                                "date": ((it.get("issued") or {}).get("date-parts")
                                         or [[None]])[0],
                                "detection": "title_search_fallback",
                            })
                except OSError as e:
                    # 這裡以前是 pass。這段標題搜尋是備援,存在的理由正是 Crossref
                    # 的 updates 關聯對 2010 年前的文獻覆蓋不佳;它失敗而被吞掉之後,
                    # notices 是空的,於是下面印出「✅ 無已知撤稿記錄」——對一篇可能
                    # 已被撤稿的文獻開綠燈。撤稿查詢開錯綠燈的代價特別高。
                    fallback_err = str(e)
            severe = any((n.get("type") or "").lower() in
                         ("retraction", "retraction_notice", "removal", "withdrawal",
                          "partial_retraction", "expression_of_concern") for n in notices)
            fallback_only = notices and all(n.get("detection") == "title_search_fallback"
                                            for n in notices)
            if severe:
                status = ("⚠️ 疑似有撤稿/關切聲明（由標題搜尋發現，未經 updates 關聯確認）"
                          "——請開啟 notices 中的通知確認是否指向本文"
                          if fallback_only else "🚨 有撤稿/疑慮記錄，不可引用，詳見 notices")
            elif notices:
                status = "⚠️ 有更正記錄(correction/erratum)，引用前確認更正內容"
            elif fallback_err:
                # 備援查詢沒跑完就不能發綠勾:notices 是空的,但那是「沒查到」
                # 還是「沒查完」,這裡分得出來,就必須說出來。
                status = "❓ 撤稿查詢不完整（updates 無記錄，標題搜尋備援失敗）"
            else:
                # 2010 年前的通知關聯覆蓋不佳，綠勾在此年代沒有證據力
                yr = None
                m = re.search(r"\b(19|20)\d{2}\b", doi)
                status = "✅ Crossref 無已知撤稿/更正記錄"
            rec = {"doi": doi, "status": status, "notices": notices}
            if fallback_err:
                rec["error"] = fallback_err
            if not notices:
                rec["coverage_caveat"] = (
                    "Crossref 的撤稿關聯對 2010 年前的文獻覆蓋不佳（部分出版社把撤稿/"
                    "關切聲明當成獨立文章發表而未設關聯）。舊文獻的「無記錄」證據力弱，"
                    "藥物安全等高風險引用建議另以 Retraction Watch 網站複核。")
            out.append(rec)
        except OSError as e:
            out.append({"doi": doi, "status": "❓ 查詢失敗", "error": str(e)})
    out.append({"_note": "Crossref 撤稿覆蓋不完備(尤其非英文期刊)：無記錄 ≠ 保證沒事；"
                         "生醫/心理領域高風險引用建議再以 Retraction Watch 網站人工複核"})
    print(json.dumps(out, ensure_ascii=False, indent=1))
    # 全部查詢失敗時給非零 exit，批次腳本才能靠 exit code 偵測「這輪等於沒查」
    checked = [r for r in out if "doi" in r]
    if checked and all(r["status"].startswith("❓") for r in checked):
        sys.exit(1)


def _load_entries(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(json.dumps({"error": "INVALID_INPUT", "file": path,
                          "message": f"檔案不是合法 JSON:{e}"}, ensure_ascii=False))
        sys.exit(1)
    if isinstance(d, list):          # pick 的輸出本身就是陣列
        return {}, d
    if not isinstance(d, dict):
        print(json.dumps({"error": "INVALID_INPUT", "file": path,
                          "message": "JSON 根層必須是物件或陣列"}, ensure_ascii=False))
        sys.exit(1)
    for key in ("results", "citations", "references", "candidates"):
        if isinstance(d.get(key), list):
            return d, d[key]
    # 單篇 paper/crossref-doi 的輸出是單一物件：視為一筆，否則會靜默產生空結果
    if d.get("title") or d.get("doi"):
        return d, [d]
    print(json.dumps({"error": "INVALID_INPUT", "file": path,
                      "message": "找不到可用的文獻清單(results/citations/references/candidates)"
                                 "，也不是單篇文獻物件"}, ensure_ascii=False))
    sys.exit(1)


def cmd_brief(args):
    """省 token 瀏覽：一行一筆，無摘要。先 brief 挑人，再 pick 精讀。"""
    d, entries = _load_entries(args.file)
    meta = {k: d[k] for k in ("fallback", "note", "verdict_hint") if d.get(k)}
    if meta:
        print(json.dumps(meta, ensure_ascii=False))
    for i, r in enumerate(entries):
        flags = ("A" if r.get("abstract") else "-") + ("P" if r.get("openAccessPdf") else "-") \
                + ("!" if r.get("quality_warnings") else "")
        print(f"[{i}] {r.get('year')} c={r.get('citationCount')} {flags} | "
              f"{(r.get('title') or '')[:80]} | {(r.get('venue') or '')[:40]} | doi={r.get('doi')}")


def cmd_pick(args):
    """讀取 brief 選中的那幾筆完整資料(含摘要)。"""
    d, entries = _load_entries(args.file)
    bad = [i for i in args.indices if not 0 <= i < len(entries)]
    if bad:
        print(json.dumps({"error": "INVALID_INPUT", "message": f"index 越界：{bad}",
                          "available_range": f"0..{len(entries) - 1}"}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps([entries[i] for i in args.indices], ensure_ascii=False, indent=1))


def cmd_batch(args):
    """一次抓多篇詳情(官方建議的高效做法，最多 500 筆)。"""
    url = S2_BASE + "/paper/batch?" + urllib.parse.urlencode({"fields": S2_FIELDS})
    body = json.dumps({"ids": args.ids}).encode("utf-8")
    headers = {**s2_headers(), "Content-Type": "application/json"}
    data = json.loads(http_get(url, headers, s2_interval(), "s2", data=body))
    out = []
    for pid, p in zip(args.ids, data):
        out.append(add_quality(norm_s2(p)) if p else {"error": "not_found", "id": pid})
    print(json.dumps({"results": out}, ensure_ascii=False, indent=1))


# ---------- snowball(引文滾雪球) ----------

def openalex_snowball(doi, direction, limit):
    w = json.loads(http_get(OPENALEX_BASE + "/works/doi:" + urllib.parse.quote(doi, safe="/"),
                            min_interval=0.2, bucket="openalex"))
    if direction == "citations":
        wid = (w.get("id") or "").rsplit("/", 1)[-1]
        params = {"filter": f"cites:{wid}", "per-page": str(limit), "sort": "cited_by_count:desc"}
    else:  # references:work 物件直接帶引用清單，批次撈詳情
        refs = [r.rsplit("/", 1)[-1] for r in (w.get("referenced_works") or [])][:min(limit, 50)]
        if not refs:
            return []
        params = {"filter": "openalex:" + "|".join(refs), "per-page": str(len(refs))}
    data = json.loads(http_get(OPENALEX_BASE + "/works?" + urllib.parse.urlencode(params),
                               min_interval=0.2, bucket="openalex"))
    return [add_quality(norm_openalex(x)) for x in data.get("results") or []]


def cmd_snowball(args):
    dirs = ["citations", "references"] if args.direction == "both" else [args.direction]
    out = {"id": args.id}
    for d in dirs:
        items = None
        # citations 方向：S2 端點按時間序，抓不到高被引的引用者；OpenAlex 可伺服器端
        # 按被引數排序，故有 DOI 時優先走 OpenAlex，失敗再退回 S2
        if d == "citations" and args.id.upper().startswith("DOI:"):
            try:
                items = openalex_snowball(args.id[4:], d, args.limit)
                out["citations_source"] = "openalex(按被引數排序)"
            except OSError:
                items = None
        if items is not None:
            out[d] = items
            continue
        try:
            # S2 按時間序回傳；多抓一頁再按被引數排序取前 N，否則只會拿到最新的
            fetch = min(100, max(args.limit * 5, 30))
            url = (S2_BASE + "/paper/" + urllib.parse.quote(args.id, safe=":/") + f"/{d}?"
                   + urllib.parse.urlencode({"fields": S2_FIELDS, "limit": str(fetch)}))
            data = json.loads(http_get(url, s2_headers(), s2_interval(), "s2"))
            key = "citingPaper" if d == "citations" else "citedPaper"
            items = [add_quality(norm_s2(x[key])) for x in data.get("data") or [] if x.get(key)]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                out[d] = {"error": "not_found", "hint": "確認 id 格式，如 DOI:10.x/y"}
                continue
            if e.code == 429 and args.id.upper().startswith("DOI:"):
                items = openalex_snowball(args.id[4:], d, args.limit)
                out["fallback"] = "openalex"
            else:
                raise
        items.sort(key=lambda p: p.get("citationCount") or 0, reverse=True)
        out[d] = items[:args.limit]
    print(json.dumps(out, ensure_ascii=False, indent=1))


# ---------- verify ----------

CJK = r"぀-ヿ㐀-䶿一-鿿豈-﫿"


def looks_like_doi(ident):
    """這串輸入是不是 DOI。

    抽成模組層函式,是為了讓迴歸測試打得到**這一個**判定。以前測試在自己檔案裡
    複製了一份等價的 lambda 然後測那份 lambda——那種測試對生產程式碼的任何改動
    都不會變紅,它證明的只是它自己抄對了。
    """
    # 先把「doi:」與「https://doi.org/」前綴清掉再認：使用者貼的常常就是網址，以前這種輸入
    # 不被認成 DOI，整串網址被當成標題去搜，最後落到「請確認輸入為 DOI:x」——把問題推給使用者。
    return bool(re.match(r"^10\.\d{4,9}/\S+", _clean_doi(ident)))


def norm_title(t):
    # 1) 保留**所有語系**的字母與數字,不只拉丁與 CJK。理由與當初保留 CJK 的理由
    #    完全相同,只是當初只想到中文:刪掉某個語系的字母,會讓兩篇不同的論文塌縮
    #    成同一個字串。實際踩到的是希臘字母——「Role of TNF-α in sepsis」與
    #    「Role of TNF-β in sepsis」都會變成「role of tnf in sepsis」,title_sim
    #    回 1.0,判定 found:一篇不存在的論文被認證為存在。西里爾、韓文、阿拉伯文
    #    同理。用 isalnum() 而不是列舉區間,因為列舉區間就是這個 bug 的成因。
    # 2) 以空格取代其餘符號(不是刪除)：刪除會讓「Need:A」黏成「needa」扭曲相似度。
    kept = [ch if ch.isalnum() else " " for ch in (t or "").lower()]
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def has_latin(t):
    """標題是否含可供英文索引比對的拉丁**字母**。

    以前這裡是 [a-z0-9]:一個純希臘文或純韓文的標題只要帶個年份數字就會被判定
    「可以用英文索引查」,然後拿一串查不到東西的關鍵字去查,回來的空結果被讀成
    查無。數字不是可索引的內容。
    """
    return bool(re.search(r"[a-z]", (t or "").lower()))


def title_sim(a, b):
    na, nb = norm_title(a), norm_title(b)
    # 正規化後為空(純符號/emoji)：空對空的 ratio 是 1.0，會讓任意標題「完全匹配」
    # 任意其他標題。查不到不得偽裝成查到 → 回 0.0。
    if not na or not nb:
        return 0.0
    char_ratio = SequenceMatcher(None, na, nb).ratio()
    # 字元層的比對會把「一整個詞不一樣」稀釋成「一個字元不一樣」:
    # 「role of tnf α in sepsis」對「role of tnf β in sepsis」只差 1/24 個字元,
    # char_ratio = 0.957,越過 0.93 的 found 門檻——兩篇不同的論文互相認證。
    # 詞層的比對看得見那是一整個詞:6 個詞差 1 個 → 0.833,落回 similar_found,
    # 使用者會被要求人工比對,而不是拿到一個綠燈。取兩者較小值:標題是由詞組成的,
    # 任一層看出差異就是差異。
    token_ratio = SequenceMatcher(None, na.split(), nb.split()).ratio()
    return round(min(char_ratio, token_ratio), 3)


_ALWAYS_SUFFIXES = {"jr", "sr"}
_ROMAN_SUFFIXES = {"ii", "iii", "iv"}


def _name_key(name):
    """作者名字 → (姓氏詞組, 名字詞組, 姓氏是否確定)；沒有鑑別力時回 None。

    「Family, Given」（有逗號）的姓氏是確定的：逗號前整段。「Given Family」（沒有逗號）分不出
    哪幾個詞是姓——「María García López」的姓是「García López」，不是「López」——所以只回傳
    整串詞、標成不確定，由比對端決定怎麼用。以前不分青紅皂白把兩種寫法都砍成最後一個詞，
    「García López」與「Pérez López」（不同人）就成了同一個姓。
    """
    name = (name or "").strip()
    if not name:
        return None
    known = "," in name
    fam_s, given_s = name.split(",", 1) if known else (name, "")
    fam = [t for t in re.findall(r"[^\s,.]+", fam_s.lower()) if t not in _ALWAYS_SUFFIXES]
    given = [t for t in re.findall(r"[^\s,.]+", given_s.lower()) if t not in _ALWAYS_SUFFIXES]
    # 羅馬數字後綴（II、III、IV）只有在去掉之後還剩兩個詞以上才當後綴：日本姓「Ii」（伊井）
    # 是真的姓，「Naosuke Ii」去掉它就只剩名字了。
    for part in (fam, given):
        if part and part[-1] in _ROMAN_SUFFIXES and len(fam) + len(given) - 1 >= 2:
            part.pop()
    if not fam:
        return None
    if len(fam) == 1 and len(fam[0]) <= 1:
        return None          # 只剩一個字母（「J.」）沒有鑑別力：拿它比對只會讓不同人互相認證
    return fam, given, known


def surname(name):
    """作者姓氏的**查詢詞**（送進 Crossref query.author 用）：姓氏詞組的最後一個詞。
    比對兩個名字是不是同一個姓，請用 same_surname，不要拿這個值互相比。"""
    k = _name_key(name)
    return k[0][-1] if k else ""


def same_surname(a, b):
    """兩個作者名字的姓氏是否相同。

    兩邊都有逗號：姓氏詞組要完全相同——「García López」≠「Pérez López」。
    一邊有、一邊沒有：有逗號那邊的姓氏詞組要是另一邊的結尾——「van der Linden, Daan」對
    「Daan van der Linden」、「García López, María」對「María García López」都成立，
    「García López, María」對「Juan Pérez López」不成立。
    兩邊都沒有逗號：分不出姓的範圍，只能比最後一個詞（與以前相同）。
    """
    ka, kb = _name_key(a), _name_key(b)
    if ka is None or kb is None:
        return False
    (fa, _ga, known_a), (fb, _gb, known_b) = ka, kb
    if known_a and known_b:
        return fa == fb
    if known_a != known_b:
        short, full = (fa, fb) if known_a else (fb, fa)
        return len(short) <= len(full) and full[-len(short):] == short
    return fa[-1] == fb[-1]


def same_person(a, b):
    """同姓之外，名字首字母也對得上（兩邊都有名字時才比）。用在「這是不是同一個人」的判斷，
    例如書評防護：第一作者與使用者的作者同姓但名字不同，就是另一個人。"""
    if not same_surname(a, b):
        return False
    ka, kb = _name_key(a), _name_key(b)
    ga = ka[1] if ka[2] else ka[0][:-1]
    gb = kb[1] if kb[2] else kb[0][:-1]
    if ga and gb:
        return ga[0][0] == gb[0][0]
    return True


def author_overlap(query_authors, cand_authors):
    """姓氏比對：query 作者中有幾成，在候選作者裡找得到同姓的人（見 same_surname）。

    query 這一側沒有任何可比對的名字（空白、只剩首字母）就回 None——不知道，不是零。
    """
    if not query_authors or not cand_authors:
        return None
    qs = [a for a in query_authors if _name_key(a)]
    if not qs:
        return None
    cs = [c for c in cand_authors if _name_key(c)]
    hit = sum(1 for q in qs if any(same_surname(q, c) for c in cs))
    return round(hit / len(qs), 2)


def score_candidate(cand, title, authors, year):
    s = {"title_sim": title_sim(title, cand.get("title"))}
    if year and cand.get("year"):
        # 年份可能是「2020a」(APA 同年多篇的字母後綴)、「2020-05」或一段文字。
        # 讓 int() 直接爆掉的後果不是報錯,是更糟:這個函式跑在 fetch_crossref／
        # fetch_s2 裡面,而那一層用 except Exception 把任何例外收成「這個來源查詢
        # 失敗」——於是一個純粹的字串格式問題,會被回報成整個資料庫查不到。
        try:
            s["year_diff"] = abs(int(re.sub(r"\D.*$", "", str(year)) or 0)
                                 - int(re.sub(r"\D.*$", "", str(cand["year"])) or 0))
        except (TypeError, ValueError):
            s["year_diff"] = None          # 比不出來就說比不出來,不要假裝相符
    ov = author_overlap(authors, cand.get("authors"))
    if ov is not None:
        s["author_overlap"] = ov
    return s


def rank_candidates(candidates):
    """依 (title_sim, author_overlap, 年份接近度) 多鍵排序。

    平手時若只看 title_sim，真論文可能排在無關候選之後，best_candidate 選錯
    會誤導下游把引用「修正」成錯的文獻。與 decide_verdict 一樣是純函式，
    可在不打 API 的情況下測試(見 evals/test_regression.py)。
    """
    return sorted(candidates,
                  key=lambda c: (c["match"]["title_sim"],
                                 c["match"].get("author_overlap") or 0.0,
                                 -(c["match"].get("year_diff")
                                   if c["match"].get("year_diff") is not None else 99)),
                  reverse=True)


def derivative_signals(c, query_authors=None):
    """偵測「同標題衍生條目」：書評、讀者投書、社論、勘誤等。

    這是跨領域測試中最嚴重的假陽性來源：期刊的 correspondence／book review 會
    原字照抄原著標題，Crossref 一律標成 journal-article，於是使用者一筆正確的
    專書或原始論文引用，會被「訂正」成書評的期刊名與卷期頁。判別所需的欄位
    Crossref 本來就有回傳（type / page / container / authors），先前完全沒用。

    回傳理由字串清單（空清單代表沒有可疑訊號）。
    """
    out = []
    page = (c.get("page") or "").strip()
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", page)
    span = None
    if m:
        lo, hi = m.group(1), m.group(2)
        # 期刊常把結束頁縮寫:「2224-30」意思是 2224–2230,不是 2224 到 30。
        # 直接相減會得到 -2193,而下面的判斷是 span <= 3——負數一律成立,於是一篇
        # 橫跨七頁的正常論文被標成「僅 -2193 頁,書評的典型長度」。
        if len(hi) < len(lo):
            hi = lo[:len(lo) - len(hi)] + hi
        span = int(hi) - int(lo) + 1
        if span <= 0:                      # 仍然算不出合理跨距就不猜
            span = None
    elif page.isdigit():
        span = 1
    if span is not None and span <= 3:
        out.append(f"候選僅 {span} 頁（{page}）——書評、讀者投書、社論等衍生條目的典型長度")
    t = (c.get("type") or "").lower()
    if t in ("book-review", "peer-review", "component"):
        out.append(f"Crossref 類型為 {t}")
    # 書評的作者欄常同時含書評人與原著作者：使用者給的作者有中，但候選第一位不是他
    if query_authors and (c.get("authors") or []):
        first = (c["authors"][0] or "")
        named = [a for a in query_authors if _name_key(a)]
        # 「第一作者是不是使用者的作者」問的是同一個人：同姓之外名字首字母也要對得上。
        # 以前先用子字串包含（'li' 在 'elizabeth wallis' 裡，防護自己關掉），後來改成詞集合
        # 交集——但詞集合連名字都算進去，而書評人只要跟原著作者共用姓的最後一個詞，防護照樣
        # 失效，書評就被蓋章成原著。
        if named and not any(same_person(a, first) for a in named) and author_overlap(list(query_authors), c["authors"]) == 1.0:
            out.append(f"使用者提供的作者出現在候選中，但第一作者是他人（{first}）"
                       "——書評／評論條目的典型樣態")
    return out


def same_work(a, b):
    """兩筆候選是否為同一篇論文(只是來自不同資料庫)。

    verify 會同時收 Crossref 與 S2 的結果,同一篇常各出現一次。若不先辨識,
    歧義檢查會把「兩個來源互相印證」(最強的證據)誤判成「配對有歧義」(疑點)——
    一筆完全正確的引用因此被降級,而且降級理由是錯的。
    """
    def norm_doi(d):
        # APA 舊刊等來源會出現 10.1037//xxx 這種雙斜線寫法，Crossref 兩種形式並存。
        # 只做 .lower() 會把同一篇的兩種寫法當成不同文獻，於是「同一作品的再版」
        # 被誤報成「另有不同文獻造成歧義」——降級理由本身是假的。
        d = re.sub(r"^https?://(dx\.)?doi\.org/", "", (d or "").strip().lower())
        return re.sub(r"/{2,}", "/", d)

    da, db = norm_doi(a.get("doi")), norm_doi(b.get("doi"))
    if da and db:
        return da == db
    # 無 DOI 時退而求其次:標題幾乎相同且年份相符
    return title_sim(a.get("title"), b.get("title")) >= 0.95 and a.get("year") == b.get("year")


def decide_verdict(candidates, *, year_supplied=False, authors_supplied=False,
                   query_authors=None):
    """雙路徑 identity gate：回 (verdict, identity_basis)。純函式，無 I/O。

    路徑一(標題主導)title_sim ≥ .93；路徑二(作者主導)title_sim ≥ .80 且
    author_overlap ≥ .80——後者用來救回「標題被複製貼上弄壞、但作者對得上」
    的真引用。任一路徑通過後仍須過年份、作者零重疊、歧義三道降級檢查。
    """
    if not candidates:
        return "not_found", None
    m = candidates[0]["match"]
    ts = m["title_sim"]
    ov = m.get("author_overlap")      # None = 使用者未給作者，不可據以否決
    yd = m.get("year_diff")           # None = 任一方無年份，「未知」不等於 0
    title_path = ts >= 0.93
    author_path = ts >= 0.80 and ov is not None and ov >= 0.80
    basis = {"match_path": "title" if title_path else "author" if author_path else "none",
             "title_similarity": ts, "author_overlap": ov,
             "author_overlap_denominator": "user_supplied", "year_diff": yd}
    reasons = []
    if yd is not None and yd > 1:
        reasons.append(f"年份差 {yd} 年(>1)")
    if ov is not None and ov == 0.0:
        # 同名標題不同作者是最危險的假陽性：標題再像也不得判 found
        reasons.append("使用者提供的作者無一出現在候選作者中")
    elif authors_supplied and ov is None:
        # 使用者給了作者、候選卻沒有作者欄 → 無從比對，不得當成「作者相符」放行。
        # 這在醫學是常態陷阱：期刊的 correspondence／editorial 會原字照抄母論文
        # 標題，Crossref 標為 journal-article 而作者欄常是空的。若不擋，一筆正確的
        # 引用會被「改正」成投書的卷期頁。（與 year_supplied 同一道理，見下一條）
        reasons.append("候選無作者資料，無法用使用者提供的作者佐證")
    if year_supplied and yd is None:
        reasons.append("候選無年份資料，無法用年份佐證")
    deriv = derivative_signals(candidates[0], query_authors)
    if deriv:
        # 不直接否決（真的有極短篇正式論文），但不得以 found 蓋章放行
        reasons.extend(deriv)
        basis["derivative_item_risk"] = deriv
    # 歧義只在「第二名是另一篇論文」時成立；同一篇的不同來源要略過
    rival = next((c for c in candidates[1:] if not same_work(candidates[0], c)), None)
    if rival is not None:
        gap = ts - rival["match"]["title_sim"]
        if gap < 0.03:
            reasons.append(f"另有相似度僅差 {round(gap, 3)} 的不同文獻，配對有歧義")
            basis["ambiguous"] = True
    corroborating = sum(1 for c in candidates[1:] if same_work(candidates[0], c))
    if corroborating:
        basis["cross_source_corroboration"] = corroborating   # 同一篇被幾個來源印證

    if (title_path or author_path) and not reasons:
        basis["identity_confidence"] = "high" if title_path and ov else "medium"
        return "found", basis
    if title_path or author_path or ts >= 0.8:
        basis["identity_confidence"] = "low"
        basis["downgrade_reasons"] = reasons
        return "similar_found", basis      # 標題相近但有疑點，需人工/LLM 判讀
    return "not_found", basis


def crossref_interval():
    # 有 mailto 就進 polite pool，Crossref 對此寬鬆得多（其文件的參考值遠高於 3 rps）；
    # 沒有 mailto 時保守維持 1 rps，不佔用公共池。
    return 0.34 if os.environ.get("CROSSREF_MAILTO") else 1.0


def verify_one(title, authors, year):
    """單筆驗證的純邏輯（不印出、不 exit），供 verify 與 verify-batch 共用。"""
    result = {"query": {"title": title, "authors": authors or None, "year": year},
              "candidates": []}
    if not has_latin(title):
        result["verdict_hint"] = "unsupported_title"
        result["absence_note"] = ("標題正規化後無可比對的拉丁字元（可能為中文/日文/純符號標題）："
                                  "本工具的相似度比對僅支援拉丁字母標題，無法判定。"
                                  "中文文獻請用 Google Scholar 或華藝等中文索引人工查核。")
        return result

    def fetch_crossref():
        return [dict(c, match=score_candidate(c, title, authors, year))
                for c in crossref_search(title, rows=4)]

    def fetch_s2():
        url = S2_BASE + "/paper/search/match?" + urllib.parse.urlencode(
            {"query": title, "fields": S2_FIELDS})
        data = json.loads(http_get(url, s2_headers(), s2_interval(), "s2"))
        cands = []
        for p in data.get("data") or []:
            c = norm_s2(p)
            cands.append(dict(c, match=score_candidate(c, title, authors, year)))
        return cands

    out = {}

    def run(name, fn):
        try:
            out[name] = fn()
        except urllib.error.HTTPError as e:
            if name == "s2" and e.code == 404:
                out[name] = []            # S2 match 端點查無時回 404，那是查無不是失敗
            else:
                out[name + "_error"] = str(e)
        except Exception as e:
            out[name + "_error"] = str(e)

    threads = [(n, threading.Thread(target=run, args=(n, f)))
               for n, f in (("crossref", fetch_crossref), ("s2", fetch_s2))]
    for _, t in threads:
        t.start()
    # 等的時間必須大於 http_get 自己的最壞情況,否則健康但慢的來源會被無故丟掉。
    # 用共用的截止時間而不是逐條各給一個預算:後者會讓總等待變成條數乘以預算
    # (兩條就是 344 秒),而使用者等的是「全部查完」,不是「每一條各自的耐心」。
    join_budget = HTTP_WORST_CASE + 10
    deadline = time.monotonic() + join_budget
    for _, t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))

    # join(timeout=…) 回來**不代表**執行緒跑完了。逾時的那一條既沒寫入結果、也
    # 沒寫入 *_error,out 裡什麼都沒有。而下面每一段都用「有沒有 *_error」判斷這
    # 次查詢完不完整,所以一片沉默會被讀成「這個來源查過了,沒有東西」:
    # single_source_degraded 不會標、identity_confidence 照樣 high、而 partial_failure
    # ——那條專門為「查詢未完成 ≠ 查無」寫的路徑——永遠走不到。
    # 沒回答和回答沒有,是這支工具最不能混淆的兩件事。
    for name, t in threads:
        if name not in out and (name + "_error") not in out:
            out[name + "_error"] = (
                "查詢逾時：%d 秒內未回應（執行緒可能仍在執行，本次不採計其結果）"
                % join_budget)

    for name in ("crossref", "s2"):
        result["candidates"].extend(out.get(name) or [])
        if out.get(name + "_error"):
            result[name + "_error"] = out[name + "_error"]

    result["candidates"] = rank_candidates(result["candidates"])
    verdict, basis = decide_verdict(result["candidates"], year_supplied=bool(year),
                                    authors_supplied=bool(authors),
                                    query_authors=authors)
    if basis:
        result["identity_basis"] = basis
    provider_errors = [k for k in ("crossref_error", "s2_error") if result.get(k)]
    if provider_errors and result["candidates"]:
        # 有候選但某個來源掛了 → 判定實際上退化成單源。這與雙源互相印證的
        # 證據強度不同，使用者必須看得出差別，不能只把錯誤埋在 *_error 欄位。
        result["single_source_degraded"] = (
            f"{', '.join(provider_errors)} 查詢失敗，本次判定僅根據單一來源；"
            "證據強度低於雙源交叉，建議稍後重跑確認")
        if basis:
            if basis.get("identity_confidence") == "high":
                basis["identity_confidence"] = "medium"
            basis["sources_degraded"] = provider_errors
    if verdict == "not_found" and provider_errors:
        verdict = "partial_failure"
        result["absence_note"] = ("查詢未完整完成（" + ", ".join(provider_errors)
                                  + " 見上），不可解讀為查無；請稍後重試或人工查證")
    elif verdict == "not_found":
        result["absence_note"] = ("實際查核來源（Crossref + Semantic Scholar）皆無合理候選；"
                                  "書籍章節、非英語文獻、產業論文集常不在這兩個索引中，查無 ≠ 不存在")
    result["verdict_hint"] = verdict
    return result


def cmd_verify_batch(args):
    """一次驗證多筆引用，跨引用併行。

    整份參考文獻列表逐筆開行程是最慢的用法：每筆都付一次 Python 啟動成本，
    而且兩個服務的等待完全不重疊。這裡在單一行程內用工作執行緒併行，
    速率限制仍由 _reserve_slot 依服務各自守住（不會因為併行就超速）。
    """
    try:
        items = json.load(open(args.file, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": "INVALID_INPUT", "message": str(e)}, ensure_ascii=False))
        sys.exit(1)
    if isinstance(items, dict):
        items = items.get("refs") or items.get("references") or items.get("items") or []
    if not isinstance(items, list) or not items:
        print(json.dumps({"error": "INVALID_INPUT",
                          "message": "需要一個陣列，每筆含 title（可選 authors/year/n）"},
                         ensure_ascii=False))
        sys.exit(1)

    results = [None] * len(items)
    lock = threading.Lock()
    idx = [0]

    def worker():
        while True:
            with lock:
                if idx[0] >= len(items):
                    return
                i = idx[0]
                idx[0] += 1
            # `items[i] or {}` 擋得住 None,擋不住字串——而使用者手寫的清單很容易
            # 是 ["Some Title", {...}] 這種混形狀。字串沒有 .get,worker 執行緒會
            # 當場死掉,results[i] 留在 None,最後那一筆從輸出裡整個消失:一份
            # 「全部查完」的報告少了一列,而沒有任何地方說少了。
            if not isinstance(items[i], dict):
                results[i] = {"verdict_hint": "invalid_entry",
                              "error": "清單第 %d 筆不是物件（而是 %s），未查詢"
                                       % (i + 1, type(items[i]).__name__),
                              "raw": items[i]}
                continue
            it = items[i]
            au = it.get("authors")
            if isinstance(au, str):
                au = [a.strip() for a in au.split(";") if a.strip()]
            r = verify_one(it.get("title") or "", au or [], it.get("year"))
            if it.get("n") is not None:
                r["n"] = it["n"]
            results[i] = r

    n_workers = max(1, min(args.workers, len(items)))
    ts = [threading.Thread(target=worker) for _ in range(n_workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    summary = {}
    for r in results:
        summary[r["verdict_hint"]] = summary.get(r["verdict_hint"], 0) + 1
    print(json.dumps({"count": len(results), "summary": summary, "results": results},
                     ensure_ascii=False, indent=1))
    if any(r["verdict_hint"] == "partial_failure" for r in results):
        sys.exit(1)


def cmd_verify(args):
    if not (args.title or "").strip():
        print(json.dumps({"error": "INVALID_INPUT", "message": "--title 不可為空"},
                         ensure_ascii=False))
        sys.exit(1)
    authors = [a.strip() for a in (args.authors or "").split(";") if a.strip()]
    result = verify_one(args.title, authors, args.year)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    # 查詢未完成或標題無法比對，必須讓自動化偵測得到：exit 0 只代表「查完且有結論」
    if result["verdict_hint"] in ("partial_failure", "unsupported_title"):
        sys.exit(1)


# ---------- export ----------

RIS_TYPE = {"journal-article": "JOUR", "proceedings-article": "CPAPER", "book": "BOOK",
            "book-chapter": "CHAP", "posted-content": "GEN", "report": "RPRT"}


def cmd_export(args):
    fmt = args.format
    if args.doi:
        accept = {"ris": "application/x-research-info-systems",
                  "bibtex": "application/x-bibtex"}[fmt]
        url = "https://doi.org/" + urllib.parse.quote(args.doi, safe="/")
        headers = {"Accept": accept, "User-Agent": "lit-review-skill/1.0"}
        try:
            print(html.unescape(http_get(url, headers, 1.0, "doi").strip()))
        except urllib.error.HTTPError as e:
            print(json.dumps({"error": f"HTTP {e.code}", "doi": args.doi}, ensure_ascii=False))
            sys.exit(1)
        return

    # arXiv：官方無 RIS/BibTeX 端點，從 metadata 組；arXiv API 掛掉時改抓 S2 的 metadata
    p = None
    try:
        entries = arxiv_query(None, 1, id_list=args.arxiv)
        p = entries[0] if entries else None
    except OSError:
        pass
    if p is None:
        try:
            sp = s2_get_paper("ARXIV:" + args.arxiv)
            p = {"title": sp["title"], "year": sp["year"], "authors": sp["authors"],
                 "arxiv": args.arxiv, "doi": sp.get("doi")}
        except urllib.error.HTTPError as e:
            # 只有 404 才是「查無」；429/5xx 是查詢失敗，不得偽裝成文獻不存在
            if e.code == 404:
                print(json.dumps({"error": "not_found", "arxiv": args.arxiv,
                                  "note": "S2 未收錄此 arXiv 編號；arXiv API 亦無回應，"
                                          "請確認編號或稍後重試"}, ensure_ascii=False))
            else:
                print(json.dumps({"error": f"UPSTREAM_UNAVAILABLE (HTTP {e.code})",
                                  "arxiv": args.arxiv,
                                  "note": "查詢失敗，不是查無此文獻；請稍後重試"},
                                 ensure_ascii=False))
            sys.exit(1)

    def ris_safe(v):
        # RIS 以行為單位：欄位值內的換行會截斷記錄，惡意/髒 metadata 可藉此注入假欄位
        return re.sub(r"\s+", " ", str(v or "")).strip()

    p = {k: (ris_safe(v) if isinstance(v, str) else v) for k, v in p.items()}
    p["authors"] = [ris_safe(a) for a in (p.get("authors") or []) if a]
    if fmt == "ris":
        lines = ["TY  - GEN", f"TI  - {p['title']}"]
        lines += [f"AU  - {a}" for a in p["authors"]]
        lines += [f"PY  - {p['year']}", f"UR  - https://arxiv.org/abs/{p['arxiv']}",
                  "PB  - arXiv", f"N1  - arXiv:{p['arxiv']} [preprint]"]
        if p.get("doi"):
            lines.append(f"DO  - {p['doi']}")
        lines.append("ER  - ")
        print("\n".join(lines))
    else:
        first_author = (p["authors"][0].split()[-1] if p["authors"] else "arxiv").lower()
        key = f"{first_author}{p['year']}"
        authors = " and ".join(p["authors"])
        print(f"@misc{{{key},\n  title={{{p['title']}}},\n  author={{{authors}}},\n"
              f"  year={{{p['year']}}},\n  eprint={{{p['arxiv']}}},\n"
              f"  archivePrefix={{arXiv}},\n  url={{https://arxiv.org/abs/{p['arxiv']}}}\n}}")


# ---------- main ----------

def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="Semantic Scholar 關鍵字搜尋")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--year", help="如 2020-2026 或 2020-")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("arxiv", help="arXiv 搜尋")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_arxiv)

    p = sub.add_parser("snowball", help="引文滾雪球：citations=誰引它(追新), references=它引誰(追經典)")
    p.add_argument("id", help="DOI:10.x/y | ARXIV:2301.12345 | S2 paperId")
    p.add_argument("--direction", choices=["citations", "references", "both"], default="both")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_snowball)

    p = sub.add_parser("verify", help="驗證一筆文獻(存在性+書目)")
    p.add_argument("--title", required=True)
    p.add_argument("--authors", help="分號分隔，如 'Smith, J.; Chen, L.'")
    p.add_argument("--year", type=int)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("verify-batch", help="一次驗證多筆引用（JSON 陣列，跨引用併行）")
    p.add_argument("file", help="JSON：[{title, authors?, year?, n?}, ...]")
    p.add_argument("--workers", type=int, default=4, help="併行數（預設 4）")
    p.set_defaults(func=cmd_verify_batch)

    p = sub.add_parser("paper", help="取單篇詳細資料含摘要")
    p.add_argument("id", help="DOI:10.x/y | ARXIV:2301.12345 | S2 paperId")
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("batch", help="一次抓多篇詳情(id 空白分隔，最多 500 筆)")
    p.add_argument("ids", nargs="+", help="DOI:10.x/y ARXIV:... 等，空白分隔")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("export-xml", help="EndNote XML 匯出(research_notes/紅旗進 Research Notes 欄)")
    p.add_argument("file", help="任何本工具存檔 JSON、pick 輸出檔，或直接貼 JSON 陣列")
    p.add_argument("indices", nargs="*", type=int, help="選填：只匯出這幾筆")
    p.set_defaults(func=cmd_export_xml)

    p = sub.add_parser("versions", help="preprint↔正式版解析(引用建議)")
    p.add_argument("id", help="DOI:10.x/y | ARXIV:2301.12345 | 完整標題")
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("fulltext", help="查合法可取得的全文位置(Unpaywall + OA 欄位)")
    p.add_argument("dois", nargs="+", help="一個或多個 DOI")
    p.set_defaults(func=cmd_fulltext)

    p = sub.add_parser("retract", help="查撤稿/更正記錄(Crossref update notices)")
    p.add_argument("dois", nargs="+", help="一個或多個 DOI")
    p.set_defaults(func=cmd_retract)

    p = sub.add_parser("brief", help="省 token 瀏覽已存檔的結果(一行一筆，無摘要)")
    p.add_argument("file")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("pick", help="讀取 brief 選中的那幾筆完整資料")
    p.add_argument("file")
    p.add_argument("indices", nargs="+", type=int)
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser("crossref-doi", help="DOI → Crossref 權威書目")
    p.add_argument("doi")
    p.set_defaults(func=cmd_crossref_doi)

    p = sub.add_parser("export", help="產生 RIS/BibTeX")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--doi")
    g.add_argument("--arxiv")
    p.add_argument("--format", choices=["ris", "bibtex"], default="ris")
    p.set_defaults(func=cmd_export)

    args = ap.parse_args()
    try:
        args.func(args)
    except OSError as e:  # 網路層錯誤統一輸出 JSON，不要 traceback
        out = {"error": str(e)}
        if isinstance(e, urllib.error.HTTPError) and e.code == 429:
            # 429 不一定來自 Semantic Scholar：Crossref、OpenAlex 也會回。以前這裡不看主機、
            # 不看有沒有金鑰，一律叫使用者去申請 S2 金鑰——歸因錯誤的處方比沒有處方更糟。
            # 不能用 getattr(e, "url")：HTTPError 在沒有回應體（fp=None）時，繼承自 tempfile 的
            # __getattr__ 對 .url 丟的是 KeyError 不是 AttributeError，getattr 的預設值擋不住，
            # 整個錯誤處理器會炸掉、exit 變 0。filename 是 __init__ 直接寫進 __dict__ 的，安全。
            try:
                u = e.filename or ""
            except Exception:
                u = ""
            host = urllib.parse.urlsplit(u).netloc or "上游服務"
            # 用「本行程 S2 回過 429」的旗標歸因，不用最後那個例外的主機：search 會在 S2 429 後退到
            # OpenAlex，最後拋出的是 OpenAlex 的 429，但該修的仍是 S2 金鑰。
            if _s2_429_seen and not os.environ.get("S2_API_KEY"):
                out["hint"] = ("Semantic Scholar 共享池壅塞（重試後仍 429）。等 60–120 秒再跑同一指令；"
                               "長期解法是申請免費 S2_API_KEY 放入 .env。" + S2_KEY_HINT)
            elif _s2_429_seen:
                out["hint"] = ("已設 S2_API_KEY 仍遇到 429（重試後仍 429）：這是金鑰本身的速率上限，"
                               "不是缺金鑰；等 60–120 秒再跑同一指令，或降低 --workers")
            else:
                out["hint"] = "%s 回 429（速率限制，重試後仍 429）：等 60–120 秒再跑同一指令" % host
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(1)
    except (ValueError, ET.ParseError) as e:
        # HTTP 200 但回傳 HTML 維護頁/截斷 XML：上游壞掉不得變成 traceback,
        # 更不得被誤解為「查無」——明確標成上游回應無法解析
        print(json.dumps({"error": "UPSTREAM_UNPARSEABLE",
                          "message": f"上游回應無法解析(可能是維護頁或截斷內容):{e}",
                          "note": "這是查詢失敗，不是查無此文獻；請稍後重試"},
                         ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
