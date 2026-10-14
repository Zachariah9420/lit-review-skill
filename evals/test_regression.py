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


def run_cli(script, *args, stdin_json=None):
    """跑 CLI，回 (exit_code, stdout, stderr)。"""
    p = subprocess.run([PY, os.path.join(SKILL, "scripts", script), *[str(a) for a in args]],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=60)
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
    check("PH-05", "標題搜尋發現的通知標為「未經關聯確認」而非直接紅色警示",
          "未經 updates 關聯確認" in fn)
    check("PH-06", "無記錄時附年代覆蓋警語（綠勾在舊文獻沒有證據力）",
          "coverage_caveat" in fn and "2010" in fn)


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
    check("FT-01", "fulltext 明確聲明不繞過付費牆", "不繞過付費牆" in fn)
    check("FT-02", "查無免費版時提供機構取得途徑", "how_to_get" in fn and "圖書館" in fn)
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


def main():
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
              test_versions_review_fixes, test_versions_round3, test_hygiene_cx20]
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

    for s in suites:
        if args.filter and args.filter.lower() not in s.__name__.lower():
            continue
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
