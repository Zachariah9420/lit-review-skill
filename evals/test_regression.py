#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迴歸測試：凍結壓力測試與獨立審查找到的缺陷，確保修正不會被未來的改動弄壞。

執行：python evals/test_regression.py           (全跑)
      python evals/test_regression.py -v        (逐案顯示)
      python evals/test_regression.py -k title  (只跑名稱含 title 的案例)

原則：**不打任何 API**。純函式直接呼叫，CLI 案例只用本地 fixture 檔——所以
可以秒級跑完、可在 CI 跑、不受 429 影響。需要網路的驗證屬 live smoke test,
不在本檔範圍(見 evals/README.md)。

每個案例都對應一個真實找到的缺陷，case id 前綴：
  TS-*  壓力測試(黑箱，6 agents / 38 案例，2026-07-30)
  CX-*  Codex 獨立原始碼審查(24 findings,2026-07-30)
  DR-*  設計文件 review v2 的 Phase 0 項目
  I18N-* 英文輸出模式（LIT_LANG=en）：表的完整性、呼叫端契約、中文不變、英文無 CJK、語言解析、--lang、.env、打包

本套件把 LIT_LANG 釘在 zh（main() 開頭）：殼層、cwd 或 ~/.env 裡的 LIT_LANG=en 不得讓既有案例變紅。
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(BASE)
FIXTURES = os.path.join(BASE, "fixtures")
PY = sys.executable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


la = load_module("lit_api", os.path.join(SKILL, "scripts", "lit_api.py"))

RESULTS = []


def check(case_id, desc, ok, detail=""):
    RESULTS.append({"id": case_id, "desc": desc, "ok": bool(ok), "detail": detail})


def run_cli(script, *args, stdin_json=None, env=None, cwd=None):
    """跑 CLI，回 (exit_code, stdout, stderr)。子行程繼承目前環境（含 main() 釘住的 LIT_LANG=zh）；
    env 的鍵值覆蓋它，值為 None 代表刪掉該變數（I18N-* 用來測「未設」）。"""
    e = dict(os.environ)
    for k, v in (env or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    p = subprocess.run([PY, os.path.join(SKILL, "scripts", script), *[str(a) for a in args]],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=60, env=e, cwd=cwd)
    return p.returncode, p.stdout or "", p.stderr or ""


def fixture(name):
    return os.path.join(FIXTURES, name)


# ─────────────────────────────────────────────────────────────
# 1. 標題正規化與相似度(最危險的假陽性來源)
# ─────────────────────────────────────────────────────────────

def test_title_similarity():
    # CX-01：混語標題塌縮——刪掉 CJK 後只剩共同的英文副標，兩篇無關論文 sim=1.0
    s = la.title_sim("深度學習：A Study", "量子物理：A Study")
    check("CX-01", "混語標題不得因 CJK 被刪而塌縮成相同", s < 0.8, f"title_sim={s}")

    # TS-F11：純中文/emoji 標題正規化後為空，空對空 ratio=1.0 → 任意配任意
    check("TS-F11a", "純符號標題相似度為 0(空對空不是匹配)",
          la.title_sim("🤖🔥", "★☆") == 0.0, f"got {la.title_sim('🤖🔥', '★☆')}")
    s = la.title_sim("智慧型維修決策支援系統之研究", "設備維修管理智能決策支持系統")
    check("TS-F11b", "不同的純中文標題不得判為相同", s < 0.9, f"title_sim={s}")
    check("TS-F11c", "相同的純中文標題仍應為 1.0",
          la.title_sim("智慧型維修決策支援系統", "智慧型維修決策支援系統") == 1.0)

    # TS-C3：刪除符號會讓 "Need:A" 黏成 "needa" 而扭曲相似度 → 應以空格取代
    check("TS-C3", "符號以空格取代而非刪除(避免單字黏合)",
          la.norm_title("Need:A Survey") == "need a survey",
          repr(la.norm_title("Need:A Survey")))

    # 英文正常路徑不得因上述修正而退步
    check("REG-01", "英文標題大小寫/標點差異仍為 1.0",
          la.title_sim("Attention Is All You Need", "attention is all you need!") == 1.0)
    check("REG-02", "完全不同的英文標題分數低",
          la.title_sim("Attention Is All You Need", "Deep Residual Learning") < 0.5)

    # has_latin：判斷「能不能用英文索引比對」的閘門
    check("CX-02", "has_latin 正確區分中文與英文標題",
          not la.has_latin("智慧型維修") and la.has_latin("BERT: Pre-training"))


def test_author_overlap():
    # 分母必須是「使用者提供的作者數」，不是候選作者總數(設計文件 §4.1.2)
    ov = la.author_overlap(["Vaswani"], ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar",
                                         "Jakob Uszkoreit", "Llion Jones", "Aidan Gomez",
                                         "Lukasz Kaiser", "Illia Polosukhin"])
    check("DR-01", "author_overlap 分母為使用者輸入(1/1 而非 1/8)", ov == 1.0, f"got {ov}")
    check("DR-02", "作者完全不重疊時 overlap 為 0",
          la.author_overlap(["Wang"], ["Ashish Vaswani", "Noam Shazeer"]) == 0.0)
    check("DR-03", "未提供作者時回 None(不得據以否決)",
          la.author_overlap([], ["Vaswani"]) is None)


# ─────────────────────────────────────────────────────────────
# 2. verify 的 identity gate(用凍結候選，不打 API)
# ─────────────────────────────────────────────────────────────

def judge(candidates, *, title, authors=(), year=None):
    """用**生產程式碼**的 rank_candidates + decide_verdict 判定，不重新實作。

    先前版本在測試裡自己抄了一份排序/判定邏輯，結果動了生產程式碼測試也不會失敗
    (突變測試抓到這件事)。測試必須呼叫真正會被使用者跑到的那份程式碼。
    """
    scored = []
    for c in candidates:
        c = dict(c)
        c["match"] = la.score_candidate(c, title, list(authors), year)
        scored.append(c)
    ranked = la.rank_candidates(scored)
    verdict, basis = la.decide_verdict(ranked, year_supplied=year is not None)
    return ranked, verdict, basis


def test_identity_gate():
    real = {"title": "Attention Is All You Need", "year": 2017,
            "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"], "doi": "10.x/real"}
    other = {"title": "Is Attention All You Need?", "year": 2025,
             "authors": ["P. Mineault"], "doi": "10.x/other"}

    # TS-C1(壓測證實的已知缺口)：同標題+作者全錯，舊版判 found
    ranked, verdict, basis = judge([real], title="Attention Is All You Need",
                                   authors=["Wang, X.", "Chen, Y."], year=2017)
    check("TS-C1", "同標題但作者無一重疊 → 不得判 found",
          verdict != "found" and ranked[0]["match"]["title_sim"] == 1.0,
          f"verdict={verdict} sim={ranked[0]['match']['title_sim']} "
          f"reasons={(basis or {}).get('downgrade_reasons')}")

    # TS-C2：標題與年份都平手時，必須由 author_overlap 決勝。錯的候選刻意放在
    # 輸入的第一位——若排序鍵少了 author_overlap，穩定排序會讓它留在第一位而失敗。
    wrong = {"title": "Attention Is All You", "year": 2017, "authors": ["X. Unknown"],
             "doi": "10.x/wrong"}
    right = {"title": "Attention Is All You", "year": 2017,
             "authors": ["Ashish Vaswani", "Noam Shazeer"], "doi": "10.x/right"}
    ranked, _, _ = judge([wrong, right], title="Attention Is All You",
                         authors=["Vaswani", "Shazeer"], year=2017)
    check("TS-C2", "標題與年份平手時由 author_overlap 決勝(真論文排第一)",
          ranked[0]["doi"] == "10.x/right",
          f"top={ranked[0]['doi']} ov={ranked[0]['match'].get('author_overlap')}")

    # TS-C2b：標題與作者都平手時，由年份接近度決勝
    old = {"title": "BERT", "year": 2010, "authors": ["J. Devlin"], "doi": "10.x/old"}
    new = {"title": "BERT", "year": 2019, "authors": ["J. Devlin"], "doi": "10.x/new"}
    ranked, _, _ = judge([old, new], title="BERT", authors=["Devlin"], year=2019)
    check("TS-C2b", "標題與作者平手時由年份接近度決勝",
          ranked[0]["doi"] == "10.x/new", f"top={ranked[0]['doi']}")

    # CX-03：候選無年份時，year_diff 缺值不得被當成 0 而蒙混通過年份閘門
    noyear = {"title": "Editorial", "year": None, "authors": ["Bob Jones"]}
    ranked, verdict, basis = judge([noyear], title="Editorial",
                                   authors=["Alice Smith"], year=2024)
    reasons = (basis or {}).get("downgrade_reasons") or []
    check("CX-03", "候選無年份時不得判 found(缺值不等於年份相符)",
          verdict != "found" and ranked[0]["match"].get("year_diff") is None
          and any("年份" in r for r in reasons),
          f"verdict={verdict} year_diff={ranked[0]['match'].get('year_diff')} reasons={reasons}")

    # 作者正確時應可判 found(避免修正過度嚴格導致真引用被擋)
    _, verdict, basis = judge([real], title="Attention Is All You Need",
                              authors=["Vaswani", "Shazeer"], year=2017)
    check("REG-08", "同標題+作者正確 → 判 found(修正未過度嚴格)",
          verdict == "found" and (basis or {}).get("match_path") == "title",
          f"verdict={verdict} basis={basis}")

    # 標題被貼壞但作者對得上 → 走 author path 救回(設計文件雙路徑 gate)
    broken = {"title": "Attention Is All You Need", "year": 2017,
              "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"]}
    _, verdict, basis = judge([broken], title="Attention Is All You",
                             authors=["Vaswani", "Shazeer"], year=2017)
    check("DR-05", "標題壞掉但作者強重疊 → 走 author path，不被判 not_found",
          verdict != "not_found" and (basis or {}).get("match_path") in ("author", "title"),
          f"verdict={verdict} path={(basis or {}).get('match_path')}")


    # NS-01（全新 session 實測發現）：同一篇論文的 Crossref 與 S2 兩筆候選，
    # 是「跨來源互相印證」而非「配對有歧義」。舊版直接比 candidates[0] vs [1]，
    # 讓一筆完全正確的引用被判 similar_found，理由還是錯的。
    cr = {"source": "crossref", "title": "Vapour phase soldering (VPS) technology: a review",
          "year": 2019, "authors": ["Illés, Balázs", "Géczy, Attila"],
          "doi": "10.1108/ssmt-10-2018-0042"}
    s2 = {"source": "semanticscholar", "title": "Vapour phase soldering (VPS) technology: A review",
          "year": 2019, "authors": ["B. Illés", "A. Géczy"],
          "doi": "10.1108/SSMT-10-2018-0042"}   # 同 DOI，僅大小寫不同
    _, verdict, basis = judge([cr, s2], title="Vapour phase soldering (VPS) technology: A review",
                              authors=["Illés", "Géczy"], year=2019)
    check("NS-01", "同一篇的跨來源候選不得被判為歧義（應判 found）",
          verdict == "found" and not (basis or {}).get("ambiguous"),
          f"verdict={verdict} ambiguous={(basis or {}).get('ambiguous')} "
          f"reasons={(basis or {}).get('downgrade_reasons')}")
    check("NS-02", "跨來源印證要記錄在 identity_basis",
          (basis or {}).get("cross_source_corroboration") == 1,
          f"corroboration={(basis or {}).get('cross_source_corroboration')}")

    # NS-03：真正不同的兩篇論文(同名但不同 DOI，如不同期刊的 Editorial)分數
    # 接近時，歧義判定仍須生效——修跨來源印證不得把這道防線一起關掉
    p1 = {"title": "Editorial", "year": 2020, "authors": ["A. One"], "doi": "10.1/a"}
    p2 = {"title": "Editorial", "year": 2020, "authors": ["B. Two"], "doi": "10.2/b"}
    _, verdict, basis = judge([p1, p2], title="Editorial",
                              authors=["One"], year=2020)
    check("NS-03", "不同文獻分數接近時歧義判定仍生效",
          (basis or {}).get("ambiguous") is True,
          f"ambiguous={(basis or {}).get('ambiguous')} verdict={verdict}")

    # 完全不相關的候選仍須 not_found
    _, verdict, _ = judge([{"title": "Deep Residual Learning", "year": 2016,
                            "authors": ["K. He"]}],
                          title="Attention Is All You Need", authors=["Vaswani"], year=2017)
    check("REG-09", "無關候選判 not_found", verdict == "not_found", f"verdict={verdict}")

    # 年份容忍度：±1 可，±3 不可
    y1 = {"title": "BERT", "year": 2019, "authors": ["J. Devlin"]}
    check("REG-03", "線上先行年差 1 年視為可接受",
          la.score_candidate(y1, "BERT", ["Devlin"], 2018)["year_diff"] == 1)
    check("REG-04", "年差 3 年應被記錄為 3(供降級判定)",
          la.score_candidate(y1, "BERT", ["Devlin"], 2016)["year_diff"] == 3)


# ─────────────────────────────────────────────────────────────
# 3. 崩潰路徑：任何輸入都必須是結構化錯誤，不得 traceback
# ─────────────────────────────────────────────────────────────

def assert_no_traceback(case_id, desc, script, *args):
    code, out, err = run_cli(script, *args)
    has_tb = "Traceback (most recent call last)" in err
    parsed = None
    try:
        parsed = json.loads(out) if out.strip().startswith("{") else None
    except json.JSONDecodeError:
        pass
    ok = not has_tb and code != 0 and (parsed is not None or out.strip())
    check(case_id, desc, ok,
          f"exit={code} traceback={has_tb} stdout={(out or err)[:90]!r}")


def test_crash_paths():
    # TS-F08 / CX-04：非 JSON 檔
    assert_no_traceback("TS-F08", "brief 遇非 JSON 檔給結構化錯誤",
                        "lit_api.py", "brief", fixture("not_json.txt"))
    # CX-05:JSON 根層是陣列(pick 的輸出)——舊版 d.get() 崩潰
    code, out, _ = run_cli("lit_api.py", "brief", fixture("list_root.json"))
    check("CX-05", "brief 接受陣列根層的 JSON(pick 輸出)不崩潰",
          code == 0 and "Traceback" not in out, f"exit={code}")
    # CX-06：單篇 paper 輸出餵給 export-xml，舊版靜默產出空 XML
    code, out, _ = run_cli("lit_api.py", "export-xml", fixture("single_work.json"))
    check("CX-06", "單篇文獻物件可匯出(不靜默產空 XML)",
          code == 0 and "<record>" in out, f"exit={code} out={out[:70]!r}")
    # CX-07:inline JSON 不合法
    assert_no_traceback("CX-07", "export-xml 對不合法 inline JSON 給結構化錯誤",
                        "lit_api.py", "export-xml", "[")
    # CX-08：陣列內含 null
    assert_no_traceback("CX-08", "export-xml 對 [null] 不崩潰且拒絕產空檔",
                        "lit_api.py", "export-xml", "[null]")
    # TS-F06：越界 index 必須回報，不得靜默回 []
    assert_no_traceback("TS-F06", "pick 越界 index 回報錯誤而非靜默空集",
                        "lit_api.py", "pick", fixture("two_works.json"), "99")
    # TS-F01：空標題應在打 API 前擋下
    assert_no_traceback("TS-F01", "verify 空標題在發 API 前擋下",
                        "lit_api.py", "verify", "--title", "   ")
    # TS-T6：假 docx
    assert_no_traceback("TS-T6", "cite_integrity 對假 docx 給結構化錯誤",
                        "cite_integrity.py", fixture("fake.docx"), "--json")
    # CX-09：不存在的 docx(舊版 FileNotFoundError traceback)
    assert_no_traceback("CX-09", "cite_integrity 對不存在的 docx 給結構化錯誤",
                        "cite_integrity.py", fixture("no_such_file.docx"), "--json")


# ─────────────────────────────────────────────────────────────
# 4. 「查不到」與「查詢失敗」的語意分離(本 skill 的核心承諾)
# ─────────────────────────────────────────────────────────────

def test_absence_semantics():
    # DR-04：非拉丁標題不得硬配，要明確標 unsupported 並給指引
    code, out, _ = run_cli("lit_api.py", "verify", "--title", "智慧型維修決策支援系統之研究")
    d = json.loads(out) if out.strip().startswith("{") else {}
    check("DR-04", "純中文標題回 unsupported_title 並附人工查核指引",
          d.get("verdict_hint") == "unsupported_title" and "absence_note" in d and code != 0,
          f"exit={code} verdict={d.get('verdict_hint')}")

    # CX-10:versions 對非識別碼的中文輸入不得自信回報錯誤的正式版
    code, out, _ = run_cli("lit_api.py", "versions", "隨便什麼")
    d = json.loads(out) if out.strip().startswith("{") else {}
    check("CX-10", "versions 對中文非識別碼輸入回 UNSUPPORTED_IDENTIFIER",
          d.get("error") == "UNSUPPORTED_IDENTIFIER" and code != 0,
          f"exit={code} error={d.get('error')}")

    # CX-11:DOI 判定必須要求 10.x/ 形狀，含斜線的正常標題不得被誤送 DOI 端點
    #（純字串檢查，不打 API）。**呼叫的是生產程式碼的 la.looks_like_doi**——
    # 這一條以前在本檔就地定義一個等價的 lambda 然後測那個 lambda，
    # 於是 lit_api.py 那邊怎麼改都不會讓它變紅：一條不可能失敗的測試。
    check("CX-11", "含斜線的標題不被誤判為 DOI",
          not la.looks_like_doi("Risk/Benefit Analysis in Medicine")
          and la.looks_like_doi("10.1234/abc")
          and la.looks_like_doi("doi:10.1234/abc"))


# ─────────────────────────────────────────────────────────────
# 5. cite_integrity 的解析正確性
# ─────────────────────────────────────────────────────────────

def integrity_json(name, *extra):
    code, out, err = run_cli("cite_integrity.py", fixture(name), "--json", *extra)
    try:
        return code, json.loads(out)
    except json.JSONDecodeError:
        return code, {"_raw": out, "_err": err[:200]}


def test_integrity_parsing():
    # TS-T2:APA 文件不得顯示「全部通過」綠勾
    code, d = integrity_json("apa_style.md")
    check("TS-T2", "APA 作者-年份文件明示不支援(不給綠勾)",
          d.get("numeric_citations_found") is False and "unsupported_style_note" in d
          and code != 0, f"exit={code} keys={list(d)[:6]}")

    # CX-12：大範圍引用不得靜默丟棄
    code, d = integrity_json("big_range.md")
    check("CX-12", "[1-52] 這類大範圍引用正確展開(不靜默丟棄)",
          d.get("cited_count") == 52, f"cited_count={d.get('cited_count')}")

    # CX-13：參考文獻標題須自成一行，不得被條目內的 References 一詞騙走切分點
    code, d = integrity_json("heading_in_title.md")
    check("CX-13", "條目標題內的 References 一詞不奪走切分點",
          d.get("listed_count") == 2 and d.get("refs_heading_match") == "strict",
          f"listed={d.get('listed_count')} mode={d.get('refs_heading_match')}")

    # 範圍與混合展開(既有功能不得退步)
    code, d = integrity_json("ranges.md")
    check("REG-05", "[8-10] 與 [1, 3-5] 正確展開", d.get("cited_count") == 7,
          f"cited_count={d.get('cited_count')}")

    # 重複編號
    code, d = integrity_json("dup_refs.md")
    check("REG-06", "列表重複編號被偵測", d.get("duplicate_ref_numbers") == [3],
          f"dupes={d.get('duplicate_ref_numbers')}")

    # 引了沒列
    code, d = integrity_json("overcite.md")
    check("REG-07", "引用超出列表範圍被偵測", 100 in (d.get("cited_not_listed") or []),
          f"cited_not_listed={d.get('cited_not_listed')}")

    # CX-14:docx 字元參照必須解碼(Word 把 en-dash 寫成 &#x2013;)
    code, d = integrity_json("charrefs.docx")
    check("CX-14", "docx 的 &#x2013; 解碼後範圍引用 [8–10] 可辨識",
          d.get("cited_count") == 3 and not d.get("unparsed_citation_tokens"),
          f"cited={d.get('cited_count')} unparsed={d.get('unparsed_citation_tokens')}")


# ─────────────────────────────────────────────────────────────
# 6. 輸出格式的健壯性
# ─────────────────────────────────────────────────────────────






def test_derivative_items():
    """XF-*（跨領域 fresh session 實測發現）：期刊的書評／讀者投書／社論會原字照抄
    原著標題，Crossref 一律標成 journal-article。人文 4 本專書有 2 本、醫學 2 筆
    有 2 筆撞到——使用者一筆正確的引用會被「訂正」成書評的期刊名與卷期頁。"""
    review = {"title": "The Making of the English Working Class", "year": 1963,
              "authors": ["H. McQueen", "E. P. Thompson"], "doi": "10.2307/27507914",
              "container": "Labour History", "type": "journal-article", "page": "75"}
    c = dict(review)
    c["match"] = la.score_candidate(c, review["title"], ["Thompson"], 1963)
    v, b = la.decide_verdict([c], year_supplied=True, authors_supplied=True,
                             query_authors=["Thompson"])
    risk = (b or {}).get("derivative_item_risk") or []
    check("XF-01", "同年出版的書評條目不得判 found（年份閘門攔不到時仍要擋）",
          v != "found" and risk, f"verdict={v} risk={risk}")
    check("XF-02", "單頁與「第一作者非查詢者」兩個訊號都要偵測到",
          any("頁" in r for r in risk) and any("第一作者" in r for r in risk), f"risk={risk}")

    normal = {"title": "Deep Residual Learning", "year": 2016, "doi": "10.1/x",
              "authors": ["Kaiming He", "Xiangyu Zhang"],
              "type": "proceedings-article", "page": "770-778"}
    c2 = dict(normal)
    c2["match"] = la.score_candidate(c2, normal["title"], ["He"], 2016)
    v2, b2 = la.decide_verdict([c2], year_supplied=True, authors_supplied=True,
                               query_authors=["He"])
    check("XF-03", "正常長篇論文不得被誤標為衍生條目",
          v2 == "found" and not (b2 or {}).get("derivative_item_risk"),
          f"verdict={v2} risk={(b2 or {}).get('derivative_item_risk')}")


