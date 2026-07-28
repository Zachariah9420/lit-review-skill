#!/usr/bin/env python3
"""lit_api.py — Semantic Scholar / Crossref / arXiv 統一查詢 CLI(純標準函式庫,Python 3.8+)

子命令:
  search   <query>            Semantic Scholar 關鍵字搜尋(找相關文獻)
  arxiv    <query>            arXiv 搜尋(預印本)
  verify   --title "..."      跨 Crossref + Semantic Scholar 驗證一筆文獻是否存在、書目是否正確
  paper    <id>               取單篇詳細資料含摘要(id 可為 DOI:10.x/y、ARXIV:2301.12345、S2 paperId)
  crossref-doi <doi>          Crossref 權威書目資料(DOI → 完整欄位)
  export   --doi 10.x/y --format ris|bibtex     產生引用檔(doi.org content negotiation)
  export   --arxiv 2301.12345 --format ris|bibtex

環境變數(自動讀取 cwd 的 .env;金鑰只放 .env,不進程式碼):
  S2_API_KEY       選填,Semantic Scholar API key(沒有也能跑,只是速率較低)
  CROSSREF_MAILTO  選填,你的 email(進 Crossref polite pool,速率較寬鬆)

所有輸出皆為 UTF-8 JSON(export 除外,輸出純文字 RIS/BibTeX)。
"""
import argparse
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
    # 先讀 cwd(專案層),再讀 HOME(全域備援);已設定的環境變數不覆蓋。
    # 注意:刻意不讀 skill 目錄自己的 .env——金鑰放在 skill 資料夾會隨打包外流。
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
                time.sleep((2 ** attempt) * 6)  # S2 無 key 時共享池很擠,退避要夠長
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
    ext = p.get("externalIds") or {}
    abstract = p.get("abstract")
    if abstract and len(abstract) > 2000:
        abstract = abstract[:2000] + "…[截斷]"
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
    """OpenAlex 的摘要是倒排索引,還原成文字。"""
    if not inv:
        return None
    pos = {}
    for w, idxs in inv.items():
        for i in idxs:
            pos[i] = w
    text = " ".join(pos[i] for i in sorted(pos))
    return text[:2000] + "…[截斷]" if len(text) > 2000 else text


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
    """為單筆結果附加 quality_warnings(紅旗):供 LLM 判斷是否只能當佐證。"""
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
        warns.append("期刊不在DOAJ也非CORE收錄,品質需人工確認")
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
               "note": "Semantic Scholar 持續 429,已改用 OpenAlex;結果同樣可信,照常使用",
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
        "title": (it.get("title") or [None])[0],
        "year": issued[0][0] if issued and issued[0] else None,
        "authors": [", ".join(filter(None, [a.get("family"), a.get("given")]))
                    for a in it.get("author") or []],
        "container": (it.get("container-title") or [None])[0],
        "doi": it.get("DOI"),
        "type": it.get("type"),
        "volume": it.get("volume"),
        "issue": it.get("issue"),
        "page": it.get("page"),
        "publisher": it.get("publisher"),
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
    # retries=1:export.arxiv.org 掛掉時盡快失敗,讓呼叫端切換到 S2 備援
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
        # export.arxiv.org 不時整台掛掉;S2 也收錄 arXiv,自動退到 S2 搜尋
        plain = re.sub(r'\b(all|ti|au|abs|cat):', "", args.query).replace('"', " ").strip()
        params = {"query": plain, "limit": str(args.limit), "fields": S2_FIELDS}
        data = json.loads(http_get(S2_BASE + "/paper/search?" + urllib.parse.urlencode(params),
                                   s2_headers(), s2_interval(), "s2"))
        print(json.dumps({
            "fallback": "semanticscholar",
            "note": f"export.arxiv.org 無法連線({e}),已改用 Semantic Scholar(含 arXiv 收錄)",
            "results": [norm_s2(p) for p in data.get("data") or []],
        }, ensure_ascii=False, indent=1))


