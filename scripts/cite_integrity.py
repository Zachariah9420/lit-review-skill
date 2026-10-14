#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cite_integrity.py — 文內引用 vs 參考文獻列表三向核對(數字式引用)

用法：
  python cite_integrity.py <檔案>          # 支援 .docx / .md / .txt
  python cite_integrity.py <檔案> --json   # 機器可讀輸出

檢查：1) 文內引了但列表沒有  2) 列表有但文內沒引  3) 列表重複編號/編號跳號
支援格式：[1]、[1, 2]、[8-10](含 en-dash)。作者-年份(APA)格式不支援，會如實說。
"""
import argparse
import html
import importlib.util
import json
import os
import re
import sys
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 訊息表與翻譯機制：機制在 scripts/_i18n.py，本檔只保有自己的訊息表。依檔案路徑載入，用專屬名稱
# 登記在 sys.modules（lit_api 與 cite_integrity 共用同一份），不動 sys.path、也不 `import _i18n`：
# 宿主行程若已載入別的同名模組，`import _i18n` 拿到的是那一個（ImportError: cannot import name
# load_dotenv），而把 scripts/ 插進 sys.path 會改到宿主的模組搜尋路徑。
_HERE = os.path.dirname(os.path.abspath(__file__))
_I18N_NAME = "lit_review_i18n"


def _load_i18n():
    mod = sys.modules.get(_I18N_NAME)
    if mod is None:
        spec = importlib.util.spec_from_file_location(_I18N_NAME, os.path.join(_HERE, "_i18n.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_I18N_NAME] = mod
        spec.loader.exec_module(mod)
    return mod


_i18n = _load_i18n()
load_dotenv = _i18n.load_dotenv   # main() 經本模組的全域呼叫它（測試會替換 ci.load_dotenv）

# 使用者看得到的訊息，全部在這裡：{key: {"zh": 中文（逐字保留原文）, "en": 英文}}。規則同 lit_api.py。
MESSAGES = {
    "err.not_docx": {
        "zh": "{path} 不是合法的 .docx(zip)檔——副檔名可能被改過；"
              "若原本是純文字請改回 .txt/.md 再跑",
        "en": "{path} is not a valid .docx (zip) file - the extension may have been changed; "
              "if it was plain text, rename it back to .txt/.md and rerun"},
    "err.no_document_xml": {
        "zh": "{path} 是 zip 但缺少 word/document.xml，不是 Word 文件",
        "en": "{path} is a zip file but has no word/document.xml, so it is not a Word document"},
    "err.not_utf8": {
        "zh": "{path} 不是 UTF-8 文字檔(若為二進位檔請確認格式)",
        "en": "{path} is not a UTF-8 text file (if it is a binary file, check its format)"},
    "note.unparsable_list": {
        "zh": "找到參考文獻標題與 {n} 個文內引用，但一筆列表條目都解析不到。"
              "本工具的列表條目需以 [1] 或 1. / 1) 開頭；若你的列表是 Word 自動編號清單，"
              "編號不存在於文字中因而無法解析。**無法核對，此結果不代表引用無誤**——"
              "請把列表改為文字編號後重跑，或人工核對。",
        "en": "Found a reference-list heading and {n} in-text citations, but could not parse a single list "
              "entry. List entries must start with [1] or 1. / 1); if your list is a Word auto-numbered list, "
              "the numbers are not part of the text and cannot be parsed. **This cannot be checked, and the "
              "result does not mean the citations are correct** - convert the list to typed numbers and "
              "rerun, or check by hand."},
    "note.no_numeric_apa": {
        "zh": "全文查無數字式引用([1]、[2])；偵測到 {apa} 處疑似作者-年份(APA)引用。"
              "本工具只支援數字式引用，作者-年份格式請人工核對——**此結果不代表引用無誤**。",
        "en": "No numeric citations ([1], [2]) were found in the text; {apa} possible author-year (APA) "
              "citations were detected. This tool only supports numeric citations, so check author-year "
              "formats by hand - **this result does not mean the citations are correct**."},
    "note.no_numeric_empty": {
        "zh": "全文查無數字式引用([1]、[2])也查無參考文獻條目；檔案可能為空、非預期格式，"
              "或使用本工具不支援的引用樣式(如作者-年份)。**此結果不代表引用無誤**。",
        "en": "No numeric citations ([1], [2]) and no reference-list entries were found; the file may be "
              "empty, in an unexpected format, or use a citation style this tool does not support (such as "
              "author-year). **This result does not mean the citations are correct**."},
    "report.file": {"zh": "檔案：{file}", "en": "File: {file}"},
    "report.no_heading": {
        "zh": "⚠️ 找不到參考文獻標題(參考文獻/References…)，整份當正文處理——結果不可靠",
        "en": "⚠️ No reference-list heading (References / Bibliography ...) found; the whole file was "
              "treated as body text - the result is unreliable"},
    "report.counts": {"zh": "文內引用 {cited} 個編號|列表 {listed} 筆",
                      "en": "In-text citations: {cited} numbers | list: {listed} entries"},
    "report.cited_not_listed": {"zh": "引了沒列：{items}", "en": "Cited but not listed: {items}"},
    "report.listed_not_cited": {"zh": "列了沒引：{items}", "en": "Listed but not cited: {items}"},
    "report.dup_numbers": {"zh": "列表重複編號：{items}", "en": "Duplicate numbers in the list: {items}"},
    "report.gaps": {"zh": "列表跳號：{items}", "en": "Gaps in the list numbering: {items}"},
    "report.none": {"zh": "無", "en": "none"},
    "report.unparsed_tokens": {"zh": "⚠️ 無法解析的引用標記(未計入):{tokens}",
                               "en": "⚠️ Citation markers that could not be parsed (not counted): {tokens}"},
    "report.loose_heading": {
        "zh": "⚠️ 參考文獻標題不是自成一行(以寬鬆比對切分)——切分點可能落錯，結果需人工確認",
        "en": "⚠️ The reference-list heading is not on its own line (split by loose matching) - the split "
              "point may be wrong, so confirm the result by hand"},
    "report.verdict": {"zh": "結論：{verdict}", "en": "Verdict: {verdict}"},
    "report.verdict.cannot_check": {"zh": "⚠️ 無法核對(見上方說明)", "en": "⚠️ Cannot be checked (see the note above)"},
    "report.verdict.problems": {"zh": "❌ 有問題，見上", "en": "❌ Problems found, see above"},
    "report.verdict.pass": {"zh": "✅ 三向核對全部通過", "en": "✅ All three cross-checks passed"},
    "help.lang": {"zh": "輸出訊息語言(zh 或 en)，優先於環境變數 LIT_LANG",
                  "en": "Language of the tool's messages (zh or en); takes precedence over the LIT_LANG environment variable"},
}

_T = _i18n.make_translator(MESSAGES)


def _H(key):
    """argparse 的 help= 會做 %-展開：只有說明文字走這裡，把 % 跳脫掉。"""
    return _T(key).replace("%", "%%")


# 要求標題自成一行(容許編號/井號前綴)：否則會被參考文獻條目標題裡的
# "References" 一詞(如 "A. References in science.")騙走切分點
HEADING_RE = re.compile(
    r"(?m)^[\s#\d.、]*(參考文獻|参考文献|References|REFERENCES|Bibliography|文獻目錄)\s*[::]?\s*$")
HEADING_LOOSE_RE = re.compile(r"(參考文獻|参考文献|References|REFERENCES|Bibliography|文獻目錄)")
# 這個字元類裡的「兩個逗號」其實是 U+002C 寫了兩次（初版就是如此），所以全形
# 「，」、頓號「、」與分號從來沒有被支援過——中文論文寫 [1，2] 是常態，而整個
# 中括號會**完全比對不到**，那一筆引用於是從查核裡消失。用跳脫寫法列出來，
# 免得下一次有人看不出兩個字元長得一樣。
CITE_SEPS = r",，、;；"
CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*[" + CITE_SEPS + r"\-–—]\s*\d{1,3})*)\]")
# 長得像引用、但 CITE_RE 吃不下的中括號（IEEE 的 [1, p. 45]、[2, Fig. 3]）。
# 以前這種整個不被比對，而 unparsed_citation_tokens 仍然回報 []，
# 等於工具宣稱它解析了它其實沒看見的東西。
CITE_LOOSE_RE = re.compile(r"\[\s*\d{1,3}[^\]]{0,40}\]")
# 跨中括號的範圍寫法：[1]-[3]。兩個中括號各自被當成獨立引用，中間那一橫是內文，
# 於是 2 永遠不會被展開，然後報告說「列了沒引：[2]」。
CITE_SPAN_RE = re.compile(r"\[\s*(\d{1,3})\s*\]\s*[-–—]\s*\[\s*(\d{1,3})\s*\]")
# 參考文獻條目：支援 [1] 與 Vancouver/AMA 的 "1." "1)" 樣式。
# 醫學與藥學期刊幾乎一律用 Vancouver；只認 [n] 會讓列表解析成 0 筆，
# 然後把全部文內引用誤報成「引了沒列」——沉默且自信的錯誤答案。
REF_LINE_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)]\s)", re.MULTILINE)


def fail(message, as_json=False, **extra):
    payload = {"error": "INVALID_INPUT", "message": message, **extra}
    print(json.dumps(payload, ensure_ascii=False) if as_json else f"❌ {message}")
    sys.exit(2)


def read_text(path, as_json=False):
    if path.lower().endswith(".docx"):
        try:
            xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
        except OSError as e:          # 檔案不存在/無權限：也要結構化錯誤，不要 traceback
            fail(str(e), as_json, file=path)
        except zipfile.BadZipFile:
            fail(_T("err.not_docx", path=path), as_json, file=path)
        except KeyError:
            fail(_T("err.no_document_xml", path=path), as_json, file=path)
        # 段落內的換行與定位點是 <w:br/> 與 <w:tab/>,不是 <w:t> 的內容。只抓
        # <w:t> 再用 "" 接起來,會讓「同一段裡用 shift+enter 分行的三筆參考文獻」
        # 黏成一行——而條目比對 REF_LINE_RE 是行首錨定的,於是只認得出第一筆,
        # 另外兩筆變成「引了沒列」。先把它們換成帶內容的 <w:t>,讓抓取看得到。
        xml = re.sub(r"<w:br\s*/?>", "</w:t><w:t>\n</w:t><w:t>", xml)
        xml = re.sub(r"<w:tab\s*/?>", "</w:t><w:t>\t</w:t><w:t>", xml)
        paras = re.split(r"</w:p>", xml)
        # 必須解 XML 字元參照：Word 會把 &quot; 與 en-dash(&#x2013;)寫成參照，
        # 不解碼會讓「[8–10]」這種範圍引用抓不到，引號也會變成 &quot; 雜訊
        return html.unescape(
            "\n".join("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)) for p in paras))
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError as e:
        fail(str(e), as_json, file=path)
    except UnicodeDecodeError:
        fail(_T("err.not_utf8", path=path), as_json, file=path)


def expand(token_group, dropped=None):
    """'8-10' -> [8,9,10];'1, 2' -> [1,2]。無法展開者記入 dropped，不靜默丟棄。"""
    out = []
    for tok in re.split(r"[" + CITE_SEPS + r"]", token_group):
        tok = tok.strip()
        m = re.match(r"^(\d+)\s*[\-–—]\s*(\d+)$", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and b - a <= 300:      # 300 足以涵蓋任何真實論文的引用範圍
                out.extend(range(a, b + 1))
            elif dropped is not None:        # 逆序或異常大範圍：回報而非默默忽略
                dropped.append(tok)
        elif tok.isdigit():
            # 參考文獻從 1 起編。抓到 [0] 幾乎一定是程式碼裡的陣列索引,
            # 當成引用會憑空生出一筆「引了沒列」。
            if int(tok) >= 1:
                out.append(int(tok))
            elif dropped is not None:
                dropped.append(tok)
        elif tok and dropped is not None:
            dropped.append(tok)
    return out


def main():
    # 順序有意義：--lang 先寫進 LIT_LANG（旗標優先），.env 只在它仍未設（或為空）時補上，然後才建 argparse。
    sys.argv = [sys.argv[0]] + _i18n.pop_lang_arg(sys.argv[1:])
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lang", metavar="zh|en", help=_H("help.lang"))   # 已被預掃描取走；只為了 -h 列出
    args = ap.parse_args()

    text = read_text(args.file, args.json)

    # 以「最後一個自成一行的參考文獻標題」切分正文與列表；找不到獨立成行的才退而
    # 求其次用寬鬆比對(並在報告標明，因為那可能切在條目標題中的 References 一詞上)
    split_at, heading_mode = None, "strict"
    for m in HEADING_RE.finditer(text):
        split_at = m.start()
    if split_at is None:
        for m in HEADING_LOOSE_RE.finditer(text):
            split_at, heading_mode = m.start(), "loose"
    body, reflist = (text, "") if split_at is None else (text[:split_at], text[split_at:])

    # 引用不會在參考文獻標題那一行就停止:附錄、致謝、作者簡介、審稿回覆都會引。
    # 以前只掃 body(標題之前),標題之後的引用整段不受查核——一篇在附錄裡引了
    # 兩筆不存在文獻的論文,會印出「✅ 三向核對全部通過」並回 0。
    # 掃全文,再把「被當成清單條目消耗掉的那個編號」扣掉:條目開頭的 [1] 是標號,
    # 不是引用。用等長空白取代,行的結構與其餘內容都不動。
    reflist_wo_markers = REF_LINE_RE.sub(lambda m: " " * len(m.group(0)), reflist)
    scan_text = body + reflist_wo_markers
    # markdown 的程式碼區塊裡,arr[0]、v[2] 這種陣列索引長得跟引用一模一樣。
    if args.file.lower().endswith((".md", ".markdown")):
        scan_text = re.sub(r"(?ms)^\s*```.*?^\s*```\s*$", "\n", scan_text)
        scan_text = re.sub(r"`[^`\n]*`", " ", scan_text)

    cited, dropped_tokens = set(), []
    for m in CITE_SPAN_RE.finditer(scan_text):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b and b - a <= 300:
            cited.update(range(a, b + 1))
    for m in CITE_RE.finditer(scan_text):
        cited.update(expand(m.group(1), dropped_tokens))
    # 長得像引用卻沒被 CITE_RE 吃下的中括號,要說出來而不是當作不存在。
    for m in CITE_LOOSE_RE.finditer(scan_text):
        if not CITE_RE.fullmatch(m.group(0)):
            dropped_tokens.append(m.group(0).strip())

    listed_seq = [int(m.group(1) or m.group(2)) for m in REF_LINE_RE.finditer(reflist)]
    listed = set(listed_seq)
    dupes = sorted({n for n in listed_seq if listed_seq.count(n) > 1})

    gaps = []
    if listed:
        expected = set(range(1, max(listed) + 1))
        gaps = sorted(expected - listed)

    # 全文都沒有 [n]：可能是 APA 作者-年份格式或空檔案——不可回報「全部通過」
    no_numeric = not cited and not listed
    # 文內有引用、卻一筆列表條目都解析不到：這是「列表格式不支援」的明確指紋
    # （如 EndNote 輸出的 Word 自動編號清單，編號不在文字節點裡）。
    # 此時把所有引用報成「引了沒列」是自信的錯誤答案，必須改判為無法核對。
    unparsable_list = bool(cited) and not listed and split_at is not None
    apa_hits = len(re.findall(r"\([A-Z][A-Za-zÀ-ɏ'’\-]+(?:\s+(?:et al\.|and|&|&amp;)"
                              r"[^)]{0,40})?,?\s*(?:19|20)\d{2}[a-z]?\)", text))
    result = {
        "file": args.file,
        "refs_heading_found": split_at is not None,
        "refs_heading_match": heading_mode if split_at is not None else None,
        "unparsed_citation_tokens": dropped_tokens,
        "numeric_citations_found": not no_numeric,
        "reference_list_parsed": not unparsable_list,
        "cited_count": len(cited),
        "listed_count": len(listed),
        # 列表解析失敗時不得輸出比對清單——那會是一整份假的「引了沒列」
        "cited_not_listed": [] if unparsable_list else sorted(cited - listed),
        "listed_not_cited": [] if unparsable_list else sorted(listed - cited),
        "duplicate_ref_numbers": dupes,
        "numbering_gaps_in_list": gaps,
    }
    if unparsable_list:
        result["unsupported_style_note"] = _T("note.unparsable_list", n=len(cited))
    if no_numeric:
        result["unsupported_style_note"] = (
            _T("note.no_numeric_apa", apa=apa_hits) if apa_hits else _T("note.no_numeric_empty"))
    issues = (result["cited_not_listed"] or result["listed_not_cited"]
              or dupes or gaps or not result["refs_heading_found"] or no_numeric
              or dropped_tokens or unparsable_list)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        none = _T("report.none")
        print(_T("report.file", file=args.file))
        if no_numeric or unparsable_list:
            print("⚠️ " + result["unsupported_style_note"])
        if not result["refs_heading_found"]:
            print(_T("report.no_heading"))
        print(_T("report.counts", cited=len(cited), listed=len(listed)))
        print(_T("report.cited_not_listed", items=result["cited_not_listed"] or none))
        print(_T("report.listed_not_cited", items=result["listed_not_cited"] or none))
        print(_T("report.dup_numbers", items=dupes or none))
        print(_T("report.gaps", items=gaps or none))
        if dropped_tokens:
            print(_T("report.unparsed_tokens", tokens=dropped_tokens))
        if heading_mode == "loose":
            print(_T("report.loose_heading"))
        print(_T("report.verdict", verdict=(
            _T("report.verdict.cannot_check") if no_numeric or unparsable_list
            else _T("report.verdict.problems") if issues else _T("report.verdict.pass"))))

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