def test_doi_variants_and_s2_entities():
    """XF-04/05：先前的兩處修正都太窄，各留了一個沒修的兄弟。"""
    check("XF-04", "same_work 認得 APA 舊刊的雙斜線 DOI（同一篇的兩種寫法）",
          la.same_work({"doi": "10.1037//0003-066x.55.1.68"},
                       {"doi": "10.1037/0003-066X.55.1.68"}) is True)
    check("XF-04b", "same_work 認得帶 doi.org 前綴的寫法",
          la.same_work({"doi": "https://doi.org/10.1/A"}, {"doi": "10.1/a"}) is True)
    check("XF-04c", "不同 DOI 仍判為不同作品（修正未過度寬鬆）",
          la.same_work({"doi": "10.1/a"}, {"doi": "10.1/b"}) is False)

    n = la.norm_s2({"title": "A &amp; B", "venue": "Group Processes &amp; Intergroup Relations",
                    "externalIds": {}, "authors": [{"name": "Smith &amp; Co"}],
                    "abstract": "x &amp; y"})
    check("XF-05", "S2 路徑也要解 HTML entity（先前只修 Crossref，是過度宣稱）",
          "&amp;" not in (n["title"] + (n["venue"] or "") + str(n["authors"]) + (n["abstract"] or "")),
          f"title={n['title']!r} venue={n['venue']!r} authors={n['authors']}")


def test_empty_author_candidate():
    """MED-*（醫學 fresh session 實測發現）：候選文獻的作者欄為空時，author_overlap
    回 None，與「使用者沒給作者」無法區分，於是繞過最強的假陽性守門。

    醫學期刊的 correspondence／editorial 會原字照抄母論文標題，Crossref 標為
    journal-article 而作者欄常是空的——一筆正確的引用會被「改正」成投書的卷期頁。
    年份側早有 year_supplied 處理同一種語意塌縮，作者側先前沒有。
    """
    letter = {"title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine",
              "year": 2021, "authors": [], "doi": "10.1056/nejmc2036242"}
    real = {"title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine",
            "year": 2020, "authors": ["Fernando P. Polack", "Stephen J. Thomas"],
            "doi": "10.1056/NEJMoa2034577"}

    c = dict(letter)
    c["match"] = la.score_candidate(c, letter["title"], ["Polack", "Thomas"], 2020)
    v, b = la.decide_verdict([c], year_supplied=True, authors_supplied=True)
    reasons = (b or {}).get("downgrade_reasons") or []
    check("MED-01", "使用者給了作者、候選作者欄為空 → 不得判 found",
          v != "found" and any("作者" in r for r in reasons),
          f"verdict={v} reasons={reasons}")

    c2 = dict(real)
    c2["match"] = la.score_candidate(c2, real["title"], ["Polack", "Thomas"], 2020)
    v2, b2 = la.decide_verdict([c2], year_supplied=True, authors_supplied=True)
    check("MED-02", "作者真的對得上時仍判 found（修正不得誤傷真論文）",
          v2 == "found" and (b2 or {}).get("identity_confidence") == "high",
          f"verdict={v2} conf={(b2 or {}).get('identity_confidence')}")

    v3, _ = la.decide_verdict([c2], year_supplied=True, authors_supplied=False)
    check("MED-03", "使用者未提供作者時不因作者欄缺失而降級",
          v3 == "found", f"verdict={v3}")

    check("MED-04", "verify_one 有把 authors_supplied 傳進 decide_verdict",
          "authors_supplied=bool(authors)" in
          open(os.path.join(SKILL, "scripts", "lit_api.py"), encoding="utf-8").read())


def test_single_source_degradation():
    """MED-05：某個來源掛掉但另一來源仍有候選時，判定實際上退化成單源。
    使用者必須看得出證據強度的差別，不能只把錯誤埋在 *_error 欄位裡。"""
    # 這兩條以前是在 lit_api.py 的**原始碼文字**裡 grep 幾個字串,一行程式都沒有
    # 跑過:把整段邏輯換成 `pass` 而保留那幾個字在註解裡,測試照樣綠。
    # 現在真的驅動失敗路徑——把 Crossref 換成會拋 OSError 的假物件、S2 回一筆
    # 正常候選,然後看輸出。不連網。
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "la_med", os.path.join(SKILL, "scripts", "lit_api.py"))
    la = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(la)

    def boom(*a, **k):
        raise OSError("模擬 Crossref 連線失敗")

    # 這裡以前替換的是 la.s2_match／la.s2_search——但 verify_one 的 fetch_s2 不走那兩個，
    # 它直接呼叫 http_get 打 S2 的 /paper/search/match。所以這兩條測試一直是真的連到
    # Semantic Scholar、拿到真的 Attention Is All You Need 才過的：遇到 429 就隨機變紅，
    # 而上面的註解還寫著「不連網」（2026-10-14 封網重跑時只有這兩條紅，才抓到）。
    # 替換要做在真正的出口 http_get。外洩的網址另外記下來檢查：假物件在供應者執行緒裡
    # 丟的例外會被 verify_one 收成「來源查詢失敗」，光靠丟例外，外洩反而會被藏起來。
    s2_match_payload = json.dumps({"data": [{
        "paperId": "fixture", "title": "Attention Is All You Need", "year": 2017,
        "authors": [{"name": "Ashish Vaswani"}], "venue": "NIPS",
        "externalIds": {"DOI": "10.5555/3295222"}}]})
    leaked = []

    def fake_http_get(url, *a, **k):
        if "api.semanticscholar.org" in url and "/paper/search/match" in url:
            return s2_match_payload
        leaked.append(url)
        raise OSError("測試替身拒絕連網：%s" % url)

    la.crossref_search = boom
    la.http_get = fake_http_get
    r = la.verify_one("Attention Is All You Need",
                      authors=["Vaswani, Ashish"], year=2017)
    basis = r.get("identity_basis") or {}
    check("MED-05", "單源退化時輸出 single_source_degraded 說明（且全程不連網）",
          "證據強度低於雙源交叉" in (r.get("single_source_degraded") or "") and not leaked,
          f"single_source_degraded={r.get('single_source_degraded')!r} leaked={leaked!r}")
    check("MED-06", "單源退化時信心不得留在 high，且標出退化來源",
          basis.get("identity_confidence") != "high" and basis.get("sources_degraded"),
          f"confidence={basis.get('identity_confidence')!r} "
          f"degraded={basis.get('sources_degraded')!r}")


def test_vancouver_and_list_guard():
    """PH-*（藥學 fresh session 實測發現）：醫藥期刊用 Vancouver 列表（1. Author），
    只認 [n] 會讓列表解析成 0 筆，然後把全部引用誤報成「引了沒列」——沉默的錯誤答案。"""
    code, d = integrity_json("vancouver.md")
    check("PH-01", "Vancouver 列表（1. / 1)）可正確解析",
          d.get("listed_count") == 5 and d.get("reference_list_parsed") is True,
          f"listed={d.get('listed_count')} parsed={d.get('reference_list_parsed')}")
    check("PH-02", "Vancouver 格式下仍正確抓出引了沒列／列了沒引",
          d.get("cited_not_listed") == [5, 7] and d.get("listed_not_cited") == [6],
          f"cited_not_listed={d.get('cited_not_listed')} listed_not_cited={d.get('listed_not_cited')}")

    # 列表存在但無文字編號（Word 自動編號）→ 必須改判無法核對，不得輸出假缺漏
    code, d = integrity_json("unnumbered_list.md")
    check("PH-03", "列表無法解析時改判「無法核對」而非報一整份假的引了沒列",
          d.get("reference_list_parsed") is False and d.get("cited_not_listed") == []
          and "unsupported_style_note" in d and code != 0,
          f"parsed={d.get('reference_list_parsed')} cited_not_listed={d.get('cited_not_listed')}")


def test_retraction_fallback():
    """PH-04：2010 年前的撤稿/關切聲明常以獨立文章發表而未設 Crossref updates 關聯
    （VIGOR/rofecoxib 就是），只查 updates 會回傳誤導性的「乾淨」。"""
    src = open(os.path.join(SKILL, "scripts", "lit_api.py"), encoding="utf-8").read()
    fn = src[src.index("def cmd_retract"):src.index("def _load_entries")]
    check("PH-04", "retract 在 updates 無記錄時以標題搜尋補查",
          "title_search_fallback" in fn and "query.bibliographic" in fn)
    # PH-05／06 以前在函式原始碼裡 grep 中文字串。文字搬進 MESSAGES 表之後，改驗兩件事：函式用的是這把鍵，
    # 以及這把鍵在兩種語言裡都說了該說的話。（PH-06 以前只靠註解裡的「2010」就能過——一條空洞的測試。）
    susp = la.MESSAGES["retract.status.suspected"]
    check("PH-05", "標題搜尋發現的通知標為「未經關聯確認」而非直接紅色警示（zh／en 兩種措辭都要）",
          '_T("retract.status.suspected")' in fn and susp["zh"].startswith("⚠️") and susp["en"].startswith("⚠️")
          and "未經 updates 關聯確認" in susp["zh"] and "not confirmed" in susp["en"])
    cav = la.MESSAGES["retract.coverage_caveat"]
    check("PH-06", "無記錄時附年代覆蓋警語（綠勾在舊文獻沒有證據力）：鍵在函式裡、兩種語言都提 2010 與 Retraction Watch",
          "coverage_caveat" in fn and '_T("retract.coverage_caveat")' in fn
          and all("2010" in cav[k] and "Retraction Watch" in cav[k] for k in ("zh", "en")))


