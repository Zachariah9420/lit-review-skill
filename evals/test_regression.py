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
    #（純字串檢查，不打 API：驗證 regex 本身)
    import re
    is_doi = lambda s: bool(s.upper().startswith("DOI:") or re.match(r"^10\.\d{4,9}/", s))
    check("CX-11", "含斜線的標題不被誤判為 DOI",
          not is_doi("Risk/Benefit Analysis in Medicine") and is_doi("10.1234/abc")
          and is_doi("doi:10.1234/abc"))


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
    # CX-15:RIS 欄位值內的換行會截斷記錄 → 必須清理
    poison = [{"title": "Legit Title\nER  - \nTY  - JOUR\nTI  - Injected",
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
    finally:
        os.unlink(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-k", "--filter", default="")
    args = ap.parse_args()

    suites = [test_title_similarity, test_author_overlap, test_identity_gate,
              test_crash_paths, test_absence_semantics, test_integrity_parsing,
              test_entity_decoding, test_chinese_punctuation,
              test_fulltext_boundary, test_output_hardening]
    for s in suites:
        if args.filter and args.filter.lower() not in s.__name__.lower():
            continue
        try:
            s()
        except Exception as e:  # 測試本身壞掉也要如實顯示，不可靜默跳過
            check(s.__name__, f"測試套件自身異常：{type(e).__name__}", False, str(e)[:200])

    shown = [r for r in RESULTS if not args.filter or args.filter.lower() in r["id"].lower()
             or args.filter.lower() in r["desc"].lower()] if args.filter else RESULTS
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
