# -*- coding: utf-8 -*-
"""突變測試：把已修的缺陷塞回去，確認迴歸測試真的抓得到。

一個永遠全綠的測試套件可能只是沒有偵測力。這支腳本反向驗證：
每個突變都**必須**讓至少一個指定案例失敗，否則該防護等於沒有測試保護。

執行：python evals/mutation_check.py

突變寫在**整棵樹的暫存複本**上，原始 repo 全程唯讀。以前是就地改 scripts/
底下那支正式檔案、再靠 finally 複原：只要中途被 Ctrl-C、當掉或斷電，留在磁碟上
的就是一份被刻意改壞的正式程式,外加一個 .mutbak。一支用來建立信心的工具，
不該把「不要在跑到一半時關掉」也算進它的前提。
"""
import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(BASE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# (描述， 目標檔， 原始片段， 突變片段， 必須因此失敗的案例)
MUTATIONS = [
    # 這條原本突變的是 norm_title 裡那個 [^a-z0-9{CJK}]+ 的字元類。那個字元類本身
    # 就是一個 bug(它只保了 CJK,把希臘、西里爾、韓文、阿拉伯文的字母全刪掉,於是
    # TNF-α 與 TNF-β 塌縮成同一個字串),已改成 isalnum()。突變因此要跟著改指向新
    # 的實作——否則「目標片段不存在」會讓這條防護變成沒有人證明過的防護。
    ("只保留 ASCII 字母 → 非拉丁標題塌縮", "lit_api.py",
     'kept = [ch if ch.isalnum() else " " for ch in (t or "").lower()]',
     'kept = [ch if (ch.isascii() and ch.isalnum()) else " " for ch in (t or "").lower()]',
     ["CX-01"]),
    ("移除空字串防護 → 空對空 = 1.0", "lit_api.py",
     "    if not na or not nb:\n        return 0.0\n", "",
     ["TS-F11a"]),
    ("移除陣列根層處理 → brief 崩潰", "lit_api.py",
     '    if isinstance(d, list):          # pick 的輸出本身就是陣列\n        return {}, d\n', "",
     ["CX-05"]),
    ("year_diff 缺值當成 0 → 無年份候選蒙混過關", "lit_api.py",
     'yd = m.get("year_diff")           # None = 任一方無年份，「未知」不等於 0',
     'yd = m.get("year_diff") or 0',
     ["CX-03"]),
    ("排序只看 title_sim → 真論文被無關候選壓過", "lit_api.py",
     'c["match"].get("author_overlap") or 0.0,',
     '0,',
     ["TS-C2"]),
    ("移除作者零重疊的否決 → 同名不同作者被判 found", "lit_api.py",
     'reasons.append("使用者提供的作者無一出現在候選作者中")',
     'pass',
     ["TS-C1"]),
    ("docx 不解 XML 字元參照 → en-dash 範圍引用抓不到", "cite_integrity.py",
     "        return html.unescape(\n", "        return (\n",
     ["CX-14"]),
    ("引用範圍上限回到 50 → [1-52] 靜默丟棄", "cite_integrity.py",
     "if a <= b and b - a <= 300:      # 300 足以涵蓋任何真實論文的引用範圍",
     "if a <= b and b - a <= 50:",
     ["CX-12"]),
    ("參考文獻標題改回寬鬆比對 → 被條目內的 References 騙走切分點", "cite_integrity.py",
     "    for m in HEADING_RE.finditer(text):\n        split_at = m.start()",
     "    for m in HEADING_LOOSE_RE.finditer(text):\n        split_at = m.start()",
     ["CX-13"]),
    # CX-18（2026-10-13）：versions 在 S2 查無 arXiv 記錄時崩潰。兩道防護各一個突變。
    # （2026-10-14 cmd_versions 改寫後，第一條的目標改指向新的 s2doi 那一行。）
    ("S2 查無時不從 arXiv 回填標題 → 版本鏈無從推定（原 s2_record=None 崩潰的那條路）", "lit_api.py",
     '                title = title or e0.get("title")',
     '                title = title',
     ["CX-18"]),
    ("S2 404 記回 s2_error → 「查無記錄」被說成「查詢失敗請重試」並回非零", "lit_api.py",
     'out["s2_note"] = ("Semantic Scholar 查無此 arXiv 記錄',
     'out["s2_error"] = ("Semantic Scholar 查無此 arXiv 記錄',
     ["CX-18", "CX-18b"]),
    # CX-18d～g（2026-10-14）：驗證 CX-18 修正時發現的 D，以及使用者回饋的「正式版改了標題」。
    ("拿掉存在性檢查 → 兩個來源都查無的編號被說成「查無正式版」並回 0", "lit_api.py",
     'elif confirmed and out["arxiv"]:',
     'elif out["arxiv"]:',
     ["CX-18d"]),
    ("關掉作者搜尋 → 改了標題才發表的正式版找不到", "lit_api.py",
     'if title and named and not out["published_version"]:',
     'if False:',
     ["CX-18e", "CX-18g"]),
    ("忽略 arXiv 上作者自填的期刊 DOI → 作者已標明的正式版被漏掉", "lit_api.py",
     'if jdoi and not jdoi.lower().startswith("10.48550"):',
     'if False:',
     ["CX-18f"]),
    ("查無正式版時改回「引 arXiv 版合理」 → 很多期刊不收的預印本被背書", "lit_api.py",
     '改了標題的正式版也可能沒搜到）。注意很多期刊不接受引用 arXiv 預印本——',
     '改了標題的正式版也可能沒搜到）。目前引 arXiv 版合理——',
     ["CX-18g"]),
    # CX-18h～u（2026-10-14）：兩位反駁者實測到的缺陷與存活突變，每個防護一個突變。
    ("posted-content 不再視為預印本 → SSRN 被當成正式版建議改引", "lit_api.py",
     'or (c.get("type") or "") == "posted-content")',
     'or False)',
     ["CX-18h"]),
    ("DOI 路徑 S2 命中不記存在性 → S2 明明有這篇卻被說成無法解析", "lit_api.py",
     '            sp = s2_get_paper("DOI:" + doi)\n            confirmed = True\n',
     '            sp = s2_get_paper("DOI:" + doi)\n',
     ["CX-18i"]),
    ("只在 S2 失敗時才查 arXiv → S2 有記錄時讀不到作者自填的期刊 DOI", "lit_api.py",
     '        if not out["published_version"]:\n            entries = None\n',
     '        if title is None:\n            entries = None\n',
     ["CX-18j"]),
    ("作者自填 DOI 的 Crossref 404 當成查詢失敗 → 不存在的 DOI 被說成請稍後重試", "lit_api.py",
     '    if status == "not_found":\n        out["arxiv_record"]["journal_doi_status"] = (',
     '    if False:\n        out["arxiv_record"]["journal_doi_status"] = (',
     ["CX-18k"]),
    ("作者自填 DOI 不做作者／題目比對 → 資料集或整本論文集的 DOI 被當成正式版", "lit_api.py",
     '    elif fits:',
     '    elif True:',
     ["CX-18l"]),
    ("姓氏取逗號前整段 → van der Linden 兩種寫法對不上", "lit_api.py",
     '        return len(short) <= len(full) and full[-len(short):] == short',
     '        return short[-1] == full[-1]',
     ["CX-19a"]),
    ("年份不放寬 → 期刊先發表、後上 arXiv 的正式版被擋掉", "lit_api.py",
     'if pre_year and (not c.get("year") or c["year"] < pre_year - 1):',
     'if pre_year and (not c.get("year") or c["year"] < pre_year):',
     ["CX-18m"]),
    ("空作者也算作者 → 沒有真的作者搜尋卻寫「標題與作者搜尋都」", "lit_api.py",
     '    named = [a for a in pre_authors if _name_key(a)]',
     '    named = [a for a in pre_authors]',
     ["CX-18n"]),
    ("由標題推定的正式版不降信心 → 輸入 DOI 或 arXiv DOI 被當成 S2 合併訊號", "lit_api.py",
     '        elif not via_s2_merge:',
     '        elif False:',
     ["CX-18o", "CX-19g"]),
    ("單一作者不拉高題目門檻 → 同姓不同題的論文被列為可能的正式版", "lit_api.py",
     '    floor = 0.3 if len(named) > 1 else 0.5',
     '    floor = 0.3',
     ["CX-18p"]),
    ("DOI 路徑只接 HTTPError → Crossref 逾時直接當掉", "lit_api.py",
     '    except OSError as e:\n        out["crossref_error"] = str(e)\n        return None, "error"',
     '    except ZeroDivisionError as e:\n        out["crossref_error"] = str(e)\n        return None, "error"',
     ["CX-18q"]),
    ("作者沒送進 Crossref 的 query.author → 作者搜尋其實只是標題搜尋", "lit_api.py",
     '        params["query.author"] = author      # versions 用作者找改名後的正式版',
     '        pass',
     ["CX-18r"]),
    ("作者重疊門檻降到 0.2 → 只對上 1/3 作者的論文被列為可能的正式版", "lit_api.py",
     'ov is not None and ov >= 0.5 and ts >= floor',
     'ov is not None and ov >= 0.2 and ts >= floor',
     ["CX-18s"]),
    ("可能的正式版不截斷 → 清單無上限", "lit_api.py",
     'out["possible_published_versions"] = merged[:3]',
     'out["possible_published_versions"] = merged',
     ["CX-18t"]),
    ("作者搜尋失敗被吞掉 → 沒查完卻說查無正式版", "lit_api.py",
     '            out.setdefault("crossref_error", str(e))',
     '            pass',
     ["CX-18u"]),
    # CX-19a～p（2026-10-14 第三輪）：兩位反駁者對 surname() 與 cmd_versions 改寫的發現，每個防護一個突變。
    ("逗號寫法只比姓的最後一個詞 → García López 與 Pérez López 被判同一人", "lit_api.py",
     '    if known_a and known_b:\n        return fa == fb',
     '    if known_a and known_b:\n        return fa[-1] == fb[-1]',
     ["CX-19a"]),
    ("羅馬數字後綴不管剩幾個詞都砍 → 日本姓 Ii 被當成後綴", "lit_api.py",
     'len(fam) + len(given) - 1 >= 2:',
     'True:',
     ["CX-19c"]),
    ("書評防護只看姓不看名 → 同姓不同名的書評人被當成原著作者", "lit_api.py",
     '        return ga[0][0] == gb[0][0]',
     '        return True',
     ["CX-19d"]),
    ("只剩一個字母的名字也拿來比 → 「J.」對「J」作者重疊 1.0", "lit_api.py",
     '    if len(fam) == 1 and len(fam[0]) <= 1:\n        return None',
     '    if False:\n        return None',
     ["CX-19p"]),
    ("S2 合併的 DOI 不擋預印本伺服器 → SSRN 被當成最強訊號推薦改引", "lit_api.py",
     '                if status == "ok" and _is_preprint(cand):',
     '                if False:',
     ["CX-19e"]),
    ("Crossref 404 當成覆核失敗 → DataCite 的 DOI 被原樣當成正式版", "lit_api.py",
     '        if e.code == 404:\n            return None, "not_found"',
     '        if False:\n            return None, "not_found"',
     ["CX-19f"]),
    ("標題搜尋不排序 → 取 Crossref 排第一而不是最像的那筆", "lit_api.py",
     '            pubs.sort(key=lambda c: (c["title_sim"], c["author_overlap"] or 0.0), reverse=True)',
     '            pass',
     ["CX-19h"]),
    ("輸入 DOI 本身是期刊論文也去搜同名 → 被改引到別篇", "lit_api.py",
     '            if not _is_preprint(n):',
     '            if False:',
     ["CX-19i"]),
    ("沒標題也說「搜尋查無」 → 沒搜的被寫成查無", "lit_api.py",
     '    elif confirmed and not title:',
     '    elif False:',
     ["CX-19j"]),
    ("arXiv 編號不清空白與版本後綴 → 送出錯的編號，誤報不存在", "lit_api.py",
     '        out["arxiv"] = re.sub(r"v\\d+$", "", ident[6:].strip(), flags=re.I)',
     '        out["arxiv"] = ident[6:]',
     ["CX-19k"]),
    ("S2 沒作者時不改用 arXiv 的 → 自填 DOI 永遠對不上作者", "lit_api.py",
     '                pre_authors = pre_authors or e0.get("authors") or []',
     '                pass',
     ["CX-19l"]),
    ("較強訊號查詢失敗不降信心 → 一條路沒走完卻信心不變", "lit_api.py",
     '        if errors and out.get("version_link") != "input_doi_is_published":',
     '        if False:',
     ["CX-19m"]),
    ("可能的正式版不整體排序 → 自填 DOI 對不上的那筆佔第一格", "lit_api.py",
     '    possible.sort(key=lambda c: (c.get("author_overlap") or 0.0, c.get("title_sim") or 0.0), reverse=True)',
     '    pass',
     ["CX-19n"]),
    ("DOI 判定不清網址前綴 → https://doi.org/… 被當成標題去搜", "lit_api.py",
     '    return bool(re.match(r"^10\\.\\d{4,9}/\\S+", _clean_doi(ident)))',
     '    return bool(re.match(r"^10\\.\\d{4,9}/\\S+", ident))',
     ["CX-19o"]),
    # CX-20a～e（2026-10-14 衛生四項）：每個防護一個突變。目標檔含 "/" 或以 "." 開頭時，路徑相對於樹根。
    ("第一次 429 不印指引 → 使用者等完整輪重試才知道要申請金鑰", "lit_api.py",
     '            if e.code == 429:\n                _warn_s2_429(url, will_retry=attempt < retries)\n',
     '            if e.code == 429:\n                pass\n',
     ["CX-20a"]),
    ("最終錯誤 JSON 的 hint 不帶申請指引 → 只說「申請 key」不說去哪、怎麼放", "lit_api.py",
     '放入 .env。" + S2_KEY_HINT)',
     '放入 .env。")',
     ["CX-20a"]),
    ("打包過濾不擋 .bak → 備份檔進 ZIP", "package_skill.py",
     '\n            or BAK_RE.search(basename) is not None)',
     '\n            )',
     ["CX-20b"]),
    ("zip_check 不把 .bak 當風險 → 手動壓縮的 ZIP 帶著備份檔也放行", "evals/zip_check.py",
     '    hits["含 .bak 備份檔"] = ["%s: 備份檔不該出貨" % n for n in bak_files]',
     '    pass',
     ["CX-20b"]),
    ("拿掉 .gitignore 的 *.bak* → 備份檔可被 git add 進庫", ".gitignore",
     '# 備份檔不入庫（也不進 ZIP）；有邊界，*.bake 這種合法檔名不受影響\n*.bak\n*.bak-*\n*.bak.*\n',
     '',
     ["CX-20b"]),
    ("docx 時間戳改回現在時間 → 每重新產生一次位元組就不同、工作樹變髒", "evals/make_fixtures.py",
     'zi = zipfile.ZipInfo(name, date_time=DOCX_DATE)',
     'zi = zipfile.ZipInfo(name, date_time=__import__("time").localtime()[:6])',
     ["CX-20c"]),
    ("只查第一作者 → 第一、第二作者對調後發表的正式版找不到", "lit_api.py",
     '            who = [surname(named[0])] + ([surname(named[1])] if len(named) > 1 else [])',
     '            who = [surname(named[0])]',
     ["CX-20d", "CX-18r"]),
    ("一位作者查詢失敗就整批丟掉 → 另一位已拿到的結果也沒了", "lit_api.py",
     '                    fetch_err = fetch_err or e',
     '                    raise',
     ["CX-20e"]),
    # CX-20f／CX-20b 補強（2026-10-14 反駁者）：hint 要看主機與金鑰、重試措辭要誠實、.bak 目錄與邊界。
    ("429 的 hint 不看主機與金鑰 → Crossref 的 429 也叫人去申請 S2 金鑰", "lit_api.py",
     '            if _s2_429_seen and not os.environ.get("S2_API_KEY"):',
     '            if True:',
     ["CX-20f"]),
    ("最後一次 429 仍說會重試 → 承諾程式不會做的事", "lit_api.py",
     '    tail = "程式會自動退避重試。" if will_retry else "重試已用完，這次指令以 429 結束。"',
     '    tail = "程式會自動退避重試。"',
     ["CX-20f"]),
    ("備份目錄不剪 → scripts.bak-20261014/ 整個進 ZIP", "package_skill.py",
     '        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not should_skip(d)]',
     '        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]',
     ["CX-20b"]),
    ("zip_check 只看最後一段路徑 → 備份目錄裡的檔案放行", "evals/zip_check.py",
     'bak_files = [n for n in names if any(re.search(r"\\.bak(?:$|[-._])", seg) for seg in n.split("/"))]',
     'bak_files = [n for n in names if re.search(r"\\.bak(?:$|[-._])", n.rsplit("/", 1)[-1])]',
     ["CX-20b"]),
    (".bak 沒有邊界 → notes.bake.md 被無聲丟掉", "package_skill.py",
     'BAK_RE = re.compile(r"\\.bak(?:$|[-._])")',
     'BAK_RE = re.compile(r"\\.bak")',
     ["CX-20b"]),
]


def run_suite(root):
    """在 root 這棵樹上跑迴歸測試。root 是暫存複本，不是原始 repo。"""
    p = subprocess.run([sys.executable, os.path.join(root, "evals", "test_regression.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=root)
    out = p.stdout or ""
    return {ln.split()[1] for ln in out.splitlines() if ln.startswith("[FAIL]")}, out


def main():
    work = tempfile.mkdtemp(prefix="lit-mutation-")
    root = os.path.join(work, "repo")
    shutil.copytree(SKILL, root, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", "*.mutbak", "*.zip"))
    print("突變寫在暫存複本上，原始 repo 唯讀：%s\n" % root)
    try:
        return _run(root)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(root):
    baseline_fails, out = run_suite(root)
    if baseline_fails:
        print(f"⚠️ 未突變時已有失敗案例 {sorted(baseline_fails)} —— 先修好再跑突變測試")
        sys.exit(1)
    print("基線：全綠，開始突變\n")

    weak = []
    for desc, fname, orig, mutant, expect in MUTATIONS:
        target = (os.path.join(root, fname) if ("/" in fname or fname.startswith("."))
                  else os.path.join(root, "scripts", fname))
        bak = target + ".mutbak"
        shutil.copy(target, bak)
        try:
            src = open(bak, encoding="utf-8").read()
            if orig not in src:
                print(f"⚠️ 跳過(找不到目標片段，程式碼已改動？):{desc}")
                weak.append((desc, "目標片段不存在"))
                continue
            open(target, "w", encoding="utf-8").write(src.replace(orig, mutant, 1))
            fails, _ = run_suite(root)
            caught = sorted(fails & set(expect))
            if caught:
                print(f"✅ 抓到 | {desc}\n   失敗案例：{caught}")
            else:
                print(f"❌ 漏抓 | {desc}\n   預期 {expect} 應失敗，實際失敗：{sorted(fails) or '無'}")
                weak.append((desc, f"預期 {expect} 未失敗"))
        finally:
            shutil.copy(bak, target)
            os.unlink(bak)

    after, _ = run_suite(root)
    print(f"\n復原檢查：{'全綠' if not after else f'仍有失敗 {sorted(after)}(復原失敗！)'}")
    if weak or after:
        print(f"\n{len(weak)} 個防護缺乏測試偵測力：")
        for d, why in weak:
            print(f"  - {d}({why})")
        sys.exit(1)
    print(f"\n{len(MUTATIONS)}/{len(MUTATIONS)} 突變全部被偵測 —— 測試套件具備偵測力")


if __name__ == "__main__":
    main()