def test_rate_limiter_concurrency():
    """併行時速率限制仍須成立——批次模式的正確性靠這個。

    舊版是「讀 _last_call → sleep → 更新」，兩條執行緒會讀到同一個舊值而同時發出
    請求，等於沒有節流（會被 API 封）。改成在鎖內預約時段。此測試不打 API。

    測試設計注意：不可比較各執行緒回傳的「還要等多久」——那是相對於各自呼叫
    time.time() 的時刻，機器負載高時排程漂移會讓它失真（本測試曾因此偶發失敗，
    是測試的問題不是程式的問題）。改為驗證兩件與排程無關的事：
      (a) 單執行緒連續預約的間隔剛好是 min_interval（純算術，確定性）
      (b) N 條執行緒預約後，_last_call 的最終值必須推進 (N-1)×min_interval
          ——代表每條線都拿到不重疊的時段，沒有人共用同一個。
    """
    import threading
    INTERVAL, N = 0.05, 12

    # 比對的是 _last_call 裡**排定的絕對時刻**，不是 _reserve_slot 回傳的等待秒數。
    # 回傳值是 earliest - now，而 now 每次呼叫都在前進，所以兩次回傳值的差 =
    # INTERVAL 減去兩次呼叫之間的真實耗時——它本來就不會剛好等於 INTERVAL，
    # 這條測試因此會偶發變紅（實測六次紅一次，0.049 對 0.05）。
    # 排定時刻則是純算術：earliest = 前一個 earliest + INTERVAL，確定性的。
    # 一條會偶發變紅的測試，訓練出來的反應是「再跑一次」，那等於把閘門關掉。
    la._last_call.clear()
    slots = []
    for _ in range(4):
        la._reserve_slot("seq_bucket", INTERVAL)
        slots.append(la._last_call["seq_bucket"])
    gaps = [round(b - a, 6) for a, b in zip(slots, slots[1:])]
    check("PERF-01", "連續預約的排定時刻間隔剛好是 min_interval（純算術，不受負載影響）",
          all(abs(g - INTERVAL) < 1e-6 for g in gaps), f"間隔={gaps} 期望每個={INTERVAL}")

    la._last_call.clear()
    t0 = time.time()
    ts = [threading.Thread(target=lambda: la._reserve_slot("conc_bucket", INTERVAL))
          for _ in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    advanced = la._last_call["conc_bucket"] - t0
    check("PERF-02", f"{N} 條執行緒各拿到不重疊的時段（節流不因併行失效）",
          advanced >= (N - 1) * INTERVAL - 1e-6,
          f"最終時段推進 {advanced:.3f}s，至少須 {(N - 1) * INTERVAL:.3f}s")

    la._last_call.clear()
    check("PERF-02b", "首次呼叫無等待（冷啟動不該付節流成本）",
          la._reserve_slot("fresh_bucket", 1.0) <= 0.001)
    la._last_call.clear()


def test_verify_one_shared_logic():
    """verify 與 verify-batch 必須共用同一份判定邏輯，不得各寫一份而漂移。"""
    src = open(os.path.join(SKILL, "scripts", "lit_api.py"), encoding="utf-8").read()
    check("PERF-03", "cmd_verify 呼叫 verify_one（不重新實作）",
          "def cmd_verify(args)" in src and "verify_one(args.title" in src)
    check("PERF-04", "verify-batch 也走 verify_one",
          "verify_one(it.get(" in src)


def test_entity_decoding():
    """NS-04（全新 session 實測發現）：Crossref 的期刊名帶 &amp;，原樣寫進 RIS 會讓
    使用者匯入 EndNote 後看到字面的 &amp;。交付物不得被上游的 HTML entity 污染。"""
    fake = {"container-title": ["Soldering &amp; Surface Mount Technology"],
            "title": ["A &amp; B: a study"], "publisher": "Emerald &amp; Co",
            "issued": {"date-parts": [[2019]]}, "DOI": "10.1/x", "author": []}
    n = la.norm_crossref(fake)
    check("NS-04", "Crossref 的 HTML entity 在正規化時解碼",
          "&" in (n["container"] or "") and "&amp;" not in (n["container"] or "")
          and "&amp;" not in (n["title"] or "") and "&amp;" not in (n["publisher"] or ""),
          f"container={n['container']!r} title={n['title']!r}")

def test_chinese_punctuation():
    """使用者可見的中文輸出必須用全形標點——半形混在中文裡是台灣學術寫作的格式錯誤，
    而使用者會把報告內容直接複製進論文。"""
    half = re.compile(r'[一-鿿][,:;!?]|[,:;][一-鿿]')
    for fname in ("lit_api.py", "cite_integrity.py"):
        path = os.path.join(SKILL, "scripts", fname)
        src = open(path, encoding="utf-8").read()
        # 只檢查字串常值(那才是輸出給使用者的)，註解與程式碼不算
        bad = []
        for m in re.finditer(r'"([^"\n]*[一-鿿][^"\n]*)"', src):
            if half.search(m.group(1)):
                bad.append(m.group(1)[:40])
        check(f"PUNCT-{fname[:4]}", f"{fname} 的中文輸出字串使用全形標點",
              not bad, f"{len(bad)} 條含半形：{bad[:3]}")


def test_fulltext_boundary():
    """fulltext 不得暗示可繞過付費牆，且查無免費版時必須給機構取得途徑。"""
    src = open(os.path.join(SKILL, "scripts", "lit_api.py"), encoding="utf-8").read()
    fn = src[src.index("def cmd_fulltext"):src.index("def cmd_retract")]
    # FT-01／02 以前 grep 函式原始碼裡的中文；文字搬進 MESSAGES 表之後改驗「用的是這把鍵」＋「鍵的兩種語言內容」。
    note, lib = la.MESSAGES["fulltext.note"], la.MESSAGES["fulltext.how.library"]
    check("FT-01", "fulltext 明確聲明不繞過付費牆（zh／en）",
          '_T("fulltext.note")' in fn and "不繞過付費牆" in note["zh"] and "does not bypass" in note["en"])
    check("FT-02", "查無免費版時提供機構取得途徑（zh／en）",
          "how_to_get" in fn and '_T("fulltext.how.library")' in fn
          and "圖書館" in lib["zh"] and "library" in lib["en"])
    check("FT-03", "Unpaywall 與備援來源不一致時明說差異", "source_discrepancy" in fn)


def test_output_hardening():
    # 酬載必須含**真正的控制字元**。以前只放 \n,而 \n 在 XML 裡完全合法,
    # 所以就算把清理程式整段刪掉,產出照樣解析得過——CX-17 是一條不可能失敗的
    # 測試。\x0c(換頁)與 \x1f(單元分隔)是 XML 1.0 不接受的字元,放進去之後,
    # 這條測試才真的在測清理有沒有做。
    poison = [{"title": "Legit \x0c Title\x1f\nER  - \nTY  - JOUR\nTI  - Injected",
               "authors": ["A. Author"], "year": 2024, "doi": "10.1/x"}]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(poison, f, ensure_ascii=False)
        path = f.name
    try:
        code, out, _ = run_cli("lit_api.py", "export-xml", path)
        check("CX-16", "export-xml 對含換行的標題產出合法 XML",
              code == 0 and "<record>" in out, f"exit={code}")
        import xml.etree.ElementTree as ET
        try:
            ET.fromstring(out[out.index("<xml>"):])
            wellformed = True
        except Exception as e:
            wellformed = False
        check("CX-17", "產出的 XML 可被解析(控制字元已清理)", wellformed)
        # CX-15 一直只存在於上面那行註解裡,從來沒有被寫出來。注入酬載的意圖是
        # 用一行「ER  - 」提前關掉這一筆、再開一筆假的;所以真正要驗的是
        # **產出恰好一筆記錄**,而不只是「解析得過」。
        check("CX-15", "注入的 ER/TY 沒有生出第二筆記錄",
              out.count("<record>") == 1, f"<record> 出現 {out.count('<record>')} 次")
    finally:
        os.unlink(path)


def test_versions_s2_absent():
    """CX-18（2026-10-13 唯讀 agent 實測 `versions ARXIV:2608.28300`）：S2 尚未收錄該 arXiv
    編號時整支崩潰。路徑：S2 回 404 → 被記成 s2_error、s2_record 留在 None → arXiv API 補到
    標題 → Crossref 標題搜尋找到候選正式版 → 走到「S2 記錄無 DOI 就把信心降為 medium」那行，
    對 None 呼叫 .get() → AttributeError。這裡用假物件驅動同一條路徑，呼叫的是生產的
    cmd_versions。不連網：http_get 被換成會直接失敗的假物件，任何漏網的網路出口都會現形。"""
    import contextlib
    import io
    import types
    import urllib.error
    spec = importlib.util.spec_from_file_location(
        "la_ver", os.path.join(SKILL, "scripts", "lit_api.py"))
    la_ver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(la_ver)

    title = "MaCoPlanner: LLM-Assisted Manual-Compiled Task Planning for Robotic Panel Operation"
    # 真實崩潰案例裡 Crossref 找到的是一筆 SSRN（posted-content）——那其實也是預印本，
    # 2026-10-14 起不再被當成正式版（見 CX-18h）。這裡改用一筆期刊論文當候選，讓本函式繼續
    # 驅動它存在的理由：s2_record 為 None 時走到「找到正式版」那一支不能崩潰。
    pub = {"source": "crossref", "title": title, "year": 2026, "doi": "10.1000/macoplanner.2026",
           "type": "journal-article", "container": "Robotics and Autonomous Systems",
           "authors": ["Xin, Guipeng"]}

    def s2_404(pid):
        raise urllib.error.HTTPError("https://api.semanticscholar.org/graph/v1/paper/" + pid,
                                     404, "Not Found", {}, None)

    def no_network(*a, **k):
        raise AssertionError("測試不得連網：http_get 被呼叫 %r" % (a[:1],))

    def run(ident, crossref_hits):
        la_ver.http_get = no_network
        la_ver.s2_get_paper = s2_404
        la_ver.arxiv_query = lambda *a, **k: [{"title": title, "year": 2026}]
        la_ver.crossref_search = lambda *a, **k: list(crossref_hits)
        buf, code, exc = io.StringIO(), 0, None
        try:
            with contextlib.redirect_stdout(buf):
                la_ver.cmd_versions(types.SimpleNamespace(id=ident))
        except SystemExit as e:
            code = e.code
        except Exception as e:
            exc = f"{type(e).__name__}: {e}"
        out = buf.getvalue()
        return code, exc, (json.loads(out) if out.strip().startswith("{") else {})

    # 崩潰當時的實際情境：S2 無記錄，但 arXiv 標題經 Crossref 找到候選正式版
    code, exc, d = run("ARXIV:2608.28300", [pub])
    check("CX-18", "versions 在 S2 查無 arXiv 記錄時不崩潰：仍回報 Crossref 找到的版本、信心 medium、附 S2 缺漏原因、exit 0",
          exc is None and code == 0 and d.get("s2_record") is None
          and d.get("published_version") and d.get("confidence") == "medium"
          and "查無" in (d.get("s2_note") or "") and not d.get("s2_error"),
          f"exc={exc} exit={code} s2_note={d.get('s2_note')!r} s2_error={d.get('s2_error')!r} "
          f"confidence={d.get('confidence')!r}")

    # 同一缺口的另一半：S2 無記錄且 Crossref 也沒有候選——「查無記錄」不是「查詢失敗」，
    # 不得走「請稍後重試」並回非零；要給可行動的建議並 exit 0
    code, exc, d = run("ARXIV:2608.28300", [])
    check("CX-18b", "S2 查無記錄且無正式版候選時，回 arXiv 版建議而非「查詢失敗請重試」，exit 0",
          exc is None and code == 0 and d.get("published_version") is None
          and "查無" in (d.get("s2_note") or "") and not d.get("s2_error")
          # 原本只檢查建議裡有「arXiv」：ARXIV_ID_NOT_FOUND 的訊息也含這個字，擋不住誤判
          and "查無正式發表版" in (d.get("recommendation") or ""),
          f"exc={exc} exit={code} recommendation={d.get('recommendation')!r}")

    # 直接給標題的路徑（else 分支）從頭到尾不會設 s2_record，它恆為 None：只要 Crossref
    # 找到候選就走到同一行——不是偶發，是每次必崩（另一個 session 離線重現後指出）
    code, exc, d = run(title, [pub])
    check("CX-18c", "versions 直接給標題且 Crossref 找到候選時不崩潰（s2_record 恆為 None 的路徑）",
          exc is None and code == 0 and d.get("s2_record") is None
          and d.get("published_version") and d.get("confidence") == "medium",
          f"exc={exc} exit={code} confidence={d.get('confidence')!r}")

def test_versions_published_lookup():
    """CX-18d～g（2026-10-14：獨立驗證 CX-18 修正時發現 d，使用者回饋引出 e/f/g）。
    d：編號根本不存在（S2 與 arXiv 都回查無）——CX-18 修正後會落到「引 arXiv 版合理」並回 0，
       等於替打錯的編號背書。要 error=ARXIV_ID_NOT_FOUND、非零。
    e：正式版改了標題——標題相似度 ≥ 0.93 找不到，要靠作者找；只列為「可能」，不自動改引。
       三個誘餌各被一個條件擋掉（年份更早、作者不同、題目無關），每個篩選條件都測到。
    f：作者在 arXiv 自填的期刊 DOI——最直接的版本連結，以前抓了卻沒用。
    g：查無正式版時不得再說「引 arXiv 版合理」——很多期刊不收 arXiv 預印本。
    不連網：http_get 只回應明確列出的 Crossref /works/<doi>，其餘一律記成外洩並讓案例失敗。"""
    import contextlib
    import io
    import types
    import urllib.error
    spec = importlib.util.spec_from_file_location(
        "la_pub", os.path.join(SKILL, "scripts", "lit_api.py"))
    la_p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(la_p)

    pre_title = "Neural Widget Alignment: A Contrastive Approach to Cross-Domain Widget Matching"
    pre = {"title": pre_title, "year": 2024, "doi": None,
           "authors": ["Alice Chen", "Bob Okafor", "Carla Diaz"]}
    same_authors = ["Chen, Alice", "Okafor, Bob", "Diaz, Carla"]
    renamed = {"source": "crossref", "title": "Contrastive Cross-Domain Matching of Neural Widgets",
               "year": 2025, "doi": "10.1000/widget.2025.7", "type": "journal-article",
               "container": "Journal of Widget Research", "authors": same_authors}
    earlier = dict(renamed, title="Neural Widget Alignment Preliminary Notes",
                   year=2022, doi="10.1000/widget.2022.1")
    stranger = dict(renamed, doi="10.1000/other.2025.3", authors=["Smith, Dana", "Lee, Evan"])
    unrelated = dict(renamed, title="Soil Moisture Sensing in Arid Vineyards",
                     doi="10.1000/soil.2025.9")

    def s2_404(pid):
        raise urllib.error.HTTPError("https://api.semanticscholar.org/graph/v1/paper/" + pid,
                                     404, "Not Found", {}, None)

    def run(arxiv_entries, by_title, by_author, works=None):
        works, leaked = works or {}, []

        def fake_http_get(url, *a, **k):
            for doi, msg in works.items():
                if url.endswith("/works/" + doi):
                    return json.dumps({"message": msg})
            leaked.append(url)
            raise AssertionError("測試不得連網：http_get 被呼叫 %r" % (url,))

        la_p.http_get = fake_http_get
        la_p.s2_get_paper = s2_404
        la_p.arxiv_query = lambda *a, **k: [dict(e) for e in arxiv_entries]
        la_p.crossref_search = lambda t, rows=5, author=None: [
            dict(c) for c in (by_author if author else by_title)]
        buf, code, exc = io.StringIO(), 0, None
        try:
            with contextlib.redirect_stdout(buf):
                la_p.cmd_versions(types.SimpleNamespace(id="ARXIV:2410.01234"))
        except SystemExit as e:
            code = e.code
        except Exception as e:
            exc = f"{type(e).__name__}: {e}"
        out = buf.getvalue()
        d = json.loads(out) if out.strip().startswith("{") else {}
        return code, (exc or ("leaked=%r" % leaked if leaked else None)), d

    code, exc, d = run([], [], [])
    check("CX-18d", "編號在 S2 與 arXiv 都查無時回 ARXIV_ID_NOT_FOUND 並非零，不背書引用",
          exc is None and code == 1 and d.get("error") == "ARXIV_ID_NOT_FOUND"
          and "合理" not in (d.get("recommendation") or ""),
          f"exc={exc} exit={code} error={d.get('error')!r} rec={d.get('recommendation')!r}")

    code, exc, d = run([pre], [], [renamed, earlier, stranger, unrelated])
    found = [c.get("doi") for c in d.get("possible_published_versions") or []]
    check("CX-18e", "正式版改了標題時以作者找到它，只列為「可能」、信心 low，三個誘餌全被擋掉",
          exc is None and code == 0 and d.get("published_version") is None
          and found == ["10.1000/widget.2025.7"] and d.get("confidence") == "low"
          and "核對" in (d.get("recommendation") or ""),
          f"exc={exc} exit={code} found={found} confidence={d.get('confidence')!r}")

    works = {"10.1000/widget.2025.7": {
        "title": ["Contrastive Cross-Domain Matching of Neural Widgets"],
        "DOI": "10.1000/widget.2025.7", "type": "journal-article",
        "container-title": ["Journal of Widget Research"],
        "issued": {"date-parts": [[2025]]},
        # 正式版列出全部三位作者：只列一位的話作者重疊只有 0.33，作者自填 DOI 的合理性
        # 檢查（作者至少一半對得上）會正確地把它降成「可能」——真實的正式版不會這樣。
        "author": [{"family": "Chen", "given": "Alice"}, {"family": "Okafor", "given": "Bob"},
                   {"family": "Diaz", "given": "Carla"}]}}
    code, exc, d = run([dict(pre, doi="10.1000/widget.2025.7")], [], [], works)
    pv = d.get("published_version") or {}
    check("CX-18f", "arXiv 上作者自填的期刊 DOI 直接當成正式版，且不被降成「由標題推定」",
          exc is None and code == 0 and pv.get("doi") == "10.1000/widget.2025.7"
          and d.get("version_link") == "arxiv_journal_ref"
          and d.get("confidence") != "medium" and "作者自填" in (d.get("confidence_note") or ""),
          f"exc={exc} exit={code} pv={pv.get('doi')!r} link={d.get('version_link')!r} "
          f"confidence={d.get('confidence')!r}")

    code, exc, d = run([pre], [], [])
    rec = d.get("recommendation") or ""
    check("CX-18g", "查無正式版時不說「引 arXiv 版合理」，改提醒很多期刊不收 arXiv",
          exc is None and code == 0 and "合理" not in rec and "不接受" in rec
          and "標題與作者" in rec,
          f"exc={exc} exit={code} rec={rec!r}")


def test_versions_review_fixes():
    """CX-18h～u（2026-10-14：兩位反駁者審查 CX-18d～g 的修正時實測到的缺陷與存活突變，逐一凍結）。
    h：Crossref 的 posted-content（SSRN 等預印本伺服器）被當成「正式版」建議改引；arXiv 自己的
       DOI 不得被當成作者自填的期刊 DOI。
    i：DOI 輸入、Crossref 查無但 S2 有這篇——不得說成兩邊都查無（存在性改用明確旗標）。
    j：S2 有記錄但還沒合併正式版時（最常見），也要讀 arXiv 上作者自填的期刊 DOI；網址前綴要清掉。
    k：作者自填的 DOI 在 Crossref 查無——不得推薦改引，並說明原因。
    l：作者自填的 DOI 指向作者、題目都對不上的紀錄——只列為「可能」。
    m：姓氏帶 van der／de 的兩種寫法要對得上；期刊早預印本一年仍要找得到，並標出來。
    n：作者欄全空時沒有真的做作者搜尋，建議文字不得寫「標題與作者搜尋都」。
    o：S2 的 DOI 只是 arXiv 自己的 DOI 時，由標題推定的正式版信心要降為 medium；已找到正式版
       就不再做作者搜尋。
    p：只有一位作者時題目門檻拉高，同姓但題目只部分相近的要被擋掉。
    q：DOI 路徑 Crossref 逾時（非 HTTP 錯誤）不得直接拋例外當掉。
    r：作者搜尋真的把作者姓氏送進 Crossref 的 query.author（這一案讓真的 crossref_search 跑）。
    s：作者搜尋的每個篩選在邊界上：同年要收、2/3 作者要收、1/3 要擋、缺 DOI／缺年份／缺作者／
       arXiv DOI／預印本伺服器／題目不相干都要擋；擋錯的話它們會排到前面，看得見。
    t：超過三筆時依（作者重疊、題目相似）由高到低排序並截到三筆。
    u：作者搜尋本身失敗時不得吞掉：記成查詢失敗、回非零。
    不連網：http_get 只回應明確列出的 Crossref 端點，其餘一律記成外洩並讓案例失敗。"""
    import contextlib
    import io
    import types
    import urllib.error
    import urllib.parse
    spec = importlib.util.spec_from_file_location(
        "la_rev", os.path.join(SKILL, "scripts", "lit_api.py"))
    la_r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(la_r)
    real_crossref_search = la_r.crossref_search

    def http_err(url, code):
        return urllib.error.HTTPError(url, code, "fixture", {}, None)

    def run(ident, s2=None, arxiv=(), by_title=(), by_author=(), works=None,
            crossref_down=False, search=None):
        """search 給值時不替換 crossref_search，改由假 http_get 回 Crossref 搜尋結果：
        {"title": [原始 item…], "author": [原始 item…]}。"""
        works, leaked, urls = works or {}, [], []

        def fake_http_get(url, *a, **k):
            urls.append(url)
            if "api.crossref.org/works?" in url and search is not None:
                items = search["author"] if "query.author=" in url else search["title"]
                return json.dumps({"message": {"items": items}})
            if "api.crossref.org/works/" in url:
                if crossref_down:
                    raise urllib.error.URLError("模擬 Crossref 逾時")
                for doi, msg in works.items():
                    if url.endswith("/works/" + doi):
                        if msg == 404:
                            raise http_err(url, 404)
                        return json.dumps({"message": msg})
                raise http_err(url, 404)
            leaked.append(url)
            raise AssertionError("測試不得連網：http_get 被呼叫 %r" % (url,))

        def fake_s2(pid):
            if s2 is None:
                raise http_err("https://api.semanticscholar.org/graph/v1/paper/" + pid, 404)
            return dict(s2)

        def fake_search(t, rows=5, author=None):
            src = by_author if author else by_title
            if isinstance(src, Exception):
                raise src
            return [dict(c) for c in src]

        la_r.http_get = fake_http_get
        la_r.s2_get_paper = fake_s2
        la_r.arxiv_query = lambda *a, **k: [dict(e) for e in arxiv]
        la_r.crossref_search = real_crossref_search if search is not None else fake_search
        buf, code, exc = io.StringIO(), 0, None
        try:
            with contextlib.redirect_stdout(buf):
                la_r.cmd_versions(types.SimpleNamespace(id=ident))
        except SystemExit as e:
            code = e.code
        except Exception as e:
            exc = f"{type(e).__name__}: {e}"
        out = buf.getvalue()
        d = json.loads(out) if out.strip().startswith("{") else {}
        return code, (exc or ("leaked=%r" % leaked if leaked else None)), d, urls

    w_title = "Neural Widget Alignment: A Contrastive Approach to Cross-Domain Widget Matching"
    renamed_t = "Contrastive Cross-Domain Matching of Neural Widgets"
    w_authors = ["Alice Chen", "Bob Okafor", "Carla Diaz"]
    cr3 = ["Chen, Alice", "Okafor, Bob", "Diaz, Carla"]
    w_pre = {"title": w_title, "year": 2024, "authors": w_authors, "doi": None}
    w_s2 = {"title": w_title, "year": 2024, "authors": w_authors, "doi": None, "arxiv": "2410.01234"}
    w_msg = {"title": [renamed_t], "DOI": "10.1000/widget.2025.7", "type": "journal-article",
             "container-title": ["Journal of Widget Research"], "issued": {"date-parts": [[2025]]},
             "author": [{"family": "Chen", "given": "Alice"}, {"family": "Okafor", "given": "Bob"},
                        {"family": "Diaz", "given": "Carla"}]}

    def cand(doi, title=renamed_t, year=2025, authors=cr3, type_="journal-article"):
        return {"source": "crossref", "title": title, "year": year, "doi": doi,
                "type": type_, "container": "Journal of Widget Research", "authors": list(authors)}

    # h
    ssrn = cand("10.2139/ssrn.1234567", title=w_title, year=2024, type_="posted-content")
    code, exc, d, _ = run("ARXIV:2410.01234",
                          arxiv=[dict(w_pre, doi="10.48550/arXiv.2410.01234")],
                          by_title=[ssrn], by_author=[ssrn])
    rec = d.get("recommendation") or ""
    check("CX-18h", "SSRN 等 posted-content 是預印本，不得當成正式版；arXiv 自己的 DOI 不是期刊 DOI",
          exc is None and code == 0 and d.get("published_version") is None
          and not d.get("possible_published_versions") and "已有正式發表版" not in rec
          and "查無正式發表版" in rec and "journal_doi" not in (d.get("arxiv_record") or {}),
          f"exc={exc} exit={code} pv={(d.get('published_version') or {}).get('doi')!r} "
          f"arxiv_record={d.get('arxiv_record')!r}")

    # i
    code, exc, d, _ = run("DOI:10.48550/arXiv.2410.01234",
                          s2=dict(w_s2, doi="10.48550/arXiv.2410.01234"))
    rec = d.get("recommendation") or ""
    check("CX-18i", "DOI 輸入、Crossref 查無但 S2 有這篇時，不得說成兩邊都查無；作者仍取自 S2",
          exc is None and code == 0 and d.get("error") != "ARXIV_ID_NOT_FOUND"
          and "查無正式發表版" in rec and "標題與作者" in rec,
          f"exc={exc} exit={code} error={d.get('error')!r} rec={rec[:40]!r}")

    # j
    code, exc, d, _ = run("ARXIV:2410.01234", s2=w_s2,
                          arxiv=[dict(w_pre, doi="https://doi.org/10.1000/widget.2025.7")],
                          works={"10.1000/widget.2025.7": w_msg})
    pv = d.get("published_version") or {}
    check("CX-18j", "S2 有記錄但未合併正式版時，仍讀 arXiv 上作者自填的期刊 DOI（網址前綴要清掉）",
          exc is None and code == 0 and pv.get("doi") == "10.1000/widget.2025.7"
          and d.get("version_link") == "arxiv_journal_ref"
          and (d.get("arxiv_record") or {}).get("journal_doi") == "10.1000/widget.2025.7",
          f"exc={exc} exit={code} pv={pv.get('doi')!r} link={d.get('version_link')!r}")

    # k
    code, exc, d, _ = run("ARXIV:2410.01234", arxiv=[dict(w_pre, doi="10.1000/missing.1")],
                          works={"10.1000/missing.1": 404})
    status = (d.get("arxiv_record") or {}).get("journal_doi_status") or ""
    check("CX-18k", "作者自填的 DOI 在 Crossref 查無時不得推薦改引，並在建議裡說明原因",
          exc is None and code == 0 and d.get("published_version") is None
          and "查無此 DOI" in status and "journal_doi_status" in (d.get("recommendation") or ""),
          f"exc={exc} exit={code} status={status!r}")

    # l
    soil_msg = {"title": ["Soil Moisture Sensing in Arid Vineyards"], "DOI": "10.1000/soil.1",
                "type": "journal-article", "container-title": ["Vineyard Science"],
                "issued": {"date-parts": [[2025]]}, "author": [{"family": "Nobody", "given": "Z"}]}
    code, exc, d, _ = run("ARXIV:2410.01234", arxiv=[dict(w_pre, doi="10.1000/soil.1")],
                          works={"10.1000/soil.1": soil_msg})
    poss = d.get("possible_published_versions") or []
    check("CX-18l", "作者自填的 DOI 指向作者與題目都對不上的紀錄時，只列為「可能」，不推薦改引",
          exc is None and code == 0 and d.get("published_version") is None
          and [p.get("basis") for p in poss] == ["arxiv_declared_doi_mismatch"]
          and d.get("confidence") == "low",
          f"exc={exc} exit={code} poss={[(p.get('doi'), p.get('basis')) for p in poss]}")

    # m
    t_pre = {"title": "Learning Robust Tulip Detectors from Sparse Field Images", "year": 2024,
             "authors": ["Daan van der Linden", "Eva de Wit"], "doi": None}
    t_pub = cand("10.1000/tulip.2023.4", title="Robust Tulip Detection from Sparse Field Imagery",
                 year=2023, authors=["van der Linden, Daan", "de Wit, Eva"])
    code, exc, d, _ = run("ARXIV:2410.07777", arxiv=[t_pre], by_author=[t_pub])
    poss = d.get("possible_published_versions") or []
    flagged = bool(poss) and poss[0].get("year_before_preprint") is True
    check("CX-18m", "姓氏帶 van der／de 的兩種寫法對得上；期刊早預印本一年仍找得到並標出",
          exc is None and code == 0 and [p.get("doi") for p in poss] == ["10.1000/tulip.2023.4"]
          and flagged,
          f"exc={exc} exit={code} poss={[(p.get('doi'), p.get('year_before_preprint')) for p in poss]}")

    # n
    code, exc, d, _ = run("ARXIV:2410.01234", arxiv=[dict(w_pre, authors=["", "  "])],
                          by_author=[cand("10.1000/should.not.search")])
    rec = d.get("recommendation") or ""
    check("CX-18n", "作者欄全空時沒有真的做作者搜尋，建議文字不得寫成「標題與作者搜尋都」",
          exc is None and code == 0 and "以標題搜尋查無" in rec and "標題與作者" not in rec,
          f"exc={exc} exit={code} rec={rec[:30]!r}")

    # o
    same = cand("10.1000/widget.same", title=w_title)
    code, exc, d, _ = run("ARXIV:2410.01234", s2=dict(w_s2, doi="10.48550/arXiv.2410.01234"),
                          arxiv=[w_pre], by_title=[same],
                          by_author=[cand("10.1000/should.not.search")])
    check("CX-18o", "S2 的 DOI 只是 arXiv 自己的 DOI 時，由標題推定的正式版信心降為 medium；不再多做作者搜尋",
          exc is None and code == 0
          and (d.get("published_version") or {}).get("doi") == "10.1000/widget.same"
          and d.get("confidence") == "medium" and not d.get("possible_published_versions"),
          f"exc={exc} exit={code} confidence={d.get('confidence')!r} "
          f"poss={[p.get('doi') for p in d.get('possible_published_versions') or []]}")

    # p
    s_pre = {"title": "Deep Learning for Image Classification", "year": 2024,
             "authors": ["Wei Wang"], "doi": None}
    s_decoy = cand("10.1000/dl.decoy", title="Image Classification with Deep Learning",
                   authors=["Wang, Wei"])
    s_real = cand("10.1000/dl.real", title="Deep Learning Models for Image Classification Tasks",
                  authors=["Wang, Wei"])
    code, exc, d, _ = run("ARXIV:2410.08888", arxiv=[s_pre], by_author=[s_decoy, s_real])
    found = [p.get("doi") for p in d.get("possible_published_versions") or []]
    check("CX-18p", "只有一位作者時題目門檻拉高（0.5），同姓但題目只部分相近（0.40）的要被擋掉",
          exc is None and code == 0 and found == ["10.1000/dl.real"],
          f"exc={exc} exit={code} found={found}")

    # q
    code, exc, d, _ = run("DOI:10.1000/some.paper", crossref_down=True)
    check("CX-18q", "DOI 路徑 Crossref 逾時不得直接拋例外當掉：記成查詢失敗並回非零",
          exc is None and code == 1 and d.get("crossref_error"),
          f"exc={exc} exit={code} crossref_error={d.get('crossref_error')!r}")

    # r
    raw_renamed = dict(w_msg)
    code, exc, d, urls = run("ARXIV:2410.01234", s2=w_s2, arxiv=[w_pre],
                             search={"title": [], "author": [raw_renamed]})
    author_urls = [u for u in urls if "query.author=" in u]
    sent = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("query.author") for u in author_urls]
    check("CX-18r", "作者搜尋真的把第一、第二作者的姓氏依序送進 Crossref 的 query.author（S2 命中的路徑）",
          exc is None and code == 0 and sent == [["chen"], ["okafor"]]
          and [p.get("doi") for p in d.get("possible_published_versions") or []] == ["10.1000/widget.2025.7"],
          f"exc={exc} exit={code} sent={sent}")

    # s
    keep_same = cand("10.1000/s.same", year=2024)
    keep_two = cand("10.1000/s.two", authors=["Chen, Alice", "Okafor, Bob"])
    decoys = [cand("10.1000/s.partial", title=w_title, authors=["Chen, Alice"]),
              cand(None, title=w_title),
              cand("10.48550/arXiv.2501.00001", title=w_title),
              cand("10.1000/s.noyear", title=w_title, year=None),
              cand("10.1000/s.noauth", title=w_title, authors=[]),
              cand("10.1000/s.lowts", title="Neural Approaches to Inventory Forecasting"),
              cand("10.31234/osf.io/abc", title=w_title, type_="posted-content")]
    code, exc, d, _ = run("ARXIV:2410.01234", arxiv=[w_pre], by_author=decoys + [keep_two, keep_same])
    found = [p.get("doi") for p in d.get("possible_published_versions") or []]
    check("CX-18s", "作者搜尋篩選在邊界上：同年與 2/3 作者要收；1/3、缺 DOI／年份／作者、arXiv DOI、預印本、題目不相干都要擋",
          exc is None and code == 0 and found == ["10.1000/s.same", "10.1000/s.two"],
          f"exc={exc} exit={code} found={found}")

    # t
    a = cand("10.1000/t.a", title=w_title)
    b = cand("10.1000/t.b")
    c = cand("10.1000/t.c", title=w_title, authors=["Chen, Alice", "Okafor, Bob"])
    dd = cand("10.1000/t.d", authors=["Chen, Alice", "Okafor, Bob"])
    code, exc, d, _ = run("ARXIV:2410.01234", arxiv=[w_pre], by_author=[dd, b, c, a])
    found = [p.get("doi") for p in d.get("possible_published_versions") or []]
    check("CX-18t", "可能的正式版依（作者重疊、題目相似）由高到低排序，並截到三筆",
          exc is None and code == 0 and found == ["10.1000/t.a", "10.1000/t.b", "10.1000/t.c"],
          f"exc={exc} exit={code} found={found}")

    # u
    code, exc, d, _ = run("ARXIV:2410.01234", arxiv=[w_pre],
                          by_author=urllib.error.URLError("模擬作者搜尋逾時"))
    check("CX-18u", "作者搜尋本身失敗時不得吞掉：記成查詢失敗並回非零",
          exc is None and code == 1 and d.get("crossref_error"),
          f"exc={exc} exit={code} crossref_error={d.get('crossref_error')!r}")