def cmd_batch(args):
    """一次抓多篇詳情(官方建議的高效做法,最多 500 筆)。"""
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
    else:  # references:work 物件直接帶引用清單,批次撈詳情
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
        # citations 方向:S2 端點按時間序,抓不到高被引的引用者;OpenAlex 可伺服器端
        # 按被引數排序,故有 DOI 時優先走 OpenAlex,失敗再退回 S2
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
            # S2 按時間序回傳;多抓一頁再按被引數排序取前 N,否則只會拿到最新的
            fetch = min(100, max(args.limit * 5, 30))
            url = (S2_BASE + "/paper/" + urllib.parse.quote(args.id, safe=":/") + f"/{d}?"
                   + urllib.parse.urlencode({"fields": S2_FIELDS, "limit": str(fetch)}))
            data = json.loads(http_get(url, s2_headers(), s2_interval(), "s2"))
            key = "citingPaper" if d == "citations" else "citedPaper"
            items = [add_quality(norm_s2(x[key])) for x in data.get("data") or [] if x.get(key)]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                out[d] = {"error": "not_found", "hint": "確認 id 格式,如 DOI:10.x/y"}
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

def norm_title(t):
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def title_sim(a, b):
    return round(SequenceMatcher(None, norm_title(a), norm_title(b)).ratio(), 3)


def author_overlap(query_authors, cand_authors):
    """姓氏比對:query 作者中有幾成出現在候選作者裡。"""
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


def cmd_verify(args):
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

    # Semantic Scholar 標題精確配對(有摘要,供內容查核)
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

    result["candidates"].sort(key=lambda c: c["match"]["title_sim"], reverse=True)
    best = result["candidates"][0] if result["candidates"] else None
    if best is None:
        verdict = "not_found"
    elif best["match"]["title_sim"] >= 0.93 and best["match"].get("year_diff", 0) <= 1:
        verdict = "found"
    elif best["match"]["title_sim"] >= 0.8:
        verdict = "similar_found"   # 標題相近但可能有出入,需人工/LLM 判讀
    else:
        verdict = "not_found"
    result["verdict_hint"] = verdict
    print(json.dumps(result, ensure_ascii=False, indent=1))


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
            print(http_get(url, headers, 1.0, "doi").strip())
        except urllib.error.HTTPError as e:
            print(json.dumps({"error": f"HTTP {e.code}", "doi": args.doi}, ensure_ascii=False))
            sys.exit(1)
        return

    # arXiv:官方無 RIS/BibTeX 端點,從 metadata 組;arXiv API 掛掉時改抓 S2 的 metadata
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
        except urllib.error.HTTPError:
            print(json.dumps({"error": "not_found", "arxiv": args.arxiv}, ensure_ascii=False))
            sys.exit(1)
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

    p = sub.add_parser("snowball", help="引文滾雪球:citations=誰引它(追新), references=它引誰(追經典)")
    p.add_argument("id", help="DOI:10.x/y | ARXIV:2301.12345 | S2 paperId")
    p.add_argument("--direction", choices=["citations", "references", "both"], default="both")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_snowball)

    p = sub.add_parser("verify", help="驗證一筆文獻(存在性+書目)")
    p.add_argument("--title", required=True)
    p.add_argument("--authors", help="分號分隔,如 'Smith, J.; Chen, L.'")
    p.add_argument("--year", type=int)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("paper", help="取單篇詳細資料含摘要")
    p.add_argument("id", help="DOI:10.x/y | ARXIV:2301.12345 | S2 paperId")
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("batch", help="一次抓多篇詳情(id 空白分隔,最多 500 筆)")
    p.add_argument("ids", nargs="+", help="DOI:10.x/y ARXIV:... 等,空白分隔")
    p.set_defaults(func=cmd_batch)

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
    except OSError as e:  # 網路層錯誤統一輸出 JSON,不要 traceback
        out = {"error": str(e)}
        if isinstance(e, urllib.error.HTTPError) and e.code == 429:
            out["hint"] = ("Semantic Scholar 共享池壅塞(重試後仍 429)。"
                           "等 60–120 秒再跑同一指令即可;長期解法是申請免費 S2_API_KEY 放入 .env。")
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
