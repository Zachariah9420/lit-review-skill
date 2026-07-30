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


def http_get(url, headers=None, min_interval=0.0, bucket="default", retries=3, data=None):
    """帶速率限制與 429/5xx 退避重試的 GET(data 給值時為 POST)。"""
    wait = min_interval - (time.time() - _last_call.get(bucket, 0.0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, data=data,
                                 headers=headers or {"User-Agent": "lit-review-skill/1.0"})
    last_err = None
    for attempt in range(retries + 1):
        try:
            _last_call[bucket] = time.time()
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                raise
            if e.code == 429 and attempt < retries:
                time.sleep((2 ** attempt) * 6)  # S2 無 key 時共享池很擠，退避要夠長
                continue
            if e.code in (500, 502, 503) and attempt < retries:
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

def s2_headers():
    key = os.environ.get("S2_API_KEY")
    return {"x-api-key": key, "User-Agent": "lit-review-skill/1.0"} if key else {"User-Agent": "lit-review-skill/1.0"}


def s2_interval():
    return 1.0 if os.environ.get("S2_API_KEY") else 1.5


def norm_s2(p):
    # 摘要不在取得層截斷：token 控制由 brief/pick 漏斗負責，截斷會切掉
    # Results/Conclusion，對 counter(零結果證據)與因果判讀特別危險
    ext = p.get("externalIds") or {}
    abstract = p.get("abstract")
    return {
        "source": "semanticscholar",
        "paperId": p.get("paperId"),
        "title": p.get("title"),
        "year": p.get("year"),
        "authors": [a.get("name") for a in (p.get("authors") or [])],
        "venue": p.get("venue") or None,
        "doi": ext.get("DOI"),
        "arxiv": ext.get("ArXiv"),
        "citationCount": p.get("citationCount"),
        "openAccessPdf": (p.get("openAccessPdf") or {}).get("url"),
        "abstract": abstract,
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


def _unesc(v):
    """解 HTML entity：Crossref 的期刊名常帶 &amp;（如 Soldering &amp; Surface Mount
    Technology），原樣傳遞會寫進 RIS，使用者匯入 EndNote 後期刊名字面帶 &amp;。"""
    return html.unescape(v) if isinstance(v, str) else v


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


def crossref_search(title, rows=5):
    params = {"query.bibliographic": title, "rows": str(rows)}
    mailto = os.environ.get("CROSSREF_MAILTO")
    if mailto:
        params["mailto"] = mailto
    url = CROSSREF_BASE + "/works?" + urllib.parse.urlencode(params)
    data = json.loads(http_get(url, crossref_headers(), 1.0, "crossref"))
    return [norm_crossref(it) for it in data.get("message", {}).get("items", [])]


def cmd_crossref_doi(args):
    url = CROSSREF_BASE + "/works/" + urllib.parse.quote(args.doi, safe="/")
    try:
        data = json.loads(http_get(url, crossref_headers(), 1.0, "crossref"))
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
        arxiv_id = (e.findtext(ATOM + "id") or "").rsplit("/abs/", 1)[-1]
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


def cmd_versions(args):
    """preprint ↔ 正式版解析：回報同一研究的版本鏈與引用建議。"""
    ident = args.id.strip()
    out = {"query": ident, "arxiv": None, "s2_record": None, "published_version": None}
    title = None
    if ident.upper().startswith("ARXIV:"):
        out["arxiv"] = ident[6:]
        try:
            sp = s2_get_paper("ARXIV:" + out["arxiv"])
            title = sp.get("title")
            out["s2_record"] = {k: sp.get(k) for k in ("title", "year", "venue", "doi")}
            pub_doi = sp.get("doi")
            if pub_doi and not pub_doi.startswith("10.48550"):
                # S2 會合併同一研究的版本：其記錄帶非 arXiv DOI 即正式版，以 Crossref 取權威書目
                try:
                    cr = json.loads(http_get(CROSSREF_BASE + "/works/" + urllib.parse.quote(pub_doi, safe="/"),
                                             crossref_headers(), 1.0, "crossref"))
                    out["published_version"] = norm_crossref(cr.get("message", {}))
                except OSError:
                    out["published_version"] = {"doi": pub_doi, "source": "semanticscholar(未經Crossref覆核)"}
        except OSError as e:
            out["s2_error"] = str(e)
        if title is None:
            try:  # S2 失敗時退 arXiv API 拿標題，供後續 Crossref 標題搜尋
                entries = arxiv_query(None, 1, id_list=out["arxiv"])
                if entries:
                    title = entries[0]["title"]
                    out["arxiv_record"] = {"title": title, "year": entries[0]["year"]}
            except OSError as e:
                out["arxiv_error"] = str(e)
    elif ident.upper().startswith("DOI:") or re.match(r"^10\.\d{4,9}/", ident):
        # 只認 DOI: 前綴(不分大小寫)或真正的 10.xxxx/ 格式——先前「含斜線就當 DOI」
        # 會把「Risk/Benefit Analysis」這種正常標題誤送 DOI 端點
        doi = re.sub(r"^doi:", "", ident, flags=re.I)
        try:
            cr = json.loads(http_get(CROSSREF_BASE + "/works/" + urllib.parse.quote(doi, safe="/"),
                                     crossref_headers(), 1.0, "crossref"))
            n = norm_crossref(cr.get("message", {}))
            title = n.get("title")
            out["s2_record"] = n
        except urllib.error.HTTPError:
            pass
        try:
            sp = s2_get_paper("DOI:" + doi)
            out["arxiv"] = sp.get("arxiv")
            title = title or sp.get("title")
        except urllib.error.HTTPError:
            pass
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

    if title and not out["published_version"]:
        pubs = []
        try:
            for c in crossref_search(title, rows=5):
                if (c.get("doi") or "").startswith("10.48550"):
                    continue  # arXiv 自身的 DataCite DOI 不算正式版
                if c.get("title") and title_sim(title, c.get("title")) >= 0.93:
                    pubs.append(c)
        except Exception as e:
            out["crossref_error"] = str(e)
        if pubs:
            out["published_version"] = pubs[0]

    if out["published_version"]:
        p = out["published_version"]
        out["recommendation"] = (f"已有正式發表版({p.get('container') or p.get('type')}, "
                                 f"{p.get('year')},DOI {p.get('doi')})，學術慣例應引正式版")
        if not out.get("s2_record", {}).get("doi"):
            out["confidence"] = "medium"
            out["confidence_note"] = ("版本鏈由標題相似度推定(未經 S2 版本合併訊號佐證):"
                                      "同名標題的不同論文可能被誤配，請核對作者與年份後再改引")
    elif out.get("crossref_error") or out.get("s2_error") or out.get("arxiv_error"):
        out["recommendation"] = ("版本解析未完成——來源查詢失敗(見 *_error 欄位),"
                                 "這不代表沒有正式發表版；請稍後重試")
        print(json.dumps(out, ensure_ascii=False, indent=1))
        sys.exit(1)
    elif out["arxiv"]:
        out["recommendation"] = ("Crossref 查無正式發表版(不保證不存在，會議論文集常不註冊 DOI);"
                                 "目前引 arXiv 版合理，投稿前可再查一次")
    else:
        out["recommendation"] = "無法解析版本鏈，請確認輸入為 DOI:x / ARXIV:x / 完整標題"
    print(json.dumps(out, ensure_ascii=False, indent=1))


def cmd_fulltext(args):
    """查合法可取得的全文位置(Unpaywall + S2/OpenAlex 的 OA 欄位)。

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
                           "(同一個值兩邊共用)，否則只能用 S2/OpenAlex 的 OA 欄位")
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
        # 補 S2/OpenAlex 的 OA 欄位(Unpaywall 沒收錄時仍可能有)
        if not rec["oa_locations"]:
            try:
                sp = s2_get_paper("DOI:" + doi)
                if sp.get("openAccessPdf"):
                    rec["oa_locations"].append({"url": sp["openAccessPdf"],
                                                "host_type": "semanticscholar_oa_field",
                                                "version": None, "license": None})
            except (urllib.error.HTTPError, OSError):
                pass
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
            data = json.loads(http_get(url, crossref_headers(), 1.0, "crossref"))
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
            severe = any((n.get("type") or "").lower() in
                         ("retraction", "retraction_notice", "removal", "withdrawal",
                          "partial_retraction", "expression_of_concern") for n in notices)
            status = ("🚨 有撤稿/疑慮記錄，不可引用，詳見 notices" if severe
                      else ("⚠️ 有更正記錄(correction/erratum)，引用前確認更正內容" if notices
                            else "✅ Crossref 無已知撤稿/更正記錄"))
            out.append({"doi": doi, "status": status, "notices": notices})
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


def norm_title(t):
    # 1) 保留 CJK 字元：刪掉它們會讓「深度學習：A Study」與「量子物理：A Study」
    #    都塌縮成「a study」而 title_sim=1.0(完全不同的論文被判為同一篇)。
    # 2) 以空格取代其餘符號(不是刪除)：刪除會讓「Need:A」黏成「needa」扭曲相似度。
    return re.sub(r"\s+", " ", re.sub(rf"[^a-z0-9{CJK}]+", " ", (t or "").lower())).strip()


def has_latin(t):
    """標題是否含可供英文索引比對的拉丁字母數字。"""
    return bool(re.search(r"[a-z0-9]", (t or "").lower()))


def title_sim(a, b):
    na, nb = norm_title(a), norm_title(b)
    # 正規化後為空(純符號/emoji)：空對空的 ratio 是 1.0，會讓任意標題「完全匹配」
    # 任意其他標題。查不到不得偽裝成查到 → 回 0.0。
    if not na or not nb:
        return 0.0
    return round(SequenceMatcher(None, na, nb).ratio(), 3)


def author_overlap(query_authors, cand_authors):
    """姓氏比對：query 作者中有幾成出現在候選作者裡。"""
    if not query_authors or not cand_authors:
        return None
    def last_names(names):
        out = set()
        for n in names:
            n = (n or "").strip()
            if not n:
                continue
            ln = n.split(",")[0].strip() if "," in n else n.split()[-1]
            out.add(ln.lower())
        return out
    q, c = last_names(query_authors), last_names(cand_authors)
    if not q:
        return None
    return round(len(q & c) / len(q), 2)


def score_candidate(cand, title, authors, year):
    s = {"title_sim": title_sim(title, cand.get("title"))}
    if year and cand.get("year"):
        s["year_diff"] = abs(int(year) - int(cand["year"]))
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


def same_work(a, b):
    """兩筆候選是否為同一篇論文(只是來自不同資料庫)。

    verify 會同時收 Crossref 與 S2 的結果,同一篇常各出現一次。若不先辨識,
    歧義檢查會把「兩個來源互相印證」(最強的證據)誤判成「配對有歧義」(疑點)——
    一筆完全正確的引用因此被降級,而且降級理由是錯的。
    """
    da, db = (a.get("doi") or "").lower(), (b.get("doi") or "").lower()
    if da and db:
        return da == db
    # 無 DOI 時退而求其次:標題幾乎相同且年份相符
    return title_sim(a.get("title"), b.get("title")) >= 0.95 and a.get("year") == b.get("year")


def decide_verdict(candidates, *, year_supplied=False):
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
    if year_supplied and yd is None:
        reasons.append("候選無年份資料，無法用年份佐證")
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


def cmd_verify(args):
    if not (args.title or "").strip():
        print(json.dumps({"error": "INVALID_INPUT", "message": "--title 不可為空"},
                         ensure_ascii=False))
        sys.exit(1)
    if not has_latin(args.title):
        print(json.dumps({
            "verdict_hint": "unsupported_title",
            "absence_note": ("標題正規化後無可比對的拉丁字元(可能為中文/日文/純符號標題):"
                             "本工具的相似度比對僅支援拉丁字母標題，無法判定。"
                             "中文文獻請用 Google Scholar 或華藝等中文索引人工查核。"),
        }, ensure_ascii=False, indent=1))
        sys.exit(1)
    authors = [a.strip() for a in (args.authors or "").split(";") if a.strip()]
    result = {"query": {"title": args.title, "authors": authors or None, "year": args.year},
              "candidates": []}

    # Crossref 書目搜尋
    try:
        for c in crossref_search(args.title, rows=4):
            c["match"] = score_candidate(c, args.title, authors, args.year)
            result["candidates"].append(c)
    except Exception as e:
        result["crossref_error"] = str(e)

    # Semantic Scholar 標題精確配對(有摘要，供內容查核)
    try:
        url = S2_BASE + "/paper/search/match?" + urllib.parse.urlencode(
            {"query": args.title, "fields": S2_FIELDS})
        data = json.loads(http_get(url, s2_headers(), s2_interval(), "s2"))
        for p in data.get("data") or []:
            c = norm_s2(p)
            c["match"] = score_candidate(c, args.title, authors, args.year)
            result["candidates"].append(c)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            result["s2_error"] = str(e)
    except Exception as e:
        result["s2_error"] = str(e)

    result["candidates"] = rank_candidates(result["candidates"])
    verdict, basis = decide_verdict(result["candidates"], year_supplied=bool(args.year))
    if basis:
        result["identity_basis"] = basis

    # 「查無」是研究結論，只有查詢真的完成才可用；來源掛掉不得偽裝成查無
    provider_errors = [k for k in ("crossref_error", "s2_error") if result.get(k)]
    if verdict == "not_found" and provider_errors:
        verdict = "partial_failure"
        result["absence_note"] = ("查詢未完整完成(" + ", ".join(provider_errors)
                                  + " 見上)，不可解讀為查無；請稍後重試或人工查證")
    elif verdict == "not_found":
        result["absence_note"] = ("實際查核來源(Crossref + Semantic Scholar)皆無合理候選；"
                                  "書籍章節、非英語文獻、產業論文集常不在這兩個索引中，查無 ≠ 不存在")
    result["verdict_hint"] = verdict
    print(json.dumps(result, ensure_ascii=False, indent=1))
    # 查詢未完成必須讓自動化偵測得到：exit 0 只代表「查完且有結論」
    if verdict == "partial_failure":
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
            out["hint"] = ("Semantic Scholar 共享池壅塞(重試後仍 429)。"
                           "等 60–120 秒再跑同一指令即可；長期解法是申請免費 S2_API_KEY 放入 .env。")
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