def test_versions_round3():
    """CX-19a～p（2026-10-14 第三輪：surname() 改寫的反駁者 + cmd_versions 改寫的反駁者）。
    a：姓氏相同、前綴不同的兩個人（García López／Pérez López，逗號寫法與混合寫法）不得判 found。
    b：「de la Cruz, Ana」對「Cruz, Pedro」是不同人；對「Ana de la Cruz」是同一人。
    c：日本姓 Ii 不是羅馬數字後綴——「Naosuke Ii」對「Ii, Naosuke」要判 found。
    d：書評人與原著作者共用尾姓時，書評防護仍要發動（看名字首字母）。
    e：S2 合併出的 DOI 指向預印本伺服器（SSRN）時不採用，改走標題／作者搜尋。
    f：S2 合併的 DOI 在 Crossref 查無 → 不採用並說明；Crossref 5xx → 採用但信心 medium、不印 None。
    g：DOI 輸入、靠標題推定出的正式版，信心必須是 medium（輸入 DOI 不是 S2 合併訊號）。
    h：標題搜尋取最像的一筆，不是 Crossref 排第一的；比預印本早兩年以上的同名論文要排除。
    i：輸入的 DOI 本身就是期刊論文時，它就是正式版，不去改引另一個同名 DOI。
    j：來源有記錄但沒標題時，什麼都沒搜，不得寫「搜尋查無」；要回非零。
    k：「arxiv: 2410.01234v2」的前導空白與版本後綴要清掉再送 S2，不得誤報編號不存在。
    l：S2 有題目沒作者時，作者改用 arXiv 的，自填 DOI 才對得上作者。
    m：較強訊號查詢失敗、較弱訊號找到時，信心不得高於 medium，且要說哪條路沒走完。
    n：「可能的正式版」整體依（作者重疊、題目相似）排序：自填 DOI 對不上的那筆不得佔第一格。
    o：網址形式的 DOI（https://doi.org/…）要被認成 DOI。
    p：只剩一個字母的作者名（「J.」）沒有鑑別力，作者重疊回 None 而不是 1.0。
    （CX-20d／e 也放在這裡：d 第一、第二作者在發表時對調，只有第二作者的查詢找得到正式版；
    e 一位作者的查詢失敗時，另一位的結果不丟、失敗如實回報。）
    （CX-21 也放在這裡：S2 合併的 DOI 經 Crossref 覆核成功時，信心不得被降成 medium、不得附「覆核失敗」說明；
    覆核失敗那一支——medium＋「未經覆核」說明——由 CX-19f 的 5xx 案釘住。）
    不連網：http_get 只回應明確列出的端點，其餘記成外洩並讓案例失敗。"""
    import contextlib
    import io
    import types
    import urllib.error
    spec = importlib.util.spec_from_file_location(
        "la_r3", os.path.join(SKILL, "scripts", "lit_api.py"))
    la3 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(la3)

    def http_err(url, code):
        return urllib.error.HTTPError(url, code, "fixture", {}, None)

    # ---- verify_one 驅動：名字比對 ----
    def vrun(q_authors, cand_authors, title="Robust Widget Matching Under Domain Shift"):
        leaked = []

        def fake_http_get(url, *a, **k):
            if "api.semanticscholar.org" in url and "/paper/search/match" in url:
                return json.dumps({"data": []})
            leaked.append(url)
            raise OSError("測試替身拒絕連網：%s" % url)

        la3.http_get = fake_http_get
        la3.crossref_search = lambda t, rows=5, author=None: [{
            "source": "crossref", "title": title, "year": 2021, "doi": "10.1000/widget.v",
            "type": "journal-article", "container": "J. Widgets", "authors": list(cand_authors)}]
        r = la3.verify_one(title, authors=list(q_authors), year=2021)
        assert not leaked, leaked
        return r.get("verdict_hint"), (r.get("identity_basis") or {}).get("identity_confidence")

    v1, _ = vrun(["García López, María"], ["Pérez López, Juan"])
    v2, _ = vrun(["García López, María"], ["Juan Pérez López"])
    v3, _ = vrun(["García López, María"], ["María García López"])
    ov_cc = la3.author_overlap(["García López, María"], ["Pérez López, Juan"])
    ov_cm = la3.author_overlap(["García López, María"], ["Juan Pérez López"])
    ov_same = la3.author_overlap(["García López, María"], ["María García López"])
    # 直接斷言 author_overlap，不只看最終判定：書評防護（看名字首字母）會在後面把壞掉的
    # 姓氏比對接住，只看判定的話姓氏那一層的突變永遠測不出來
    check("CX-19a", "姓氏相同、前綴不同的兩個人作者重疊為 0、不得判 found（逗號與混合寫法）；同一人兩種寫法重疊 1、判 found",
          ov_cc == 0.0 and ov_cm == 0.0 and ov_same == 1.0
          and v1 != "found" and v2 != "found" and v3 == "found",
          f"overlap 逗號對逗號={ov_cc} 逗號對混合={ov_cm} 同一人={ov_same} | 判定 {v1}/{v2}/{v3}")

    v4, _ = vrun(["de la Cruz, Ana"], ["Cruz, Pedro"])
    v5, _ = vrun(["de la Cruz, Ana"], ["Ana de la Cruz"])
    check("CX-19b", "「de la Cruz, Ana」對「Cruz, Pedro」是不同人；對「Ana de la Cruz」是同一人",
          v4 != "found" and v5 == "found", f"不同人={v4} 同一人={v5}")

    v6, _ = vrun(["Naosuke Ii"], ["Ii, Naosuke"])
    check("CX-19c", "日本姓 Ii 不是羅馬數字後綴：「Naosuke Ii」對「Ii, Naosuke」判 found",
          v6 == "found" and la3.surname("Naosuke Ii") == "ii" and la3.surname("John Smith III") == "smith",
          f"verdict={v6} surname(Naosuke Ii)={la3.surname('Naosuke Ii')!r}")

    rev = {"page": "101-103", "type": "journal-article"}
    fires = lambda sig: any("第一作者是他人" in s for s in sig)
    d1 = fires(la3.derivative_signals(dict(rev, authors=["Pérez López, Juan", "García López, María"]),
                                      query_authors=["García López, María"]))
    d2 = fires(la3.derivative_signals(dict(rev, authors=["Smith, Mary", "Smith, John"]),
                                      query_authors=["Smith, John"]))
    d3 = fires(la3.derivative_signals({"page": "1-28", "type": "journal-article",
                                       "authors": ["Smith, John", "Doe, Ann"]}, query_authors=["Smith, John"]))
    check("CX-19d", "書評人與原著作者共用尾姓或同姓不同名時，書評防護仍發動；真論文不誤報",
          d1 and d2 and not d3, f"共用尾姓={d1} 同姓不同名={d2} 真論文誤報={d3}")

    check("CX-19p", "只剩一個字母的作者名沒有鑑別力：作者重疊回 None，不是 1.0",
          la3.author_overlap(["J."], ["J"]) is None and la3.author_overlap(["Li, W."], ["Wei Li"]) == 1.0,
          f"J.={la3.author_overlap(['J.'], ['J'])!r} Li={la3.author_overlap(['Li, W.'], ['Wei Li'])!r}")

    # ---- cmd_versions 驅動 ----
    w_title = "Neural Widget Alignment: A Contrastive Approach to Cross-Domain Widget Matching"
    renamed_t = "Contrastive Cross-Domain Matching of Neural Widgets"
    w_authors = ["Alice Chen", "Bob Okafor", "Carla Diaz"]
    cr3 = ["Chen, Alice", "Okafor, Bob", "Diaz, Carla"]
    w_pre = {"title": w_title, "year": 2024, "authors": w_authors, "doi": None}
    w_s2 = {"title": w_title, "year": 2024, "authors": w_authors, "doi": None, "arxiv": "2410.01234"}
    w_msg = {"title": [renamed_t], "DOI": "10.1000/widget.2025.7", "type": "journal-article",
             "container-title": ["Journal of Widget Research"], "issued": {"date-parts": [[2025]]},
             "author": [{"family": "Chen", "given": "Alice"}, {"family": "Okafor", "given": "Bob"},
                        {"family": "Diaz", "given": "Carla"}]}

    def msg(doi, title=renamed_t, year=2025, type_="journal-article", authors=cr3):
        return {"title": [title], "DOI": doi, "type": type_, "container-title": ["J"],
                "issued": {"date-parts": [[year]]},
                "author": [{"family": a.split(",")[0], "given": a.split(",")[1].strip()} for a in authors]}

    def cand(doi, title=renamed_t, year=2025, authors=cr3, type_="journal-article"):
        return {"source": "crossref", "title": title, "year": year, "doi": doi,
                "type": type_, "container": "Journal of Widget Research", "authors": list(authors)}

    def run(ident, s2=None, s2_pid=None, arxiv=(), by_title=(), by_author=(), works=None,
            works_status=None, arxiv_down=False, author_map=None, author_log=None):
        works, works_status, leaked = works or {}, works_status or {}, []

        def fake_http_get(url, *a, **k):
            if "api.crossref.org/works/" in url:
                for doi in list(works) + list(works_status):
                    if url.endswith("/works/" + doi):
                        st = works_status.get(doi)
                        if st == 404:
                            raise http_err(url, 404)
                        if st == 500:
                            raise http_err(url, 500)
                        return json.dumps({"message": works[doi]})
                raise http_err(url, 404)
            leaked.append(url)
            raise AssertionError("測試不得連網：http_get 被呼叫 %r" % (url,))

        def fake_s2(pid):
            if s2_pid is not None:
                s2_pid.append(pid)
            if s2 is None:
                raise http_err("https://api.semanticscholar.org/graph/v1/paper/" + pid, 404)
            return dict(s2)

        def fake_arxiv(*a, **k):
            if arxiv_down:
                raise urllib.error.URLError("模擬 arXiv 逾時")
            return [dict(e) for e in arxiv]

        la3.http_get = fake_http_get
        la3.s2_get_paper = fake_s2
        la3.arxiv_query = fake_arxiv
        def fake_crossref_search(t, rows=5, author=None):
            # author_map 給值時，依 query.author 的姓氏分別回結果（並依序記下被查過的姓氏）
            if author and author_log is not None:
                author_log.append(author)
            if author and author_map is not None:
                hit = author_map.get(author, [])
                if isinstance(hit, Exception):
                    raise hit
                return [dict(c) for c in hit]
            return [dict(c) for c in (by_author if author else by_title)]
        la3.crossref_search = fake_crossref_search
        buf, code, exc = io.StringIO(), 0, None
        try:
            with contextlib.redirect_stdout(buf):
                la3.cmd_versions(types.SimpleNamespace(id=ident))
        except SystemExit as e:
            code = e.code
        except Exception as e:
            exc = f"{type(e).__name__}: {e}"
        out = buf.getvalue()
        d = json.loads(out) if out.strip().startswith("{") else {}
        return code, (exc or ("leaked=%r" % leaked if leaked else None)), d

    # e
    code, exc, d = run("ARXIV:2410.01234", s2=dict(w_s2, doi="10.2139/ssrn.999"), arxiv=[w_pre],
                       works={"10.2139/ssrn.999": msg("10.2139/ssrn.999", title=w_title, type_="posted-content")},
                       by_author=[cand("10.1000/widget.2025.7")])
    check("CX-19e", "S2 合併出的 DOI 指向預印本伺服器（SSRN）時不採用、說明原因，並改走作者搜尋",
          exc is None and code == 0 and d.get("published_version") is None
          and "預印本" in (d.get("s2_note") or "")
          and [p.get("doi") for p in d.get("possible_published_versions") or []] == ["10.1000/widget.2025.7"],
          f"exc={exc} exit={code} pv={(d.get('published_version') or {}).get('doi')!r} s2_note={d.get('s2_note')!r}")

    # f
    code, exc, d = run("ARXIV:2410.01234", s2=dict(w_s2, doi="10.5281/zenodo.123"), arxiv=[w_pre],
                       works_status={"10.5281/zenodo.123": 404})
    f_404 = (exc is None and code == 0 and d.get("published_version") is None
             and "查無" in (d.get("s2_note") or ""))
    code2, exc2, d2 = run("ARXIV:2410.01234", s2=dict(w_s2, doi="10.1000/flaky.1"), arxiv=[w_pre],
                          works_status={"10.1000/flaky.1": 500})
    rec2 = d2.get("recommendation") or ""
    f_500 = (exc2 is None and code2 == 0 and (d2.get("published_version") or {}).get("doi") == "10.1000/flaky.1"
             and d2.get("confidence") == "medium" and "None" not in rec2 and "覆核" in (d2.get("confidence_note") or ""))
    check("CX-19f", "S2 合併的 DOI：Crossref 查無就不採用並說明；Crossref 5xx 才採用，且信心 medium、不印 None",
          f_404 and f_500, f"404案 ok={f_404} exc={exc} | 5xx案 ok={f_500} exc={exc2} rec={rec2[:50]!r}")

    # g
    code, exc, d = run("DOI:10.2139/ssrn.999",
                       works={"10.2139/ssrn.999": msg("10.2139/ssrn.999", title=w_title, year=2024, type_="posted-content")},
                       by_title=[cand("10.1000/j.1", title=w_title)])
    check("CX-19g", "DOI 輸入、靠標題推定出的正式版信心必須是 medium（輸入 DOI 不算 S2 合併訊號）",
          exc is None and code == 0 and (d.get("published_version") or {}).get("doi") == "10.1000/j.1"
          and d.get("confidence") == "medium",
          f"exc={exc} exit={code} pv={(d.get('published_version') or {}).get('doi')!r} confidence={d.get('confidence')!r}")

    # h
    near = cand("10.1000/near", title=w_title + " Revisited", authors=["Q, Zed"])   # title_sim≈0.956：過門檻但不是最像
    old = cand("10.1000/old", title=w_title, year=2019)
    exact = cand("10.1000/exact", title=w_title)
    code, exc, d = run("ARXIV:2410.01234", arxiv=[w_pre], by_title=[old, near, exact])
    check("CX-19h", "標題搜尋取最像、作者對得上的一筆，不是 Crossref 排第一的；早兩年以上的同名論文排除",
          exc is None and code == 0 and (d.get("published_version") or {}).get("doi") == "10.1000/exact",
          f"exc={exc} exit={code} pv={(d.get('published_version') or {}).get('doi')!r}")

    # i
    code, exc, d = run("DOI:10.1000/j.1", works={"10.1000/j.1": msg("10.1000/j.1", title=w_title)},
                       by_title=[cand("10.1000/other", title=w_title, type_="proceedings-article")])
    rec = d.get("recommendation") or ""
    check("CX-19i", "輸入的 DOI 本身就是期刊論文時它就是正式版，不改引另一個同名 DOI",
          exc is None and code == 0 and (d.get("published_version") or {}).get("doi") == "10.1000/j.1"
          and d.get("version_link") == "input_doi_is_published" and "可直接引用" in rec,
          f"exc={exc} exit={code} pv={(d.get('published_version') or {}).get('doi')!r} rec={rec[:40]!r}")

    # j
    code, exc, d = run("ARXIV:2410.01234", s2=dict(w_s2, title=None, authors=[]), arxiv=[])
    rec = d.get("recommendation") or ""
    check("CX-19j", "來源有記錄但沒標題時，什麼都沒搜，不得寫「搜尋查無」，要回非零",
          exc is None and code == 1 and "未做任何版本搜尋" in rec and "搜尋查無" not in rec,
          f"exc={exc} exit={code} rec={rec[:50]!r}")

    # k
    pids = []
    code, exc, d = run("arxiv: 2410.01234v2", s2=w_s2, s2_pid=pids, arxiv=[w_pre])
    check("CX-19k", "arXiv 編號的前導空白與版本後綴要清掉再送 S2，不得誤報編號不存在",
          exc is None and code == 0 and pids == ["ARXIV:2410.01234"] and d.get("arxiv") == "2410.01234"
          and d.get("error") != "ARXIV_ID_NOT_FOUND",
          f"exc={exc} exit={code} pids={pids} arxiv={d.get('arxiv')!r} error={d.get('error')!r}")

    # l
    code, exc, d = run("ARXIV:2410.01234", s2=dict(w_s2, authors=[]),
                       arxiv=[dict(w_pre, doi="10.1000/widget.2025.7")],
                       works={"10.1000/widget.2025.7": w_msg})
    check("CX-19l", "S2 有題目沒作者時改用 arXiv 的作者，作者自填 DOI 才對得上、被當成正式版",
          exc is None and code == 0 and (d.get("published_version") or {}).get("doi") == "10.1000/widget.2025.7"
          and d.get("version_link") == "arxiv_journal_ref",
          f"exc={exc} exit={code} pv={(d.get('published_version') or {}).get('doi')!r} "
          f"poss={[p.get('basis') for p in d.get('possible_published_versions') or []]}")

    # m
    code, exc, d = run("ARXIV:2410.01234", s2=w_s2, arxiv_down=True, by_title=[cand("10.1000/exact", title=w_title)])
    note = d.get("confidence_note") or ""
    check("CX-19m", "較強訊號（arXiv）查詢失敗、較弱訊號（標題）找到時：可用但信心 medium，並說哪條路沒走完",
          exc is None and code == 0 and (d.get("published_version") or {}).get("doi") == "10.1000/exact"
          and d.get("confidence") == "medium" and "查詢失敗" in note and d.get("arxiv_error"),
          f"exc={exc} exit={code} confidence={d.get('confidence')!r} note={note[-40:]!r}")

    # n
    junk_msg = msg("10.1000/junk", title="Soil Moisture Sensing in Arid Vineyards", authors=["Nobody, Z"])
    good = [cand("10.1000/a1", title=w_title), cand("10.1000/a2"), cand("10.1000/a3", authors=["Chen, Alice", "Okafor, Bob"])]
    code, exc, d = run("ARXIV:2410.01234", arxiv=[dict(w_pre, doi="10.1000/junk")],
                       works={"10.1000/junk": junk_msg}, by_author=good)
    found = [p.get("doi") for p in d.get("possible_published_versions") or []]
    check("CX-19n", "「可能的正式版」整體依（作者重疊、題目相似）排序：自填 DOI 對不上的那筆不得佔第一格",
          exc is None and code == 0 and found == ["10.1000/a1", "10.1000/a2", "10.1000/a3"],
          f"exc={exc} exit={code} found={found}")

    # o
    code, exc, d = run("https://doi.org/10.1000/j.1", works={"10.1000/j.1": msg("10.1000/j.1", title=w_title)})
    check("CX-19o", "網址形式的 DOI 被認成 DOI 並走 DOI 路徑",
          la3.looks_like_doi("https://doi.org/10.1000/j.1") and not la3.looks_like_doi("Risk/Benefit Analysis")
          and exc is None and code == 0 and d.get("version_link") == "input_doi_is_published",
          f"exc={exc} exit={code} link={d.get('version_link')!r}")

    # CX-20d：預印本第一、第二作者在發表時對調——只查第一作者的姓氏會漏掉。標題搜尋查無、
    # 第一作者的查詢也查無，只有第二作者的查詢帶得出改名後的正式版。
    queried = []
    code, exc, d = run("ARXIV:2410.01234", s2=w_s2, arxiv=[w_pre], by_title=[],
                       author_map={"chen": [], "okafor": [cand("10.1000/widget.2025.7")]},
                       author_log=queried)
    got = [p.get("doi") for p in d.get("possible_published_versions") or []]
    check("CX-20d", "第一作者的查詢查無、只有第二作者的查詢帶得出改名後的正式版時，仍列為可能的正式版",
          exc is None and code == 0 and d.get("published_version") is None
          and got == ["10.1000/widget.2025.7"] and queried == ["chen", "okafor"],
          f"exc={exc} exit={code} found={got} queried={queried}")

    # CX-20e：兩位作者各查一次，其中一位查詢失敗時，另一位已經拿到的結果不得丟掉；
    # 但失敗也不得被吞掉——要記成查詢失敗（crossref_error）。
    queried = []
    code, exc, d = run("ARXIV:2410.01234", s2=w_s2, arxiv=[w_pre], by_title=[],
                       author_map={"chen": urllib.error.URLError("模擬 Crossref 逾時"),
                                   "okafor": [cand("10.1000/widget.2025.7")]},
                       author_log=queried)
    got = [p.get("doi") for p in d.get("possible_published_versions") or []]
    check("CX-20e", "一位作者的查詢失敗時，另一位已拿到的結果不丟、失敗如實記成查詢失敗",
          exc is None and got == ["10.1000/widget.2025.7"] and queried == ["chen", "okafor"]
          and "逾時" in (d.get("crossref_error") or ""),
          f"exc={exc} exit={code} found={got} queried={queried} crossref_error={d.get('crossref_error')!r}")

    # CX-21：S2 合併的 DOI 經 Crossref 覆核成功（期刊論文）——四層訊號裡最強的一種。以前 confidence 分支用
    # p.get("source") 判斷「覆核失敗」，但覆核成功時正式版是 norm_crossref 的記錄、source 恆為 "crossref"，
    # 於是被降成 medium、還附「Crossref 覆核失敗：書目未經核對」。覆核失敗那一支（medium＋未經覆核的說明）
    # 由 CX-19f 的 5xx 案釘住，這裡只釘成功的那一支。
    code, exc, d = run("ARXIV:2410.01234", s2=dict(w_s2, doi="10.1000/widget.2025.7"), arxiv=[w_pre],
                       works={"10.1000/widget.2025.7": w_msg})
    pv = d.get("published_version") or {}
    note = d.get("confidence_note") or ""
    check("CX-21", "S2 合併的 DOI 經 Crossref 覆核成功時，正式版取 Crossref 書目，信心維持預設（不降為 medium）、不附「覆核失敗」說明",
          exc is None and code == 0 and pv.get("doi") == "10.1000/widget.2025.7" and pv.get("source") == "crossref"
          and pv.get("container") == "Journal of Widget Research" and "confidence" not in d
          and "覆核" not in note and d.get("version_link") is None and not d.get("crossref_error"),
          f"exc={exc} exit={code} pv={pv.get('doi')!r}/{pv.get('source')!r} confidence={d.get('confidence')!r} "
          f"note={note[:30]!r} link={d.get('version_link')!r}")


