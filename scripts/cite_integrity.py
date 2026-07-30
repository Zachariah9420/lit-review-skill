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
import json
import re
import sys
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 要求標題自成一行(容許編號/井號前綴)：否則會被參考文獻條目標題裡的
# "References" 一詞(如 "A. References in science.")騙走切分點
HEADING_RE = re.compile(
    r"(?m)^[\s#\d.、]*(參考文獻|参考文献|References|REFERENCES|Bibliography|文獻目錄)\s*[::]?\s*$")
HEADING_LOOSE_RE = re.compile(r"(參考文獻|参考文献|References|REFERENCES|Bibliography|文獻目錄)")
CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*[,,\-–—]\s*\d{1,3})*)\]")
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
            fail(f"{path} 不是合法的 .docx(zip)檔——副檔名可能被改過；"
                 "若原本是純文字請改回 .txt/.md 再跑", as_json, file=path)
        except KeyError:
            fail(f"{path} 是 zip 但缺少 word/document.xml，不是 Word 文件", as_json, file=path)
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
        fail(f"{path} 不是 UTF-8 文字檔(若為二進位檔請確認格式)", as_json, file=path)


def expand(token_group, dropped=None):
    """'8-10' -> [8,9,10];'1, 2' -> [1,2]。無法展開者記入 dropped，不靜默丟棄。"""
    out = []
    for tok in re.split(r"[,,]", token_group):
        tok = tok.strip()
        m = re.match(r"^(\d+)\s*[\-–—]\s*(\d+)$", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and b - a <= 300:      # 300 足以涵蓋任何真實論文的引用範圍
                out.extend(range(a, b + 1))
            elif dropped is not None:        # 逆序或異常大範圍：回報而非默默忽略
                dropped.append(tok)
        elif tok.isdigit():
            out.append(int(tok))
        elif tok and dropped is not None:
            dropped.append(tok)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true")
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

    cited, dropped_tokens = set(), []
    for m in CITE_RE.finditer(body):
        cited.update(expand(m.group(1), dropped_tokens))

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
        result["unsupported_style_note"] = (
            f"找到參考文獻標題與 {len(cited)} 個文內引用，但一筆列表條目都解析不到。"
            "本工具的列表條目需以 [1] 或 1. / 1) 開頭；若你的列表是 Word 自動編號清單，"
            "編號不存在於文字中因而無法解析。**無法核對，此結果不代表引用無誤**——"
            "請把列表改為文字編號後重跑，或人工核對。")
    if no_numeric:
        result["unsupported_style_note"] = (
            f"全文查無數字式引用([1]、[2])；偵測到 {apa_hits} 處疑似作者-年份(APA)引用。"
            "本工具只支援數字式引用，作者-年份格式請人工核對——**此結果不代表引用無誤**。"
            if apa_hits else
            "全文查無數字式引用([1]、[2])也查無參考文獻條目；檔案可能為空、非預期格式，"
            "或使用本工具不支援的引用樣式(如作者-年份)。**此結果不代表引用無誤**。")
    issues = (result["cited_not_listed"] or result["listed_not_cited"]
              or dupes or gaps or not result["refs_heading_found"] or no_numeric
              or dropped_tokens or unparsable_list)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"檔案：{args.file}")
        if no_numeric or unparsable_list:
            print("⚠️ " + result["unsupported_style_note"])
        if not result["refs_heading_found"]:
            print("⚠️ 找不到參考文獻標題(參考文獻/References…)，整份當正文處理——結果不可靠")
        print(f"文內引用 {len(cited)} 個編號|列表 {len(listed)} 筆")
        print(f"引了沒列：{result['cited_not_listed'] or '無'}")
        print(f"列了沒引：{result['listed_not_cited'] or '無'}")
        print(f"列表重複編號：{dupes or '無'}")
        print(f"列表跳號：{gaps or '無'}")
        if dropped_tokens:
            print(f"⚠️ 無法解析的引用標記(未計入):{dropped_tokens}")
        if heading_mode == "loose":
            print("⚠️ 參考文獻標題不是自成一行(以寬鬆比對切分)——切分點可能落錯，結果需人工確認")
        print("結論：" + ("⚠️ 無法核對(見上方說明)" if no_numeric or unparsable_list
                        else "❌ 有問題，見上" if issues else "✅ 三向核對全部通過"))

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