def test_hygiene_cx20():
    """CX-20a～c（2026-10-14 衛生三項：使用者看得到、而且會踩到的小地方）。
    a：Semantic Scholar 第一次回 429 就要在 stderr 給可執行的指引（申請網址、.env 那一行、.env 讀取
       順序），不是等重試全用完；重試次數與退避秒數不得因此改變；已有金鑰、非 S2 網址不印；最終的
       錯誤 JSON 的 hint 也要帶同一份指引。
    b：備份檔不出貨：打包器的過濾函式擋 .bak 檔與目錄（有邊界，.bake 不算）、.gitignore 有 *.bak／*.bak-*／*.bak.*、
       zip_check 對路徑任一段含 .bak 的條目都當風險。
    c：charrefs.docx 位元組可重現：連產生兩次完全相同、時間戳是寫死的常數、且與入庫的那份一致。
    不連網：urlopen 與 sleep 都換成替身（還原在 finally），整個套件另有 NET-01 封網閘門。"""
    import contextlib
    import io
    import shutil
    import urllib.error
    import urllib.request
    import zipfile
    la_path = os.path.join(SKILL, "scripts", "lit_api.py")
    s2_url = "https://api.semanticscholar.org/graph/v1/paper/search?query=widgets"
    page = "https://www.semanticscholar.org/product/api"
    env_line = "S2_API_KEY=<your key>"

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": 1}'

    def drive(outcomes, url=s2_url, call=None, argv=None):
        """跑一次 http_get（或 main）：outcomes 依序是每次 urlopen 的結果（429 或 "ok"）。
        回傳 (結果, 例外, urlopen 次數, sleep 秒數清單, stderr, stdout, 離開碼)。"""
        spec = importlib.util.spec_from_file_location("la_cx20", la_path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.load_dotenv = lambda: None           # main 會讀使用者真正的 .env，測試不得受它影響
        calls, sleeps, it = [], [], iter(outcomes)

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            o = next(it)
            if o in (429, 500):
                raise urllib.error.HTTPError(req.full_url, o, "fixture", {}, None)
            return FakeResp()

        real_open, real_sleep, real_argv = urllib.request.urlopen, time.sleep, sys.argv
        urllib.request.urlopen, time.sleep = fake_urlopen, sleeps.append
        err, out, res, exc, code = io.StringIO(), io.StringIO(), None, None, 0
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                try:
                    if argv is not None:
                        sys.argv = ["lit_api.py"] + argv
                        m.main()
                    else:
                        res = m.http_get(url)
                except SystemExit as e:
                    code = e.code
                except Exception as e:
                    exc = e
        finally:
            urllib.request.urlopen, time.sleep, sys.argv = real_open, real_sleep, real_argv
        return res, exc, len(calls), sleeps, err.getvalue(), out.getvalue(), code

    saved_key = os.environ.pop("S2_API_KEY", None)
    try:
        # a1：第一次 429、第二次成功——重試沒用完也要有指引，而且重試行為沒變（一次退避 6 秒）
        res, exc, n, sleeps, err, _, _ = drive([429, "ok"])
        a1 = (exc is None and res is not None and n == 2 and sleeps == [6]
              and page in err and env_line in err and "~/.env" in err)
        # a2：一直 429——照舊重試 4 次、退避 6/12/24、最後拋 HTTPError；指引只印一次不洗版
        res, exc, n, sleeps, err, _, _ = drive([429, 429, 429, 429])
        a2 = (isinstance(exc, urllib.error.HTTPError) and exc.code == 429 and n == 4
              and sleeps == [6, 12, 24] and err.count(page) == 1 and env_line in err)
        # a3：非 Semantic Scholar 的網址 429，或已設金鑰，都不印申請金鑰的指引（那是誤導）
        _, _, _, _, err_cr, _, _ = drive([429, "ok"], url="https://api.crossref.org/works?query=x")
        os.environ["S2_API_KEY"] = "dummy-test-key"
        _, _, _, _, err_key, _, _ = drive([429, "ok"])
        del os.environ["S2_API_KEY"]
        a3 = page not in err_cr and env_line not in err_cr and page not in err_key
        # a4：最終的錯誤 JSON（stdout）的 hint 也帶同一份指引
        _, _, n4, _, _, out4, code4 = drive([429] * 12, argv=["search", "widgets"])
        try:
            hint = json.loads(out4).get("hint") or ""
        except ValueError:
            hint = ""
        a4 = code4 == 1 and page in hint and env_line in hint and "~/.env" in hint and "60–120" in hint
        check("CX-20a", "S2 第一次 429 就在 stderr 給可執行指引（申請網址＋.env 那一行＋讀取順序）；重試行為不變；"
              "有金鑰或非 S2 網址不印；最終錯誤 JSON 的 hint 帶同一份指引",
              a1 and a2 and a3 and a4,
              f"第一次429={a1} 重試用完={a2} 不誤印={a3} 最終hint={a4}（exit={code4} calls={n4} hint={hint[:30]!r}）")

        # f：反駁者抓到的三件——最終 hint 以前不看主機、不看金鑰，一律叫人去申請 S2 金鑰；
        #    stderr 指引在最後一次嘗試仍說「會自動退避重試」，但接著就是 raise。
        def hint_of(out):
            try:
                return json.loads(out).get("hint") or ""
            except ValueError:
                return ""
        _, _, _, _, _, out_cr, code_cr = drive([429] * 12, argv=["crossref-doi", "10.1000/x"])
        h_cr = hint_of(out_cr)
        f1 = code_cr == 1 and page not in h_cr and env_line not in h_cr and "api.crossref.org" in h_cr
        os.environ["S2_API_KEY"] = "dummy-test-key"
        _, _, _, _, _, out_k, code_k = drive([429] * 12, argv=["search", "widgets"])
        del os.environ["S2_API_KEY"]
        h_k = hint_of(out_k)
        f2 = code_k == 1 and page not in h_k and env_line not in h_k and "金鑰本身" in h_k
        _, _, _, _, err_first, _, _ = drive([429, "ok"])
        _, _, _, _, err_last, _, _ = drive([500, 500, 500, 429])   # 前三次 5xx 重試，最後一次才 429
        f3 = ("會自動退避重試" in err_first and "重試已用完" not in err_first
              and "重試已用完" in err_last and "會自動退避重試" not in err_last)
        check("CX-20f", "429 的 hint 看主機與金鑰：Crossref 的 429 不叫人申請 S2 金鑰、已設金鑰的 S2 429 說是金鑰上限；"
              "stderr 指引只在真的會重試時說會重試",
              f1 and f2 and f3,
              f"crossref={f1}（hint={h_cr[:40]!r}） 有金鑰={f2}（hint={h_k[:40]!r}） 重試措辭={f3}")
    finally:
        if saved_key is not None:
            os.environ["S2_API_KEY"] = saved_key
        else:
            os.environ.pop("S2_API_KEY", None)

    # b：備份檔不出貨
    ps = load_module("package_skill_cx20", os.path.join(SKILL, "scripts", "package_skill.py"))
    skipped = [ps.should_skip(n) for n in ("foo.py.bak-20261014", "x.bak", "lit_api.py.bak-c1",
                                           "x.bak.py", "scripts.bak-20261014")]
    kept = [ps.should_skip(n) for n in ("lit_api.py", "SKILL.md", "backup_notes.md", "charrefs.docx",
                                        "notes.bake.md")]
    # 檔案掃描路徑：備份目錄要整個剪掉，裡面檔名乾淨的 lit_api.py 不得溜進來
    tree = tempfile.mkdtemp(prefix="cx20b-tree-")
    try:
        for rel in ("scripts/lit_api.py", "scripts.bak-20261014/lit_api.py", "notes.bake.md", "x.bak"):
            p = os.path.join(tree, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        found = {os.path.relpath(p, tree).replace(os.sep, "/") for p in ps.scan_files(tree)}
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    dir_pruned = ("scripts/lit_api.py" in found and "notes.bake.md" in found
                  and not any("scripts.bak" in p for p in found))
    still = [ps.should_skip(n) for n in (".env", ".env.local", "x.pyc", ".DS_Store")]
    gi_path = os.path.join(SKILL, ".gitignore")
    gi_lines = ([ln.strip() for ln in open(gi_path, encoding="utf-8")] if os.path.isfile(gi_path) else [])
    tmp = tempfile.mkdtemp(prefix="cx20b-")
    try:
        def zip_with(name, extra):
            p = os.path.join(tmp, name)
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("lit-review/SKILL.md", "# clean\n")
                # zip_check 也查必要檔（I18N-12）：這裡測的是 .bak，其餘必要檔放空殼，免得「乾淨」的 ZIP 因缺檔變紅
                for r in _zip_check_required():
                    if r != "SKILL.md":
                        z.writestr("lit-review/" + r, "stub\n")
                for e in extra:
                    z.writestr(e, "stale copy\n")
            return p

        def zc(p):
            r = subprocess.run([PY, os.path.join(BASE, "zip_check.py"), p], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=60,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            return r.returncode, r.stdout or ""
        code_bad, out_bad = zc(zip_with("bad.zip", ["lit-review/scripts/lit_api.py.bak-20261014"]))
        code_baddir, _ = zc(zip_with("bad_dir.zip", ["lit-review/scripts.bak-20261014/lit_api.py"]))
        code_bake, _ = zc(zip_with("bake.zip", ["lit-review/notes.bake.md"]))
        code_ok, out_ok = zc(zip_with("ok.zip", []))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    gi_ok = all(x in gi_lines for x in ("*.bak", "*.bak-*", "*.bak.*")) and "*.bak*" not in gi_lines
    check("CX-20b", "備份檔不出貨：打包過濾擋 .bak 檔與 .bak 目錄（有邊界：不誤擋 notes.bake.md、backup_notes.md）、"
          ".gitignore 有 *.bak／*.bak-*／*.bak.*、zip_check 對 ZIP 內任何一段路徑含 .bak 回 exit 1，乾淨與 .bake 仍過",
          all(skipped) and not any(kept) and all(still) and gi_ok and dir_pruned
          and code_bad == 1 and ".bak" in out_bad and code_baddir == 1 and code_bake == 0 and code_ok == 0,
          f"過濾擋下={skipped} 誤擋={kept} 原規則={still} gitignore={gi_ok} 目錄剪掉={dir_pruned} "
          f"zip_check(含bak)={code_bad} (bak目錄)={code_baddir} (.bake)={code_bake} (乾淨)={code_ok}")

    # c：fixture 位元組可重現
    mf = load_module("make_fixtures_cx20", os.path.join(BASE, "make_fixtures.py"))
    tmp = tempfile.mkdtemp(prefix="cx20c-")
    try:
        p1, p2 = os.path.join(tmp, "one.docx"), os.path.join(tmp, "two.docx")
        mf.write_charrefs_docx(p1)
        mf.write_charrefs_docx(p2)
        b1, b2 = open(p1, "rb").read(), open(p2, "rb").read()
        with zipfile.ZipFile(p1) as z:
            stamps = {i.date_time for i in z.infolist()}
            methods = {i.compress_type for i in z.infolist()}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    committed = fixture("charrefs.docx")
    same_as_committed = os.path.isfile(committed) and open(committed, "rb").read() == b1
    check("CX-20c", "charrefs.docx 位元組可重現：連產生兩次完全相同、時間戳寫死為 2026-01-01、不壓縮，且與入庫的那份一致",
          b1 == b2 and stamps == {(2026, 1, 1, 0, 0, 0)} and methods == {zipfile.ZIP_STORED}
          and same_as_committed,
          f"兩次相同={b1 == b2} 時間戳={sorted(stamps)} 壓縮={sorted(methods)} 與入庫一致={same_as_committed}")


# ─────────────────────────────────────────────────────────────
# 7. 英文輸出模式（LIT_LANG=en）：I18N-*
# ─────────────────────────────────────────────────────────────

CJK_RE = re.compile(r"[　-〿㐀-鿿＀-￯]")
EMOJI_RE = re.compile("✅|❌|❓|⚠️|🚨|🔒|🚫")
TOKEN_RE = re.compile(r"[A-Za-z_]+_[A-Za-z_*.]+|\*_error")
I18N_ALLOW_NAMES = {"HEADING_RE", "HEADING_LOOSE_RE", "CITE_SEPS", "CJK"}
I18N_ALLOW_TEXT = ("^[^:",)          # cmd_retract 剝標題前綴的 regex：是輸入解析器，不是輸出
MSG_FIELDS = {"note", "_note", "message", "recommendation", "confidence_note", "s2_note", "absence_note",
              "single_source_degraded", "status", "coverage_caveat", "how_to_get", "hint", "quality_warnings",
              "journal_doi_status", "label", "source_discrepancy", "citations_source", "unsupported_style_note",
              "crossref_error", "s2_error", "arxiv_error", "downgrade_reasons", "derivative_item_risk"}


def _ci():
    return load_module("cite_integrity_i18n", os.path.join(SKILL, "scripts", "cite_integrity.py"))


def _t(mod, lang, key, **kw):
    """用指定語言取一條訊息；語言在每次呼叫時讀 os.environ，取完立刻還原成呼叫前的值。"""
    saved = os.environ.get("LIT_LANG")
    os.environ["LIT_LANG"] = lang
    try:
        return mod._T(key, **kw)
    finally:
        if saved is None:
            os.environ.pop("LIT_LANG", None)
        else:
            os.environ["LIT_LANG"] = saved


def _placeholders(tpl):
    import string
    try:
        names = {f for _, f, _, _ in string.Formatter().parse(tpl) if f}
    except ValueError:
        return set()
    return {n for n in names if re.fullmatch(r"[A-Za-z_]\w*", n)}   # 非識別字的 {…} 是字面文字（R4）


def _stray_cjk_literals(path):
    """設計文件附錄 A 的掃描器：docstring、允許清單與 MESSAGES 以外的任何含 CJK 字串常值。"""
    import ast
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body \
           and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            skip.add(id(n.body[0].value))
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in I18N_ALLOW_NAMES | {"MESSAGES"}
                                               for t in n.targets):
            skip.update(id(c) for c in ast.walk(n))
    return [(c.lineno, c.value[:40]) for c in ast.walk(tree)
            if isinstance(c, ast.Constant) and isinstance(c.value, str) and id(c) not in skip
            and CJK_RE.search(c.value) and not c.value.startswith(I18N_ALLOW_TEXT)]


def _table_problems(messages):
    probs = []
    for key, entry in messages.items():
        zh, en = entry.get("zh"), entry.get("en")
        if not isinstance(zh, str) or not zh or not isinstance(en, str) or not en:
            probs.append(f"{key}: zh/en 缺項或不是字串")
            continue
        if _placeholders(zh) != _placeholders(en):
            probs.append(f"{key}: 佔位符不一致 zh={sorted(_placeholders(zh))} en={sorted(_placeholders(en))}")
        if set(TOKEN_RE.findall(zh)) != set(TOKEN_RE.findall(en)):
            probs.append(f"{key}: 識別字不一致 zh={sorted(set(TOKEN_RE.findall(zh)))} en={sorted(set(TOKEN_RE.findall(en)))}")
        if EMOJI_RE.findall(zh) != EMOJI_RE.findall(en):
            probs.append(f"{key}: emoji 不一致")
        if ".status." in key or key.startswith("report.verdict."):
            mz, me = EMOJI_RE.match(zh), EMOJI_RE.match(en)
            if not (mz and me and mz.group() == me.group()):
                probs.append(f"{key}: 狀態 emoji 前綴不同或不在開頭")
        if CJK_RE.search(en):
            probs.append(f"{key}: en 含 CJK／全形字元")
        if any(ord(ch) >= 128 for ch in EMOJI_RE.sub("", en)):
            probs.append(f"{key}: en 含 emoji 以外的非 ASCII 字元")
        for lang, tpl in (("zh", zh), ("en", en)):
            ph = _placeholders(tpl)
            if ph:
                try:
                    tpl.format(**{n: "x" for n in ph})
                except (KeyError, IndexError, ValueError) as e:
                    probs.append(f"{key}: {lang} 模板無法 format（{e}）")
    return probs


def _callsite_problems(path, messages):
    import ast
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    probs, used = [], set()

    def calls(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_T", "_H"):
                yield n

    # _H 本身是 _T 的包裝（return _T(key).replace(...)），它裡面那一次呼叫的鍵是參數，不是字面值
    wrapper = next((f for f in tree.body if isinstance(f, ast.FunctionDef) and f.name == "_H"), None)
    inside_wrapper = {id(n) for n in calls(wrapper)} if wrapper else set()
    for n in calls(tree):
        if id(n) in inside_wrapper:
            continue
        if not n.args or not isinstance(n.args[0], ast.Constant) or not isinstance(n.args[0].value, str):
            probs.append(f"line {n.lineno}: 鍵不是字串字面值")
            continue
        key = n.args[0].value
        used.add(key)
        if key not in messages:
            probs.append(f"line {n.lineno}: 鍵 {key!r} 不在表裡")
            continue
        if len(n.args) > 1 or any(k.arg is None for k in n.keywords):
            probs.append(f"line {n.lineno}: {key} 用了位置參數或 **kwargs")
        kw = {k.arg for k in n.keywords}
        for lang in ("zh", "en"):
            ph = _placeholders(messages[key][lang])
            if kw != ph:
                probs.append(f"line {n.lineno}: {key} 傳了 {sorted(kw)}，{lang} 模板要 {sorted(ph)}")
    for key in sorted(set(messages) - used):
        probs.append(f"表裡的 {key!r} 沒有任何呼叫端")
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for n in calls(stmt):
            probs.append(f"line {n.lineno}: import 時就呼叫 _T/_H（.env 還沒讀）")
    for ln, txt in _stray_cjk_literals(path):
        probs.append(f"line {ln}: 表外殘留中文字串 {txt!r}")
    return probs


def _strip_msgs(obj, extra=()):
    if isinstance(obj, dict):
        return {k: _strip_msgs(v, extra) for k, v in obj.items() if k not in MSG_FIELDS and k not in extra}
    if isinstance(obj, list):
        return [_strip_msgs(v, extra) for v in obj]
    return obj


def _statuses(obj):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "status" and isinstance(v, str):
                out.append(v)
            out += _statuses(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _statuses(v)
    return out


def _capture(fn, *args):
    """在行程內跑一個 cmd_*：回 (exit, stdout, stderr, exception)。"""
    import contextlib
    import io
    out, err, code, exc = io.StringIO(), io.StringIO(), 0, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*args)
        except SystemExit as e:
            code = e.code
        except Exception as e:
            exc = f"{type(e).__name__}: {e}"
    return code, out.getvalue(), err.getvalue(), exc


def _both(fn, *args):
    """同一個情境各跑一次 zh 與 en：回 ((exit, json, exc), (exit, json, exc))。語言只影響訊息欄位。"""
    res = []
    for lang in ("zh", "en"):
        os.environ["LIT_LANG"] = lang
        try:
            code, out, _, exc = _capture(fn, *args)
        finally:
            os.environ["LIT_LANG"] = "zh"
        s = out.strip()
        d = json.loads(s) if s.startswith(("{", "[")) else None
        res.append((code, d, exc, out))
    return res


def test_i18n_tables():
    """I18N-01：兩支腳本的訊息表結構完整——zh/en 鍵集合、佔位符、識別字、emoji 一致；en 純 ASCII；模板可 format。"""
    ci = _ci()
    probs = [("lit_api", p) for p in _table_problems(la.MESSAGES)] + \
            [("cite_integrity", p) for p in _table_problems(ci.MESSAGES)]
    check("I18N-01", "訊息表：zh/en 同鍵同佔位符同識別字同 emoji、en 純 ASCII（emoji 除外）、狀態前綴一致、模板可 format",
          not probs and len(la.MESSAGES) >= 117 and len(ci.MESSAGES) >= 21,
          f"lit_api={len(la.MESSAGES)} cite={len(ci.MESSAGES)} 問題 {len(probs)}：{probs[:4]}")


def test_i18n_callsites():
    """I18N-02：呼叫端契約（AST）——鍵是字面值且在表裡、kwargs 等於佔位符、沒有沒人用的鍵、import 時不呼叫、
    表外沒有殘留中文字串（設計文件附錄 A 的掃描器）。"""
    ci = _ci()
    probs = [("lit_api", p) for p in _callsite_problems(os.path.join(SKILL, "scripts", "lit_api.py"), la.MESSAGES)] + \
            [("cite_integrity", p) for p in _callsite_problems(os.path.join(SKILL, "scripts", "cite_integrity.py"), ci.MESSAGES)]
    check("I18N-02", "呼叫端契約：鍵為字面值且存在、kwargs＝佔位符、無未用鍵、無 import 時呼叫、表外無中文字串",
          not probs, f"問題 {len(probs)}：{probs[:4]}")


def test_i18n_zh_frozen():
    """I18N-03：中文一個字都沒變——(1) 改動前掃描器列出的每個中文字串片段都還在 zh 表裡（evals/fixtures/zh_literals.json
    是改動前那棵樹的快照；只有刻意改某段中文時才在同一個 commit 裡同步更新，例如兩條被引紅旗改成照實說年份）；
    (1b) 逐鍵金樣 evals/fixtures/zh_messages.json（{腳本: {鍵: zh}}）：金樣裡每把鍵都在表裡、zh 一字不差，
    表裡每把鍵也都在金樣裡。片段檢查只問「這段字還在某處」，同一句話在兩把鍵各有一份時改掉其中一把照樣過；逐鍵比對補這個洞。
    新增一把鍵卻沒同步更新金樣會在這裡大聲失敗——這是刻意的：中文輸出的任何增減，都要有人在同一個 commit 裡明確寫進金樣；
    (2) LIT_LANG 未設／zh／zh-TW／ZH 的 stdout 位元組相同、stderr 為空。"""
    ci = _ci()
    golden = json.load(open(fixture("zh_literals.json"), encoding="utf-8"))
    missing = []
    for fname, table in (("lit_api.py", la.MESSAGES), ("cite_integrity.py", ci.MESSAGES)):
        blob = "\n".join(v["zh"] for v in table.values())
        for frag in golden[fname]:
            for piece in re.split(r"%[sd]", frag):
                # 「；另有來源查詢失敗（」那一條：分隔符依 R6 拆成 sep.clause，片段本身只差開頭的「；」
                if piece and piece not in blob and piece.strip("；") not in blob:
                    missing.append((fname, piece[:30]))
    n_frag = sum(len(v) for k, v in golden.items() if not k.startswith("_"))
    # (1b) 逐鍵：兩個方向都比。金樣是空的或少了某支腳本，表裡每把鍵都會被列成「金樣裡沒有」，不會悄悄變成零比對。
    per_key = json.load(open(fixture("zh_messages.json"), encoding="utf-8"))
    key_bad = []
    for fname, table in (("lit_api.py", la.MESSAGES), ("cite_integrity.py", ci.MESSAGES)):
        gold = per_key.get(fname) or {}
        key_bad += [(fname, k, "zh 與金樣不同") for k, z in gold.items() if k in table and table[k].get("zh") != z]
        key_bad += [(fname, k, "表裡沒有") for k in gold if k not in table]
        key_bad += [(fname, k, "金樣裡沒有") for k in table if k not in gold]
    n_keys = sum(len(v) for v in per_key.values() if isinstance(v, dict))
    check("I18N-03a", f"改動前的 {n_frag} 個中文字串片段全部仍在 zh 表裡（金樣：fixtures/zh_literals.json），"
          f"且 {n_keys} 把鍵的 zh 逐鍵與 fixtures/zh_messages.json 一字不差、兩邊鍵集合相同",
          n_frag >= 150 and not missing and not key_bad,
          f"片段缺 {len(missing)}：{missing[:4]}；逐鍵不符 {len(key_bad)}：{key_bad[:4]}")

    # 子行程的 HOME／USERPROFILE 與 cwd 都指向空的暫存目錄（同 I18N-09）：開發者 ~/.env 或 ./.env 裡的
    # LIT_LANG=en 會讓「未設」那一格讀成英文，這條就因為與程式無關的理由變紅。
    import shutil
    iso = tempfile.mkdtemp(prefix="i18n-zh-")
    outs = {}
    try:
        home, cwd = os.path.join(iso, "home"), os.path.join(iso, "cwd")
        os.makedirs(home)
        os.makedirs(cwd)
        for val in (None, "zh", "zh-TW", "ZH"):
            env = {"LIT_LANG": val, "HOME": home, "USERPROFILE": home}
            c1, o1, e1 = run_cli("lit_api.py", "verify", "--title", "   ", env=env, cwd=cwd)
            c2, o2, e2 = run_cli("cite_integrity.py", fixture("apa_style.md"), "--json", env=env, cwd=cwd)
            outs[val] = (c1, o1, e1, c2, o2, e2)
    finally:
        shutil.rmtree(iso, ignore_errors=True)
    same = len({v for v in outs.values()}) == 1
    quiet = all(not v[2] and not v[5] for v in outs.values())
    check("I18N-03b", "LIT_LANG 未設／zh／zh-TW／ZH：stdout 位元組相同、離開碼相同、stderr 為空",
          same and quiet, f"相同={same} 無警告={quiet}")


def test_i18n_english_cli():
    """I18N-04：LIT_LANG=en 時主要指令的離線路徑輸出英文——訊息欄位等於 _T 在 en 下的值，而且整份輸出不含 CJK
    （輸入含中文的兩個案例只看訊息欄位，query 回顯的是使用者資料）。"""
    ci = _ci()
    en = {"LIT_LANG": "en"}

    def js(out):
        s = out.strip()
        return json.loads(s) if s.startswith(("{", "[")) else {}

    bad = []
    c, o, _ = run_cli("lit_api.py", "verify", "--title", "   ", env=en)
    d = js(o)
    if not (c == 1 and d.get("message") == _t(la, "en", "verify.empty_title") and not CJK_RE.search(o)):
        bad.append(("verify.blank", o[:80]))
    c, o, _ = run_cli("lit_api.py", "verify", "--title", "智慧型維修決策支援系統之研究", env=en)
    d = js(o)
    if not (c == 1 and d.get("verdict_hint") == "unsupported_title"
            and d.get("absence_note") == _t(la, "en", "verify.unsupported_title")):
        bad.append(("verify.cjk", str(d.get("absence_note"))[:80]))
    c, o, _ = run_cli("lit_api.py", "versions", "隨便什麼", env=en)
    d = js(o)
    if not (c == 1 and d.get("error") == "UNSUPPORTED_IDENTIFIER"
            and d.get("recommendation") == _t(la, "en", "versions.unsupported_identifier")):
        bad.append(("versions.cjk", str(d.get("recommendation"))[:80]))
    c, o, _ = run_cli("lit_api.py", "brief", fixture("not_json.txt"), env=en)
    d = js(o)
    if not (c == 1 and d.get("error") == "INVALID_INPUT"
            and str(d.get("message")).startswith(_t(la, "en", "load.bad_json", err="")) and not CJK_RE.search(o)):
        bad.append(("brief.not_json", o[:80]))
    c, o, _ = run_cli("lit_api.py", "pick", fixture("two_works.json"), "99", env=en)
    d = js(o)
    if not (c == 1 and d.get("message") == _t(la, "en", "common.index_out_of_range", bad=[99]) and not CJK_RE.search(o)):
        bad.append(("pick.oob", o[:80]))
    c, o, _ = run_cli("lit_api.py", "export-xml", "[", env=en)
    d = js(o)
    if not (c == 1 and str(d.get("message")).startswith(_t(la, "en", "export_xml.bad_inline_json", err=""))
            and not CJK_RE.search(o)):
        bad.append(("export_xml.bad", o[:80]))
    c, o, _ = run_cli("lit_api.py", "export-xml", "[null]", env=en)
    d = js(o)
    if not (c == 1 and d.get("message") == _t(la, "en", "export_xml.nothing_to_export") and not CJK_RE.search(o)):
        bad.append(("export_xml.null", o[:80]))
    tmp = tempfile.mkdtemp(prefix="i18n-")
    try:
        p = os.path.join(tmp, "empty.json")
        open(p, "w").write("[]")
        c, o, _ = run_cli("lit_api.py", "verify-batch", p, env=en)
        d = js(o)
        if not (c == 1 and d.get("message") == _t(la, "en", "batch.need_array") and not CJK_RE.search(o)):
            bad.append(("verify-batch.empty", o[:80]))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # cite_integrity：--json 與文字模式
    c, o, _ = run_cli("cite_integrity.py", fixture("apa_style.md"), "--json", env=en)
    d = js(o)
    if not (c == 1 and d.get("unsupported_style_note") == _t(ci, "en", "note.no_numeric_apa", apa=2) and not CJK_RE.search(o)):
        bad.append(("cite.apa.json", o[:80]))
    c, o, _ = run_cli("cite_integrity.py", fixture("unnumbered_list.md"), "--json", env=en)
    d = js(o)
    if not (c == 1 and d.get("unsupported_style_note") == _t(ci, "en", "note.unparsable_list", n=3) and not CJK_RE.search(o)):
        bad.append(("cite.unnumbered.json", o[:80]))
    f = fixture("overcite.md")
    c, o, _ = run_cli("cite_integrity.py", f, env=en)
    lines = o.splitlines()
    want = [_t(ci, "en", "report.file", file=f),
            _t(ci, "en", "report.counts", cited=3, listed=10),
            _t(ci, "en", "report.cited_not_listed", items=[100]),
            _t(ci, "en", "report.listed_not_cited", items=[3, 4, 5, 6, 7, 8, 9, 10]),
            _t(ci, "en", "report.dup_numbers", items=_t(ci, "en", "report.none")),
            _t(ci, "en", "report.gaps", items=_t(ci, "en", "report.none")),
            _t(ci, "en", "report.verdict", verdict=_t(ci, "en", "report.verdict.problems"))]
    if not (c == 1 and lines == want and not CJK_RE.search(o)):
        bad.append(("cite.overcite.text", lines[:3]))
    f = fixture("heading_in_title.md")
    c, o, _ = run_cli("cite_integrity.py", f, env=en)
    if not (c == 0 and o.splitlines()[-1] == _t(ci, "en", "report.verdict", verdict=_t(ci, "en", "report.verdict.pass"))
            and not CJK_RE.search(o)):
        bad.append(("cite.pass.text", o[-80:]))
    f = fixture("fake.docx")
    c, o, _ = run_cli("cite_integrity.py", f, "--json", env=en)
    d = js(o)
    if not (c == 2 and d.get("message") == _t(ci, "en", "err.not_docx", path=f) and not CJK_RE.search(o)):
        bad.append(("cite.fake.json", o[:80]))
    c, o, _ = run_cli("cite_integrity.py", f, env=en)
    if not (c == 2 and o.strip() == "❌ " + _t(ci, "en", "err.not_docx", path=f)):
        bad.append(("cite.fake.text", o[:80]))
    c, o, _ = run_cli("cite_integrity.py", fixture("no_such_file.docx"), "--json", env=en)
    if not (c == 2 and js(o).get("error") == "INVALID_INPUT" and not CJK_RE.search(o)):
        bad.append(("cite.missing", o[:80]))
    check("I18N-04", "LIT_LANG=en：verify/versions/brief/pick/export-xml/verify-batch 與 cite_integrity（文字＋JSON）的離線輸出"
          "全為英文，訊息欄位等於 _T 在 en 下的值，整份輸出不含 CJK",
          not bad, f"不符 {len(bad)}：{bad[:3]}")


def test_i18n_english_stubbed():
    """I18N-05／06：需要假 HTTP 的路徑——versions、verify_one、decide_verdict、retract、fulltext、429 指引——在 en 下
    訊息欄位等於 _T 的值且不含 CJK（05）；同一情境 zh 與 en 除了訊息欄位以外完全相同、離開碼相同、每個 status 的
    開頭 emoji 相同（06）。不連網：http_get 一律換成替身。"""
    import types
    import urllib.error
    spec = importlib.util.spec_from_file_location("la_i18n", os.path.join(SKILL, "scripts", "lit_api.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.load_dotenv = lambda: None
    real_arxiv_query = m.arxiv_query

    def http_err(url, code):
        return urllib.error.HTTPError(url, code, "fixture", {}, None)

    def no_net(*a, **k):
        raise AssertionError("測試不得連網：http_get 被呼叫 %r" % (a[:1],))

    def en(key, **kw):
        return _t(m, "en", key, **kw)

    bad5, bad6 = [], []

    def pair(name, zh_r, en_r, extra=(), expect=None):
        """zh_r/en_r = (exit, json, exc, raw)。expect：en 下必須成立的 (欄位路徑→值) 清單。"""
        (cz, dz, xz, rz), (ce, de, xe, re_) = zh_r, en_r
        if xz or xe or dz is None or de is None:
            bad6.append((name, f"exc zh={xz} en={xe}"))
            return
        if cz != ce or _strip_msgs(dz, extra) != _strip_msgs(de, extra):
            bad6.append((name, "訊息欄位以外有差異或離開碼不同"))
        sz, se = _statuses(dz), _statuses(de)
        if [EMOJI_RE.match(s).group() if EMOJI_RE.match(s) else None for s in sz] != \
           [EMOJI_RE.match(s).group() if EMOJI_RE.match(s) else None for s in se]:
            bad6.append((name, "status 的開頭 emoji 不同"))
        if CJK_RE.search(re_):
            bad5.append((name, "en 輸出含 CJK"))
        for path, val in (expect or []):
            cur = de
            for k in path:
                cur = cur[k] if isinstance(cur, (dict, list)) and (isinstance(k, int) or k in cur) else None
            if cur != val:
                bad5.append((name, f"{path}: {str(cur)[:60]!r} != {str(val)[:60]!r}"))

    # ---- versions ----
    title = "Neural Widget Alignment: A Contrastive Approach to Cross-Domain Widget Matching"
    pre = {"title": title, "year": 2024, "authors": ["Alice Chen", "Bob Okafor"], "doi": None}
    w_msg = {"title": ["Contrastive Cross-Domain Matching of Neural Widgets"], "DOI": "10.1000/widget.2025.7",
             "type": "journal-article", "container-title": ["Journal of Widget Research"],
             "issued": {"date-parts": [[2025]]},
             "author": [{"family": "Chen", "given": "Alice"}, {"family": "Okafor", "given": "Bob"}]}

    def vrun(ident, s2=None, arxiv=(), by_title=(), works=None, works_404=(), crossref_down=False, arxiv_down=False):
        def fake_http_get(url, *a, **k):
            if "api.crossref.org/works/" in url:
                if crossref_down:
                    raise urllib.error.URLError("simulated crossref timeout")
                for doi in works_404:
                    if url.endswith("/works/" + doi):
                        raise http_err(url, 404)
                for doi, msg in (works or {}).items():
                    if url.endswith("/works/" + doi):
                        return json.dumps({"message": msg})
                raise http_err(url, 404)
            return no_net(url)

        def fake_s2(pid):
            if s2 is None:
                raise http_err("https://api.semanticscholar.org/graph/v1/paper/" + pid, 404)
            return dict(s2)

        def fake_arxiv(*a, **k):
            if arxiv_down:
                raise urllib.error.URLError("simulated arxiv timeout")
            return [dict(e) for e in arxiv]

        m.http_get, m.s2_get_paper, m.arxiv_query = fake_http_get, fake_s2, fake_arxiv
        m.crossref_search = lambda t, rows=5, author=None: [] if author else [dict(c) for c in by_title]
        return _both(m.cmd_versions, types.SimpleNamespace(id=ident))

    zh_r, en_r = vrun("ARXIV:2410.01234", arxiv=[pre])
    pair("versions.not_found_title_author", zh_r, en_r, expect=[
        (("s2_note",), en("versions.s2_404_note")), (("recommendation",), en("versions.rec.not_found_title_author"))])
    zh_r, en_r = vrun("ARXIV:2410.01234", arxiv=[dict(pre, authors=[])])
    pair("versions.not_found_title", zh_r, en_r, expect=[(("recommendation",), en("versions.rec.not_found_title"))])
    zh_r, en_r = vrun("ARXIV:2410.01234", arxiv=[])
    pair("versions.arxiv_id_not_found", zh_r, en_r, expect=[
        (("error",), "ARXIV_ID_NOT_FOUND"), (("recommendation",), en("versions.rec.arxiv_id_not_found"))])
    if en_r[0] != 1:
        bad5.append(("versions.arxiv_id_not_found", f"exit={en_r[0]}"))
    zh_r, en_r = vrun("ARXIV:2410.01234", s2={"title": None, "year": 2024, "authors": [], "doi": None, "venue": None}, arxiv=[])
    pair("versions.no_title", zh_r, en_r, expect=[(("recommendation",), en("versions.rec.no_title"))])
    zh_r, en_r = vrun("DOI:10.1000/widget.2025.7", works={"10.1000/widget.2025.7": w_msg})
    pv = (en_r[1] or {}).get("published_version") or {}
    os.environ["LIT_LANG"] = "en"
    try:
        desc_en = m._describe(pv)
    finally:
        os.environ["LIT_LANG"] = "zh"
    pair("versions.input_doi_is_published", zh_r, en_r, expect=[
        (("version_link",), "input_doi_is_published"),
        (("recommendation",), en("versions.rec.input_is_published", desc=desc_en))])
    zh_r, en_r = vrun("ARXIV:2410.01234", arxiv=[dict(pre, doi="10.1000/missing.1")], works_404=["10.1000/missing.1"])
    pair("versions.jdoi_not_found", zh_r, en_r, expect=[
        (("arxiv_record", "journal_doi_status"), en("versions.jdoi.not_found")),
        (("recommendation",), en("versions.rec.not_found_title_author") + en("versions.rec.suffix_jdoi_unused"))])
    pub = {"source": "crossref", "title": title, "year": 2025, "doi": "10.1000/exact", "type": "journal-article",
           "container": "J", "authors": ["Chen, Alice", "Okafor, Bob"]}
    zh_r, en_r = vrun("ARXIV:2410.01234", s2=dict(pre, venue=None), arxiv_down=True, by_title=[pub])
    pair("versions.published_arxiv_down", zh_r, en_r, expect=[
        (("confidence",), "medium"),
        (("confidence_note",), en("versions.note.title_inferred") + en("sep.clause")
         + en("versions.note.errors_suffix", errs="arxiv_error"))])
    zh_r, en_r = vrun("DOI:10.1000/some.paper", crossref_down=True)
    pair("versions.incomplete", zh_r, en_r, expect=[(("recommendation",), en("versions.rec.incomplete"))])
    if en_r[0] != 1:
        bad5.append(("versions.incomplete", f"exit={en_r[0]}"))

    # ---- verify_one（含逾時）與 decide_verdict／derivative_signals ----
    s2_match = json.dumps({"data": [{"paperId": "fixture", "title": "Attention Is All You Need", "year": 2017,
                                     "authors": [{"name": "Ashish Vaswani"}], "venue": "NIPS",
                                     "externalIds": {"DOI": "10.5555/3295222"}}]})
    s2_empty = json.dumps({"data": []})

    def vone(crossref, s2_payload, title="Attention Is All You Need"):
        def cs(*a, **k):
            if isinstance(crossref, Exception):
                raise crossref
            return [dict(c) for c in crossref]

        def hg(url, *a, **k):
            if isinstance(s2_payload, Exception):
                raise s2_payload
            return s2_payload
        m.crossref_search, m.http_get = cs, hg
        return _both(m.cmd_verify, types.SimpleNamespace(title=title, authors="Vaswani, Ashish", year=2017))

    zh_r, en_r = vone(OSError("simulated crossref outage"), s2_match)
    pair("verify.single_source", zh_r, en_r, expect=[
        (("single_source_degraded",), en("verify.single_source", providers="crossref_error"))])
    zh_r, en_r = vone(OSError("simulated crossref outage"), OSError("simulated s2 outage"))
    pair("verify.partial_failure", zh_r, en_r, expect=[
        (("verdict_hint",), "partial_failure"),
        (("absence_note",), en("verify.partial_failure", providers="crossref_error, s2_error"))])
    zh_r, en_r = vone([], s2_empty)
    pair("verify.not_found", zh_r, en_r, expect=[(("verdict_hint",), "not_found"), (("absence_note",), en("verify.not_found"))])
    real_worst = m.HTTP_WORST_CASE
    try:
        m.HTTP_WORST_CASE = -10            # join 預算 0 秒：兩條供應者執行緒都還在睡，逾時路徑必走

        def slow(*a, **k):
            time.sleep(0.3)
            return []
        m.crossref_search, m.http_get = slow, lambda *a, **k: (time.sleep(0.3), s2_empty)[1]
        zh_r, en_r = _both(m.cmd_verify, types.SimpleNamespace(title="Attention Is All You Need",
                                                                 authors="Vaswani, Ashish", year=2017))
        time.sleep(0.8)                    # 讓替身執行緒跑完再往下，免得它們之後才寫進字典
    finally:
        m.HTTP_WORST_CASE = real_worst
    pair("verify.timeout", zh_r, en_r, expect=[(("crossref_error",), en("verify.timeout", sec=0))])

    os.environ["LIT_LANG"] = "en"
    try:
        rev = {"title": "The Making of the English Working Class", "year": 1963,
               "authors": ["H. McQueen", "E. P. Thompson"], "type": "journal-article", "page": "75"}
        c = dict(rev, match=m.score_candidate(rev, rev["title"], ["Thompson"], 1961))
        _, b = m.decide_verdict([c], year_supplied=True, authors_supplied=True, query_authors=["Thompson"])
        reasons = (b or {}).get("downgrade_reasons") or []
        want = [en("verdict.year_gap", yd=2), en("deriv.short_pages", span=1, page="75"),
                en("deriv.first_author_other", first="H. McQueen")]
        noauth = {"title": "Editorial", "year": None, "authors": []}
        c2 = dict(noauth, match=m.score_candidate(noauth, "Editorial", ["Smith"], 2024))
        _, b2 = m.decide_verdict([c2], year_supplied=True, authors_supplied=True)
        want2 = [en("verdict.cand_no_authors"), en("verdict.cand_no_year")]
        p1 = {"title": "Editorial", "year": 2020, "authors": ["A. One"], "doi": "10.1/a"}
        p2 = {"title": "Editorial", "year": 2020, "authors": ["B. Two"], "doi": "10.2/b"}
        cs_ = [dict(p, match=m.score_candidate(p, "Editorial", ["Wang"], 2020)) for p in (p1, p2)]
        _, b3 = m.decide_verdict(m.rank_candidates(cs_), year_supplied=True, authors_supplied=True, query_authors=["Wang"])
        want3 = [en("verdict.no_author_overlap"), en("verdict.ambiguous", gap=0.0)]
        want4 = en("deriv.crossref_type", type="book-review")
        got4 = m.derivative_signals({"type": "book-review", "page": "1-20", "authors": []})
    finally:
        os.environ["LIT_LANG"] = "zh"
    if reasons != want:
        bad5.append(("verdict.reasons", reasons))
    if (b2 or {}).get("downgrade_reasons") != want2:
        bad5.append(("verdict.no_data", (b2 or {}).get("downgrade_reasons")))
    if (b3 or {}).get("downgrade_reasons") != want3:
        bad5.append(("verdict.ambiguous", (b3 or {}).get("downgrade_reasons")))
    if got4 != [want4]:
        bad5.append(("deriv.crossref_type", got4))

    # ---- retract：六種狀態 + coverage_caveat + _note ----
    doi = "10.1056/nejm200011233432103"

    def rhttp(updates_items, meta_title, cand_items, fail_fallback=False, fail_all=False):
        def f(url, *a, **k):
            if fail_all:
                raise OSError("simulated crossref outage")
            if "filter=updates" in url:
                return json.dumps({"message": {"items": updates_items}})
            if "/works/" in url:
                if fail_fallback:
                    raise OSError("simulated fallback outage")
                return json.dumps({"message": {"title": [meta_title]}})
            if "query.bibliographic" in url:
                return json.dumps({"message": {"items": cand_items}})
            return no_net(url)
        return f

    def rrun(*a, **k):
        m.http_get = rhttp(*a, **k)
        return _both(m.cmd_retract, types.SimpleNamespace(dois=[doi]))

    t0 = "Comparison of Upper Gastrointestinal Toxicity"
    zh_r, en_r = rrun([], t0, [])
    pair("retract.clean", zh_r, en_r, expect=[((0, "status"), en("retract.status.clean")),
                                             ((0, "coverage_caveat"), en("retract.coverage_caveat")),
                                             ((1, "_note"), en("retract.note"))])
    upd = [{"update-to": [{"DOI": doi, "type": "retraction", "label": "Retraction",
                           "updated": {"date-parts": [[2020, 1, 1]]}}], "DOI": "10.1/n", "title": ["Retraction: X"]}]
    zh_r, en_r = rrun(upd, t0, [])
    pair("retract.retracted", zh_r, en_r, expect=[((0, "status"), en("retract.status.retracted"))])
    corr = [{"update-to": [{"DOI": doi, "type": "correction", "label": "Correction",
                            "updated": {"date-parts": [[2020, 1, 1]]}}], "DOI": "10.1/n", "title": ["Correction: X"]}]
    zh_r, en_r = rrun(corr, t0, [])
    pair("retract.corrected", zh_r, en_r, expect=[((0, "status"), en("retract.status.corrected"))])
    zh_r, en_r = rrun([], t0, [{"title": ["Expression of Concern: " + t0], "DOI": "10.1/eoc",
                                "issued": {"date-parts": [[2005, 12, 8]]}}])
    pair("retract.suspected", zh_r, en_r, expect=[((0, "status"), en("retract.status.suspected")),
                                                 ((0, "notices", 0, "label"), en("retract.label.title_search"))])
    zh_r, en_r = rrun([], t0, [], fail_fallback=True)
    pair("retract.incomplete", zh_r, en_r, expect=[((0, "status"), en("retract.status.incomplete"))])
    if en_r[0] != 1 or zh_r[0] != 1:
        bad6.append(("retract.incomplete", f"exit zh={zh_r[0]} en={en_r[0]}（❓ 狀態應回非零）"))
    zh_r, en_r = rrun([], t0, [], fail_all=True)
    pair("retract.failed", zh_r, en_r, extra=("error",), expect=[((0, "status"), en("retract.status.failed"))])
    if en_r[0] != 1 or zh_r[0] != 1:
        bad6.append(("retract.failed", f"exit zh={zh_r[0]} en={en_r[0]}"))

    # ---- fulltext：三種狀態 + how_to_get + _note + 無 mailto 的 note ----
    saved_mailto = os.environ.pop("CROSSREF_MAILTO", None)
    try:
        def frun(s2_pdf=None, s2_fail=False, unpaywall=None):
            def sg(pid):
                if s2_fail:
                    raise OSError("simulated s2 outage")
                return {"openAccessPdf": s2_pdf}
            m.s2_get_paper = sg
            m.http_get = (lambda url, *a, **k: json.dumps(unpaywall)) if unpaywall is not None else no_net
            return _both(m.cmd_fulltext, types.SimpleNamespace(dois=["10.1/x"]))

        zh_r, en_r = frun()
        pair("fulltext.none", zh_r, en_r, expect=[
            ((0, "note"), en("fulltext.need_mailto")), ((0, "status"), en("fulltext.status.none")),
            ((0, "how_to_get"), [en("fulltext.how.publisher", doi="10.1/x"), en("fulltext.how.library"), en("fulltext.how.ill")]),
            ((1, "_note"), en("fulltext.note"))])
        zh_r, en_r = frun(s2_pdf="https://example.org/x.pdf")
        pair("fulltext.ok", zh_r, en_r, expect=[((0, "status"), en("fulltext.status.ok"))])
        zh_r, en_r = frun(s2_fail=True)
        pair("fulltext.failed", zh_r, en_r, extra=("error",), expect=[
            ((0, "status"), en("fulltext.status.failed")),
            ((0, "error"), en("fulltext.s2_oa_failed", err="simulated s2 outage"))])
        os.environ["CROSSREF_MAILTO"] = "test@example.org"
        zh_r, en_r = frun(s2_pdf="https://repo.example.org/x.pdf",
                          unpaywall={"is_oa": False, "oa_status": "closed", "oa_locations": []})
        pair("fulltext.discrepancy", zh_r, en_r, expect=[((0, "source_discrepancy"), en("fulltext.source_discrepancy"))])
    finally:
        os.environ.pop("CROSSREF_MAILTO", None)
        if saved_mailto is not None:
            os.environ["CROSSREF_MAILTO"] = saved_mailto

    # ---- search／arxiv 備援、snowball、verify-batch 混形狀 ----
    def s2_429(url, *a, **k):
        if "semanticscholar" in url:
            raise http_err(url, 429)
        return json.dumps({"results": [{"title": "W", "publication_year": 2020, "authorships": [], "doi": None,
                                        "cited_by_count": 0, "primary_location": {"source": {
                                            "display_name": "J", "type": "journal", "is_in_doaj": False, "is_core": False}}}]})
    real_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        m.http_get = s2_429
        zh_r, en_r = _both(m.cmd_search, types.SimpleNamespace(query="widgets", limit=1, year=None))
    finally:
        time.sleep = real_sleep
    pair("search.fallback", zh_r, en_r, expect=[
        (("note",), en("search.fallback_note")),
        (("results", 0, "quality_warnings"), [en("quality.no_doi"), en("quality.zero_cites"), en("quality.venue_unindexed")])])
    m.arxiv_query = real_arxiv_query       # 上面的 versions 情境換掉了它；這裡要讓真的 arxiv_query 去撞假的 http_get
    m.http_get = lambda url, *a, **k: (_ for _ in ()).throw(OSError("down")) if "arxiv.org" in url else json.dumps({"data": []})
    zh_r, en_r = _both(m.cmd_arxiv, types.SimpleNamespace(query="widgets", limit=1))
    pair("arxiv.fallback", zh_r, en_r, expect=[(("note",), en("arxiv.fallback_note", err="down"))])
    m.http_get = lambda url, *a, **k: (_ for _ in ()).throw(http_err(url, 404))
    zh_r, en_r = _both(m.cmd_snowball, types.SimpleNamespace(id="S2:abc", direction="references", limit=5))
    pair("snowball.not_found", zh_r, en_r, expect=[(("references", "hint"), en("snowball.not_found_hint"))])
    tmp = tempfile.mkdtemp(prefix="i18n-batch-")
    try:
        p = os.path.join(tmp, "mixed.json")
        open(p, "w").write('[{"title": "x"}, "just a string"]')
        m.verify_one = lambda t, a, y: {"query": {"title": t}, "verdict_hint": "not_found"}
        zh_r, en_r = _both(m.cmd_verify_batch, types.SimpleNamespace(file=p, workers=1))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    pair("batch.mixed", zh_r, en_r, extra=("error",), expect=[
        (("results", 1, "error"), en("batch.entry_not_object", n=2, type="str"))])

    # ---- 429 指引（CX-20a 的英文孿生）與上游無法解析 ----
    import contextlib
    import io
    import urllib.request
    real_open, real_sleep, real_argv = urllib.request.urlopen, time.sleep, sys.argv

    def drive(argv, urlopen, lang):
        spec2 = importlib.util.spec_from_file_location("la_i18n_429", os.path.join(SKILL, "scripts", "lit_api.py"))
        mm = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(mm)
        mm.load_dotenv = lambda: None
        err, out, code = io.StringIO(), io.StringIO(), 0
        os.environ["LIT_LANG"] = lang
        urllib.request.urlopen, time.sleep, sys.argv = urlopen, lambda s: None, ["lit_api.py"] + argv
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                try:
                    mm.main()
                except SystemExit as e:
                    code = e.code
        finally:
            urllib.request.urlopen, time.sleep, sys.argv = real_open, real_sleep, real_argv
            os.environ["LIT_LANG"] = "zh"
        return code, out.getvalue(), err.getvalue()

    saved_key = os.environ.pop("S2_API_KEY", None)
    try:
        def always_429(req, timeout=None):
            raise http_err(req.full_url, 429)
        cz, oz, ez = drive(["search", "widgets"], always_429, "zh")
        ce, oe, ee = drive(["search", "widgets"], always_429, "en")
        page, env_line = "https://www.semanticscholar.org/product/api", "S2_API_KEY=<your key>"
        hz, he = json.loads(oz).get("hint", ""), json.loads(oe).get("hint", "")
        ok429 = (cz == ce == 1 and page in ee and env_line in ee and "~/.env" in ee and not CJK_RE.search(ee)
                 and ee.count(page) == 1 and he == en("cli.hint_429", hint=en("s2.key_hint", url=page))
                 and "60-120" in he and not CJK_RE.search(oe)
                 and _strip_msgs(json.loads(oz)) == _strip_msgs(json.loads(oe)))
        if not ok429:
            bad5.append(("hint_429", (ee[:60], he[:60])))
        ce2, oe2, _ = drive(["crossref-doi", "10.1000/x"], always_429, "en")
        he2 = json.loads(oe2).get("hint", "")
        if not (ce2 == 1 and he2 == en("cli.hint_429_other", host="api.crossref.org") and not CJK_RE.search(oe2)):
            bad5.append(("hint_429_other", he2[:60]))

        class Html:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"<html>maintenance</html>"
        ce3, oe3, _ = drive(["paper", "DOI:10.1/x"], lambda req, timeout=None: Html(), "en")
        d3 = json.loads(oe3)
        if not (ce3 == 1 and d3.get("error") == "UPSTREAM_UNPARSEABLE" and d3.get("note") == en("cli.unparseable_note")
                and str(d3.get("message")).startswith(en("cli.unparseable_msg", err="")) and not CJK_RE.search(oe3)):
            bad5.append(("unparseable", oe3[:80]))
    finally:
        if saved_key is not None:
            os.environ["S2_API_KEY"] = saved_key

    check("I18N-05", "LIT_LANG=en：versions／verify_one（含單源、未完成、查無、逾時）／降級理由／retract 六狀態／fulltext 三狀態／"
          "備援 note／429 指引／上游無法解析，訊息欄位等於 _T 在 en 下的值、輸出不含 CJK",
          not bad5, f"不符 {len(bad5)}：{bad5[:6]}")
    check("I18N-06", "語言只改訊息欄位：同一情境 zh 與 en 去掉訊息欄位後 JSON 相同、離開碼相同、status 開頭 emoji 相同",
          not bad6, f"不符 {len(bad6)}：{bad6[:6]}")


def test_i18n_resolution():
    """I18N-07：LIT_LANG 的解析矩陣——大小寫、地區後綴、空白都認；空值＝未設（不警告）；不認得的值退回 zh、
    stdout 與 zh 完全相同、stderr 恰好一行 ASCII 警告、離開碼不變；同一行程只警告一次。"""
    import contextlib
    import io
    i18n = la._i18n
    table = {"": "zh", None: "zh", "zh": "zh", "zh-TW": "zh", "zh_tw": "zh", "ZH": "zh", "zh-Hant": "zh",
             "en": "en", "EN": "en", "en-US": "en", "en_GB": "en", " en ": "en"}

    def norm(v):
        # 解析器拋例外也要算成「這一條錯」而不是讓整個套件函式炸掉：否則失敗會記在套件名下，
        # 突變測試就對不到 I18N-07（第一次跑突變時就是這樣漏抓的）。
        try:
            return i18n.normalize(v)
        except Exception as e:
            return f"raised {type(e).__name__}"
    wrong = [(v, norm(v)) for v, want in table.items() if norm(v) != (want, None)]
    wrong += [(v, norm(v)) for v in ("fr", "klingon", "english", "1", "zhen") if norm(v) != ("zh", v)]
    i18n._warned.clear()
    err = io.StringIO()
    os.environ["LIT_LANG"] = "fr"
    try:
        with contextlib.redirect_stderr(err):
            try:
                langs = [i18n.get_lang() for _ in range(3)]
            except Exception as e:
                langs = [f"raised {type(e).__name__}"]
    finally:
        os.environ["LIT_LANG"] = "zh"
        i18n._warned.clear()
    lines = err.getvalue().splitlines()
    once = (langs == ["zh"] * 3 and len(lines) == 1 and "LIT_LANG" in lines[0] and "zh" in lines[0]
            and all(ord(ch) < 128 for ch in lines[0]))

    cz, oz, ez = run_cli("lit_api.py", "verify", "--title", "   ", env={"LIT_LANG": "zh"})
    cb, ob, eb = run_cli("lit_api.py", "verify", "--title", "   ", env={"LIT_LANG": "fr"})
    cz2, oz2, _ = run_cli("cite_integrity.py", fixture("apa_style.md"), env={"LIT_LANG": "zh"})
    cb2, ob2, eb2 = run_cli("cite_integrity.py", fixture("apa_style.md"), env={"LIT_LANG": "klingon"})
    sub_ok = (cz == cb == 1 and oz == ob and not ez and len(eb.splitlines()) == 1 and "LIT_LANG" in eb
              and all(ord(ch) < 128 for ch in eb)
              and cz2 == cb2 and oz2 == ob2 and len(eb2.splitlines()) == 1 and "LIT_LANG" in eb2
              and all(ord(ch) < 128 for ch in eb2))
    check("I18N-07", "LIT_LANG 解析：別名／大小寫／地區後綴／空白都認、空值視為未設；不認得的值退回 zh、stdout 同 zh、"
          "stderr 恰一行 ASCII 警告、離開碼不變、每個行程只警告一次（兩支腳本）",
          not wrong and once and sub_ok,
          f"矩陣錯誤={wrong[:3]} 只警告一次={once}（{lines[:2]}） 子行程={sub_ok}（stderr={eb[:60]!r} / {eb2[:60]!r}）")


def test_i18n_flag():
    """I18N-08：--lang 旗標——勝過環境變數、=X 寫法、子命令前後都認、無效值退回 zh 並警告（不是 argparse 錯誤）、
    頂層 -h 列出它（子命令 -h 不變）；分開寫的空值（--lang ""）等於沒給旗標，環境變數的 LIT_LANG=en 照樣生效；
    cite_integrity 同樣。"""
    en_msg, zh_msg = _t(la, "en", "verify.empty_title"), _t(la, "zh", "verify.empty_title")

    def msg(*args, env):
        c, o, e = run_cli("lit_api.py", *args, env=env)
        try:
            return c, json.loads(o).get("message"), e
        except ValueError:
            return c, o[:60], e
    r1 = msg("--lang", "en", "verify", "--title", "   ", env={"LIT_LANG": "zh"})
    r2 = msg("--lang", "zh", "verify", "--title", "   ", env={"LIT_LANG": "en"})
    r3 = msg("--lang=en", "verify", "--title", "   ", env={"LIT_LANG": None})
    r4 = msg("verify", "--title", "   ", "--lang", "en", env={"LIT_LANG": None})
    r5 = msg("verify", "--title", "   ", "--lang=en", env={"LIT_LANG": "zh"})
    r6 = msg("--lang", "fr", "verify", "--title", "   ", env={"LIT_LANG": None})
    # 分開寫的空值：pop_lang_arg 照樣吃掉兩個 argv，但不寫 LIT_LANG（寫進空字串會把環境變數的 en 蓋成 zh）
    r7 = msg("--lang", "", "verify", "--title", "   ", env={"LIT_LANG": "en"})
    flag_ok = (r1[:2] == (1, en_msg) and r2[:2] == (1, zh_msg) and r3[:2] == (1, en_msg) and r4[:2] == (1, en_msg)
               and r5[:2] == (1, en_msg) and r6[:2] == (1, zh_msg) and "LIT_LANG" in r6[2]
               and r7[:2] == (1, en_msg)
               and "usage" not in r6[2] and all(not r[2] for r in (r1, r2, r3, r4, r5, r7)))
    ch, oh, _ = run_cli("lit_api.py", "-h", env={"LIT_LANG": "zh"})
    cs, os_, _ = run_cli("lit_api.py", "verify", "-h", env={"LIT_LANG": "zh"})
    help_ok = ch == 0 and "--lang" in oh and cs == 0 and "--lang" not in os_
    ci = _ci()
    en_note = _t(ci, "en", "note.no_numeric_apa", apa=2)
    c1, o1, _ = run_cli("cite_integrity.py", fixture("apa_style.md"), "--json", "--lang", "en", env={"LIT_LANG": "zh"})
    c2, o2, _ = run_cli("cite_integrity.py", "--lang=en", fixture("apa_style.md"), "--json", env={"LIT_LANG": "zh"})
    c3, o3, _ = run_cli("cite_integrity.py", "-h", env={"LIT_LANG": "zh"})
    c4, o4, e4 = run_cli("cite_integrity.py", "--lang", "", fixture("apa_style.md"), "--json", env={"LIT_LANG": "en"})
    try:
        note4 = json.loads(o4).get("unsupported_style_note")
    except ValueError:
        note4 = o4[:60]
    ci_ok = (c1 == 1 and json.loads(o1).get("unsupported_style_note") == en_note
             and c2 == 1 and json.loads(o2).get("unsupported_style_note") == en_note
             and c3 == 0 and "--lang" in o3
             and c4 == 1 and note4 == en_note and not e4)
    check("I18N-08", "--lang：勝過 LIT_LANG、--lang=X、子命令前後皆可、無效值退回 zh 並警告而非 argparse 錯誤、"
          "頂層 -h 列出而子命令 -h 不變、--lang \"\"（分開寫的空值）等於沒給旗標；cite_integrity 同樣",
          flag_ok and help_ok and ci_ok,
          f"旗標={flag_ok}（{[r[:2] for r in (r1, r2, r3, r4, r5, r6, r7)]}） 說明={help_ok} cite={ci_ok}"
          f"（--lang \"\"：{c4} {str(note4)[:30]!r}）")


def test_i18n_dotenv():
    """I18N-09：.env——cwd 的 .env 寫 LIT_LANG=en 對兩支腳本都生效（BOM、引號、註解都吃）；真的環境變數勝過 .env；
    cwd 沒有就退到 ~/.env；空值 LIT_LANG= 視為未設。子行程的 HOME／USERPROFILE 指向空的暫存目錄，開發者真正的 ~/.env 插不進來。
    空值規則只限 LIT_LANG：其他鍵維持「已設就不覆蓋」——殼層的 S2_API_KEY="" 不被 cwd .env 的值補上
    （行程內呼叫 la.load_dotenv，cwd 與 HOME／USERPROFILE 都指向暫存目錄，跑完還原）。
    文件範例也是閘門：四份文件（README／USAGE，中英）裡含 S2_API_KEY= 與 LIT_LANG= 的 .env 範例區塊原樣載入。
    行內註解比照 python-dotenv：沒有引號的值從第一個「空白 + #」起是註解（LIT_LANG=en   # c → en、S2_API_KEY=abc # x → abc）；
    成對引號包住的值原樣保留，含 #（CROSSREF_MAILTO="a@b.c # keep" → a@b.c # keep）。以前不剝，LIT_LANG 變成 "en   # c"。"""
    import shutil
    en_msg, zh_msg = _t(la, "en", "verify.empty_title"), _t(la, "zh", "verify.empty_title")
    ci = _ci()
    en_note = _t(ci, "en", "note.no_numeric_apa", apa=2)
    root = tempfile.mkdtemp(prefix="i18n-env-")
    try:
        cwd_env, home_a, home_b, home_c, cwd_empty = [os.path.join(root, n) for n in ("cwd", "home_a", "home_b", "home_c", "empty")]
        for d in (cwd_env, home_a, home_b, home_c, cwd_empty):
            os.makedirs(d)
        with open(os.path.join(cwd_env, ".env"), "w", encoding="utf-8-sig") as f:
            f.write("# comment line\n\nS2_API_KEY = 'not-a-real-key'\nLIT_LANG = \"en\"\n")
        with open(os.path.join(home_b, ".env"), "w", encoding="utf-8") as f:
            f.write("LIT_LANG=en\n")
        with open(os.path.join(home_c, ".env"), "w", encoding="utf-8") as f:
            f.write("LIT_LANG=\n")

        def env(home, lit=None):
            return {"LIT_LANG": lit, "HOME": home, "USERPROFILE": home, "S2_API_KEY": None}

        def msg(o):
            try:
                return json.loads(o).get("message")
            except ValueError:
                return o[:60]

        def note(o):
            try:
                return json.loads(o).get("unsupported_style_note")
            except ValueError:
                return o[:60]
        c1, o1, e1 = run_cli("lit_api.py", "verify", "--title", "   ", env=env(home_a), cwd=cwd_env)
        c2, o2, _ = run_cli("cite_integrity.py", fixture("apa_style.md"), "--json", env=env(home_a), cwd=cwd_env)
        c3, o3, _ = run_cli("lit_api.py", "verify", "--title", "   ", env=env(home_a, "zh"), cwd=cwd_env)
        c4, o4, _ = run_cli("lit_api.py", "verify", "--title", "   ", env=env(home_b), cwd=cwd_empty)
        c5, o5, _ = run_cli("cite_integrity.py", fixture("apa_style.md"), "--json", env=env(home_b), cwd=cwd_empty)
        c6, o6, e6 = run_cli("lit_api.py", "verify", "--title", "   ", env=env(home_c), cwd=cwd_empty)
        c7, o7, _ = run_cli("lit_api.py", "verify", "--title", "   ", env=env(home_a), cwd=cwd_empty)
        # 空值等於未設：殼層的 LIT_LANG=""（POSIX 的 export LIT_LANG=）擋不住 cwd 的 .env；
        # --lang= 空值等於沒給旗標，環境變數的 LIT_LANG=en 照樣生效。兩支腳本都要。
        c8, o8, e8 = run_cli("lit_api.py", "verify", "--title", "   ", env=env(home_a, ""), cwd=cwd_env)
        c9, o9, _ = run_cli("cite_integrity.py", fixture("apa_style.md"), "--json", env=env(home_a, ""), cwd=cwd_env)
        c10, o10, e10 = run_cli("lit_api.py", "--lang=", "verify", "--title", "   ", env=env(home_a, "en"),
                                cwd=cwd_empty)
        c11, o11, _ = run_cli("cite_integrity.py", "--lang=", fixture("apa_style.md"), "--json", env=env(home_a, "en"),
                              cwd=cwd_empty)
        empty_ok = (msg(o8) == en_msg and not e8 and note(o9) == en_note and msg(o10) == en_msg and not e10
                    and note(o11) == en_note and c8 == c10 == 1 and c9 == c11 == 1)
        # 空值規則只限 LIT_LANG：其他鍵維持「已設就不覆蓋」（與 b0db1e0 相同）。殼層 S2_API_KEY="" 算已設，
        # cwd .env 的 S2_API_KEY 不得補進來；殼層沒設的 CROSSREF_MAILTO 照樣從同一份 .env 讀進來——
        # 證明 .env 真的讀過，空字串不是因為 load_dotenv 空轉才留著。行程內呼叫，cwd 與 HOME 都隔離、跑完還原。
        pin_cwd = os.path.join(root, "pin")
        os.makedirs(pin_cwd)
        with open(os.path.join(pin_cwd, ".env"), "w", encoding="utf-8") as f:
            f.write("S2_API_KEY=fromdotenv\nCROSSREF_MAILTO=fromdotenv\n")
        saved = {k: os.environ.get(k) for k in ("S2_API_KEY", "CROSSREF_MAILTO", "HOME", "USERPROFILE")}
        old_cwd = os.getcwd()
        try:
            os.environ.update({"S2_API_KEY": "", "HOME": home_a, "USERPROFILE": home_a})
            os.environ.pop("CROSSREF_MAILTO", None)
            os.chdir(pin_cwd)
            la.load_dotenv()
            pin = (os.environ.get("S2_API_KEY"), os.environ.get("CROSSREF_MAILTO"))
        except Exception as e:
            pin = f"raised {type(e).__name__}: {e}"
        finally:
            os.chdir(old_cwd)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        pin_ok = pin == ("", "fromdotenv")
        # 文件範例即閘門：四份文件各自含 S2_API_KEY= 與 LIT_LANG= 的 ``` 區塊，原樣寫成 .env 載入：LIT_LANG 要是 en，
        # 金鑰與信箱不得缺、不得帶 #（行內註解會被剝掉，但緊貼在值後面、前面沒有空白的 # 仍算值的一部分）。
        # 行程內呼叫，cwd 與 HOME／USERPROFILE 都指向空的暫存目錄，LIT_LANG／S2_API_KEY／CROSSREF_MAILTO 先移除，跑完還原。
        doc_home = os.path.join(root, "doc_home")
        os.makedirs(doc_home)

        def load_doc_env(doc_cwd):
            keys = ("LIT_LANG", "S2_API_KEY", "CROSSREF_MAILTO", "HOME", "USERPROFILE")
            saved_env = {k: os.environ.get(k) for k in keys}
            cwd_before = os.getcwd()
            try:
                for k in ("LIT_LANG", "S2_API_KEY", "CROSSREF_MAILTO"):
                    os.environ.pop(k, None)
                os.environ.update({"HOME": doc_home, "USERPROFILE": doc_home})
                os.chdir(doc_cwd)
                la.load_dotenv()
                return {k: os.environ.get(k) for k in ("LIT_LANG", "S2_API_KEY", "CROSSREF_MAILTO")}
            except Exception as e:
                return f"raised {type(e).__name__}: {e}"
            finally:
                os.chdir(cwd_before)
                for k, v in saved_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

        doc_res = {}
        for doc in ("README.md", "README.zh-TW.md", "USAGE.md", "USAGE.zh-TW.md"):
            with open(os.path.join(SKILL, doc), "rb") as f:
                doc_lines = f.read().decode("utf-8").replace("\r\n", "\n").split("\n")
            blocks, cur = [], None
            for ln in doc_lines:
                if ln.startswith("```"):
                    if cur is None:
                        cur = []
                    else:
                        blocks.append("\n".join(cur))
                        cur = None
                elif cur is not None:
                    cur.append(ln)
            hits = [b for b in blocks if "S2_API_KEY=" in b and "LIT_LANG=" in b]
            if len(hits) != 1:
                doc_res[doc] = f"含兩個鍵的 ``` 區塊有 {len(hits)} 個（應為 1）"
                continue
            doc_cwd = os.path.join(root, "doc_" + doc.replace(".", "_"))
            os.makedirs(doc_cwd)
            with open(os.path.join(doc_cwd, ".env"), "w", encoding="utf-8") as f:
                f.write(hits[0] + "\n")
            got = load_doc_env(doc_cwd)
            if not isinstance(got, dict):
                doc_res[doc] = got
            elif got["LIT_LANG"] != "en":
                doc_res[doc] = f"LIT_LANG={got['LIT_LANG']!r}（應為 'en'）"
            elif any(got[k] is None or "#" in got[k] for k in ("S2_API_KEY", "CROSSREF_MAILTO")):
                doc_res[doc] = (f"S2_API_KEY={got['S2_API_KEY']!r} CROSSREF_MAILTO={got['CROSSREF_MAILTO']!r}"
                                "（不得缺、不得含 #）")
            else:
                doc_res[doc] = "ok"
        doc_ok = len(doc_res) == 4 and all(v == "ok" for v in doc_res.values())
        doc_detail = "；".join(f"{d}:{r}" for d, r in doc_res.items())
        # 行內註解（比照 python-dotenv）：沒有引號的值從第一個「空白 + #」起是註解；成對引號包住的值原樣保留（含 #）。
        # 以前整段都當成值：LIT_LANG 讀成 "en   # c"（不認得的語言，輸出悄悄退回中文），金鑰帶著註解送出去。
        inline_cwd = os.path.join(root, "inline")
        os.makedirs(inline_cwd)
        with open(os.path.join(inline_cwd, ".env"), "w", encoding="utf-8") as f:
            f.write('LIT_LANG=en   # c\nS2_API_KEY=abc # x\nCROSSREF_MAILTO="a@b.c # keep"\n')
        inline = load_doc_env(inline_cwd)
        inline_ok = inline == {"LIT_LANG": "en", "S2_API_KEY": "abc", "CROSSREF_MAILTO": "a@b.c # keep"}
        ok = (msg(o1) == en_msg and not e1 and json.loads(o2).get("unsupported_style_note") == en_note
              and msg(o3) == zh_msg and msg(o4) == en_msg
              and json.loads(o5).get("unsupported_style_note") == en_note
              and msg(o6) == zh_msg and not e6 and msg(o7) == zh_msg
              and c1 == c3 == c4 == c6 == c7 == 1 and c2 == c5 == 1)
        check("I18N-09", ".env：cwd 的 LIT_LANG=en（BOM／引號／註解）對 lit_api 與 cite_integrity 都生效；環境變數勝過 .env；"
              "cwd 沒有就退到 ~/.env；LIT_LANG= 空值視為未設且不警告；都沒有就是 zh；"
              "殼層的 LIT_LANG=\"\" 擋不住 .env、--lang= 等於沒給旗標（兩支腳本）；空值規則只限 LIT_LANG："
              "殼層的 S2_API_KEY=\"\" 仍算已設、不被 .env 補上；"
              "四份文件的 .env 範例原樣載入：LIT_LANG=en 生效、金鑰與信箱不帶 #；"
              "行內註解比照 python-dotenv：LIT_LANG=en   # c → en、S2_API_KEY=abc # x → abc、引號裡的 # 原樣保留",
              ok and empty_ok and pin_ok and doc_ok and inline_ok,
              f"cwd.env={msg(o1)!r}/{str(json.loads(o2).get('unsupported_style_note'))[:20]!r} env>file={msg(o3)!r} "
              f"home={msg(o4)!r} empty={msg(o6)!r}（stderr={e6[:40]!r}） none={msg(o7)!r} "
              f"空值不擋={empty_ok}（{msg(o8)!r}/{str(note(o9))[:20]!r}/{msg(o10)!r}/{str(note(o11))[:20]!r}）"
              f" 其他鍵不補={pin_ok}（{pin!r}）"
              f" 文件範例={doc_ok}（{doc_detail}）"
              f" 行內註解={inline_ok}（{inline!r}）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_i18n_help():
    """I18N-10：LIT_LANG=en 時頂層與 15 個子命令的 -h、以及 cite_integrity 的 -h 都離開碼 0、非空、不含 CJK、
    而且真的是英文說明（含該子命令的 en help 字串）。在行程內跑 main()，不開子行程。"""
    import contextlib
    import io
    spec = importlib.util.spec_from_file_location("la_i18n_help", os.path.join(SKILL, "scripts", "lit_api.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.load_dotenv = lambda: None
    ci = _ci()
    ci.load_dotenv = lambda: None
    # 子命令自己的 -h 只印它的參數說明（不印頂層列表裡那句 help=），所以對照的是參數的那把鍵
    cmds = {"search": "help.search.year", "arxiv": None, "snowball": None, "verify": "help.verify.authors",
            "verify-batch": "help.verify_batch.workers", "paper": None, "batch": "help.batch.ids",
            "export-xml": "help.export_xml.file", "versions": "help.versions.id", "fulltext": "help.fulltext.dois",
            "retract": "help.retract.dois", "brief": None, "pick": None, "crossref-doi": None, "export": None}

    def squash(s):
        return " ".join(s.split())      # argparse 依終端寬度換行，比對時把空白壓平

    def helptext(mod, argv):
        out, err, code = io.StringIO(), io.StringIO(), None
        real_argv = sys.argv
        sys.argv = ["x.py"] + argv
        os.environ["LIT_LANG"] = "en"
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    mod.main()
                except SystemExit as e:
                    code = e.code
        finally:
            sys.argv = real_argv
            os.environ["LIT_LANG"] = "zh"
        return code, out.getvalue(), err.getvalue()

    bad = []
    code, out, err = helptext(m, ["-h"])
    if not (code == 0 and out and not CJK_RE.search(out) and "--lang" in out
            and squash(_t(m, "en", "help.verify")) in squash(out) and "Sub-commands" in out):
        bad.append(("top", (code, out[:60], err[:60])))
    for c, key in cmds.items():
        code, out, err = helptext(m, [c, "-h"])
        want = squash(_t(m, "en", key)) if key else ""
        if not (code == 0 and out and not CJK_RE.search(out) and (want in squash(out))):
            bad.append((c, (code, out[:60], err[:60])))
    code, out, err = helptext(ci, ["-h"])
    if not (code == 0 and out and not CJK_RE.search(out) and squash(_t(ci, "en", "help.lang")) in squash(out)):
        bad.append(("cite", (code, out[:60], err[:60])))
    check("I18N-10", "LIT_LANG=en：頂層＋15 個子命令＋cite_integrity 的 -h 都離開碼 0、不含 CJK、含英文說明、沒有 % 展開錯誤",
          not bad, f"不符 {len(bad)}：{bad[:3]}")


def test_i18n_translator():
    """I18N-11：_T 永不拋例外——未知鍵回可見標記、缺 kwarg／多 kwarg／值含 {}% 都安全、en 缺項退回 zh、
    _H 把 % 加倍、語言每次呼叫時讀（同一行程改 os.environ 就翻）。"""
    i18n = la._i18n
    T = i18n.make_translator({"k": {"zh": "中 {a}", "en": "E {a}"}, "z": {"zh": "只有中文"},
                              "lit": {"zh": "字面 {x} 100%", "en": "lit {x} 100%"}})
    os.environ["LIT_LANG"] = "en"
    try:
        r = [T("nope"), T("k"), T("k", a=1, b=2), T("k", a="{x} %s {"), T("z"), T("lit"), T("lit", x=1)]
    finally:
        os.environ["LIT_LANG"] = "zh"
    safe = r == ["[nope]", "E {a}", "E 1", "E {x} %s {", "只有中文", "lit {x} 100%", "lit 1 100%"]
    la.MESSAGES["_test.pct"] = {"zh": "100% 中", "en": "100% en"}
    try:
        h = la._H("_test.pct")
    finally:
        del la.MESSAGES["_test.pct"]
    flip = []
    for lang in ("zh", "en", "zh"):
        os.environ["LIT_LANG"] = lang
        flip.append(la._T("verify.empty_title"))
    os.environ["LIT_LANG"] = "zh"
    per_call = flip == [la.MESSAGES["verify.empty_title"]["zh"], la.MESSAGES["verify.empty_title"]["en"],
                        la.MESSAGES["verify.empty_title"]["zh"]]

    # 宿主行程已經有別的 _i18n 模組（誘餌：沒有 load_dotenv）時，兩支腳本照樣載得起來、共用同一份機制，
    # 而且載入不改 sys.path。以前是 sys.path.insert + import _i18n：拿到誘餌（ImportError），還改了宿主的搜尋路徑。
    # 例外一律收成這一條的失敗，不讓它炸成套件層級的失敗（突變測試要對得到 I18N-11）。
    import types
    scripts_dir = os.path.normcase(os.path.abspath(os.path.join(SKILL, "scripts")))
    saved_path = list(sys.path)
    saved_mods = {n: sys.modules.get(n) for n in ("_i18n", "lit_review_i18n")}
    try:
        sys.modules["_i18n"] = types.ModuleType("_i18n")
        sys.modules.pop("lit_review_i18n", None)       # 走一次「宿主從沒載過本機制」的那條路
        sys.path[:] = [p for p in saved_path if os.path.normcase(os.path.abspath(p or os.curdir)) != scripts_dir]
        before = list(sys.path)
        try:
            la2 = load_module("la_i18n_decoy", os.path.join(SKILL, "scripts", "lit_api.py"))
            ci2 = load_module("ci_i18n_decoy", os.path.join(SKILL, "scripts", "cite_integrity.py"))
            decoy_ok = (la2._i18n is ci2._i18n and la2._i18n is not sys.modules["_i18n"] and sys.path == before
                        and callable(la2.load_dotenv) and callable(ci2.load_dotenv)
                        and _t(la2, "en", "verify.empty_title") == la.MESSAGES["verify.empty_title"]["en"]
                        and _t(ci2, "en", "report.none") == ci2.MESSAGES["report.none"]["en"])
            why = f"共用={la2._i18n is ci2._i18n} sys.path 不變={sys.path == before}"
        except Exception as e:
            decoy_ok, why = False, f"{type(e).__name__}: {e}"
    finally:
        sys.path[:] = saved_path
        for n, mod in saved_mods.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod
    try:
        shared = la._i18n is _ci()._i18n
    except Exception as e:
        shared = f"{type(e).__name__}: {e}"
    check("I18N-11", "_T 安全：未知鍵→[key]、缺／多 kwarg 與含 {}% 的值不拋、en 缺項退回 zh、無參數模板原樣回傳；"
          "_H 加倍 %；語言每次呼叫時讀；宿主已有別的 _i18n 模組時兩支腳本照樣載入、共用同一份機制、不改 sys.path",
          safe and h == "100%% 中" and per_call and decoy_ok and shared is True,
          f"safe={safe}（{r}） _H={h!r} per_call={per_call} 誘餌={decoy_ok}（{why}） la/ci 共用={shared}")


def _zip_check_required():
    """evals/zip_check.py 自己的 REQUIRED。它是腳本、import 就會執行，所以用 AST 讀常值；讀不到回空 tuple，
    用它做的 stub ZIP 就缺檔，相關案例會紅，不會靜默變成什麼都沒比。"""
    import ast
    tree = ast.parse(open(os.path.join(BASE, "zip_check.py"), encoding="utf-8").read())
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "REQUIRED" for t in n.targets):
            return tuple(ast.literal_eval(n.value))
    return ()


def _packaging_negative(tmp):
    """I18N-12 的反向檢查，回 (ok, detail)：整棵樹複製到暫存目錄、另建一個 git repo，把 scripts/_i18n.py 從索引拿掉
    （檔案仍在磁碟上，正是「寫了卻沒 git add」的樣子），package_skill.py 必須 exit 1、不留 ZIP，而且由它自己點名缺的檔。
    只看離開碼不夠：zip_check.py 也有同一份清單，打包器自己的檢查壞了，zip_check 會在後面補位擋下，
    離開碼照樣是 1——分不出是哪一層擋的。git 不能用就算失敗：沒跑成的檢查不是通過。"""
    import shutil
    tree = os.path.join(tmp, "neg-tree")
    shutil.copytree(SKILL, tree, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", "*.zip", "*.mutbak", ".env", ".env.*", "*.env"))
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")}
    env["PYTHONIOENCODING"] = "utf-8"

    def git(*a):
        return subprocess.run(["git", "-C", tree, *a], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, env=env)
    try:
        steps = [git("init", "-q"), git("add", "-A"), git("rm", "--cached", "-q", "scripts/_i18n.py")]
        listed = git("ls-files").stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"git 不能用，反向檢查沒跑成：{type(e).__name__}"
    if any(s.returncode != 0 for s in steps) or "scripts/_i18n.py" in listed or "scripts/lit_api.py" not in listed:
        return False, f"暫存 git repo 不是預期的樣子：{[(s.returncode, s.stderr[-60:]) for s in steps]}"
    zp = os.path.join(tmp, "neg.zip")
    r = subprocess.run([PY, os.path.join(tree, "scripts", "package_skill.py"), "-o", zp], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120, cwd=tree, env=env)
    own = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("❌ ZIP 缺少必要檔案")]
    left = os.path.exists(zp)
    ok = r.returncode == 1 and not left and len(own) == 1 and "scripts/_i18n.py" in own[0]
    return ok, f"exit={r.returncode} 留下ZIP={left} 打包器自己點名={own[:1]!r}"


def test_i18n_packaging():
    """I18N-12：打包後的 ZIP 跑得動英文模式——package_skill.py 只打包 git ls-files，_i18n.py 沒進 git 就會在
    使用者機器上以 FileNotFoundError 收場；本地測試全綠也看不出來。反向：必要檔不在 git 索引裡時，
    package_skill.py 自己拒絕（exit 1、不留 ZIP、點名缺檔）；zip_check.py 對缺必要檔的 ZIP 也回 exit 1。"""
    import shutil
    import zipfile
    tmp = tempfile.mkdtemp(prefix="i18n-pkg-")
    try:
        zp = os.path.join(tmp, "lit-review.zip")
        r = subprocess.run([PY, os.path.join(SKILL, "scripts", "package_skill.py"), "-o", zp], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120, cwd=SKILL,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        packed = r.returncode == 0 and os.path.isfile(zp)
        names = zipfile.ZipFile(zp).namelist() if packed else []
        has_i18n = "lit-review/scripts/_i18n.py" in names
        # I18N-03a 讀這份金樣；它沒進 git 就不在 ZIP 裡（package_skill.py 只打包 git ls-files）
        has_golden = ("lit-review/evals/fixtures/zh_literals.json" in names
                      and "lit-review/evals/fixtures/zh_messages.json" in names)
        xdir = os.path.join(tmp, "x")
        if packed:
            zipfile.ZipFile(zp).extractall(xdir)
        env = dict(os.environ, LIT_LANG="en", PYTHONIOENCODING="utf-8")
        la_p = os.path.join(xdir, "lit-review", "scripts", "lit_api.py")
        ci_p = os.path.join(xdir, "lit-review", "scripts", "cite_integrity.py")
        runs = []
        for cmd in ([PY, la_p, "-h"], [PY, ci_p, "-h"], [PY, la_p, "verify", "--title", "   "]):
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env, cwd=tmp)
            runs.append((p.returncode, p.stdout, p.stderr))
        ok = (packed and has_i18n and has_golden and runs[0][0] == 0 and runs[1][0] == 0 and runs[2][0] == 1
              and not any(CJK_RE.search(o) for _, o, _ in runs) and not any("Traceback" in e for _, _, e in runs)
              and json.loads(runs[2][1]).get("message") == _t(la, "en", "verify.empty_title"))
        neg_ok, neg_detail = _packaging_negative(tmp)
        # zip_check 那一份清單（給不經打包器、手動壓縮的 ZIP）：必要檔都在→過；只少 _i18n.py→exit 1 並列「缺必要檔」。
        # 用 zip_check 自己的清單造 ZIP，不用打包器的：兩半各測各的，打包器那半壞了不會被這半順手抓到而掩蓋。
        zc_req = _zip_check_required()
        zc_codes = []
        for nm, skip in (("zc_full.zip", None), ("zc_no_i18n.zip", "scripts/_i18n.py")):
            zpath = os.path.join(tmp, nm)
            with zipfile.ZipFile(zpath, "w") as z:
                for req in zc_req:
                    if req != skip:
                        z.writestr("lit-review/" + req, "stub\n")
            p = subprocess.run([PY, os.path.join(BASE, "zip_check.py"), zpath], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            # 看風險項那一行（「缺必要檔（1 處）」），不是摘要行「缺必要檔： 無」
            zc_codes.append((p.returncode, "缺必要檔（" in (p.stdout or "")))
        zc_ok = "scripts/_i18n.py" in zc_req and zc_codes == [(0, False), (1, True)]
        check("I18N-12", "package_skill.py 產出的 ZIP 含 scripts/_i18n.py 與 evals/fixtures/zh_literals.json／zh_messages.json，"
              "解壓後 lit_api.py／cite_integrity.py 在 LIT_LANG=en 下跑得動且輸出英文；_i18n.py 不在 git 索引時"
              "打包器自己拒絕（exit 1、不留 ZIP、點名缺檔），zip_check 對缺必要檔的 ZIP 回 exit 1",
              ok and neg_ok and zc_ok,
              f"打包={packed}（{(r.stdout or r.stderr)[-80:]!r}） 含_i18n={has_i18n} 含金樣={has_golden} "
              f"解壓後={[(c, e[-60:]) for c, _, e in runs]} 反向={neg_ok}（{neg_detail}） zip_check={zc_codes}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    # 釘住語言：既有案例全部斷言中文輸出。開發者殼層、cwd 或 ~/.env 裡的 LIT_LANG=en 會讓二十幾條案例
    # 因為與程式無關的理由變紅。語言是每次呼叫時讀的，所以在這裡釘就夠（load_module 不在 import 時取字）。
    os.environ["LIT_LANG"] = "zh"
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-k", "--filter", default="")
    args = ap.parse_args()

    suites = [test_derivative_items, test_doi_variants_and_s2_entities,
              test_empty_author_candidate, test_single_source_degradation,
              test_vancouver_and_list_guard, test_retraction_fallback,
              test_title_similarity, test_author_overlap, test_identity_gate,
              test_crash_paths, test_absence_semantics, test_integrity_parsing,
              test_rate_limiter_concurrency, test_verify_one_shared_logic,
              test_entity_decoding, test_chinese_punctuation,
              test_fulltext_boundary, test_output_hardening,
              test_versions_s2_absent, test_versions_published_lookup,
              test_versions_review_fixes, test_versions_round3, test_hygiene_cx20,
              test_i18n_tables, test_i18n_callsites, test_i18n_zh_frozen, test_i18n_english_cli,
              test_i18n_english_stubbed, test_i18n_resolution, test_i18n_flag, test_i18n_dotenv,
              test_i18n_help, test_i18n_translator, test_i18n_packaging]
    selected = [s for s in suites if args.filter.lower() in s.__name__.lower()]
    if not selected:
        # 打錯字的 -k 以前一個套件都沒跑、只剩 NET-01，卻印「1/1 passed」、離開碼 0——看起來像驗證過了。
        print(f"-k {args.filter!r} 沒有對到任何套件；可用的套件：{' '.join(s.__name__ for s in suites)}",
              file=sys.stderr)
        sys.exit(2)
    # 封網閘門。本套件宣稱「不連網」，但這句話以前只寫在 README：MED-05/06 一直真的在連
    # Semantic Scholar，直到有人手動封網重跑才看出來——一次性的檢查抓得到這一次，抓不到下一次。
    # 所以寫成閘門：本行程的對外連線一律攔下並記錄，跑完只要有一筆，NET-01 就失敗。
    # （run_cli 起的子行程不在攔截範圍：它們只跑輸入驗證、本機檔案解析這類送出請求前就結束的路徑。）
    import socket
    attempts = []

    def _blocked(*a, **k):
        attempts.append(repr(a[1] if len(a) > 1 and isinstance(a[0], socket.socket) else a[:1]))
        raise OSError("test_regression 封網：測試不得連網")

    socket.socket.connect = _blocked
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked

    for s in selected:
        try:
            s()
        except Exception as e:  # 測試本身壞掉也要如實顯示，不可靜默跳過
            check(s.__name__, f"測試套件自身異常：{type(e).__name__}", False, str(e)[:200])
    check("NET-01", "整個套件執行期間沒有任何對外連線（封網閘門）",
          not attempts, f"{len(attempts)} 次連線嘗試：{attempts[:3]}")

    # -k 只決定「跑哪些套件」，不再過濾結果。以前結果還會依 id／描述再篩一次，於是
    # `-k versions` 跑了 test_versions_published_lookup，但 CX-18d 的描述裡沒有「versions」，
    # 它失敗了也不會顯示、離開碼仍是 0——查證時用 -k 的人會被騙。
    shown = RESULTS
    failed = [r for r in shown if not r["ok"]]
    for r in shown:
        if args.verbose or not r["ok"]:
            mark = "PASS" if r["ok"] else "FAIL"
            print(f"[{mark}] {r['id']:10s} {r['desc']}")
            if r["detail"] and (args.verbose or not r["ok"]):
                print(f"         {r['detail']}")
    print(f"\n{len(shown) - len(failed)}/{len(shown)} passed"
          + (f" — {len(failed)} FAILED" if failed else " — all green"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
