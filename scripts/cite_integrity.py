#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cite_integrity.py — 文內引用 vs 參考文獻列表三向核對(數字式引用)

用法:
  python cite_integrity.py <檔案>          # 支援 .docx / .md / .txt
  python cite_integrity.py <檔案> --json   # 機器可讀輸出

檢查:1) 文內引了但列表沒有  2) 列表有但文內沒引  3) 列表重複編號/編號跳號
支援格式:[1]、[1, 2]、[8-10](含 en-dash)。作者-年份(APA)格式不支援,會如實說。
"""
import argparse
import json
import re
import sys
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HEADING_RE = re.compile(r"(參考文獻|参考文献|References|REFERENCES|Bibliography|文獻目錄)")
CITE_RE = re.compile(r"\[(\d{1,3}(?:\s*[,,\-–—]\s*\d{1,3})*)\]")
REF_LINE_RE = re.compile(r"^\s*\[(\d{1,3})\]", re.MULTILINE)


def read_text(path):
    if path.lower().endswith(".docx"):
        xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
        paras = re.split(r"</w:p>", xml)
        return "\n".join("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)) for p in paras)
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def expand(token_group):
    """'8-10' -> [8,9,10];'1, 2' -> [1,2]"""
    out = []
    for tok in re.split(r"[,,]", token_group):
        tok = tok.strip()
        m = re.match(r"^(\d+)\s*[\-–—]\s*(\d+)$", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and b - a <= 50:
                out.extend(range(a, b + 1))
        elif tok.isdigit():
            out.append(int(tok))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    text = read_text(args.file)

    # 以「最後一個」參考文獻標題切分正文與列表(正文可能提到 References 一詞)
    split_at = None
    for m in HEADING_RE.finditer(text):
        split_at = m.start()
    if split_at is None:
        body, reflist = text, ""
    else:
        body, reflist = text[:split_at], text[split_at:]

    cited = set()
    for m in CITE_RE.finditer(body):
        cited.update(expand(m.group(1)))

    listed_seq = [int(m.group(1)) for m in REF_LINE_RE.finditer(reflist)]
    listed = set(listed_seq)
    dupes = sorted({n for n in listed_seq if listed_seq.count(n) > 1})

    gaps = []
    if listed:
        expected = set(range(1, max(listed) + 1))
        gaps = sorted(expected - listed)

    result = {
        "file": args.file,
        "refs_heading_found": split_at is not None,
        "cited_count": len(cited),
        "listed_count": len(listed),
        "cited_not_listed": sorted(cited - listed),
        "listed_not_cited": sorted(listed - cited),
        "duplicate_ref_numbers": dupes,
        "numbering_gaps_in_list": gaps,
    }
    issues = (result["cited_not_listed"] or result["listed_not_cited"]
              or dupes or gaps or not result["refs_heading_found"])

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"檔案:{args.file}")
        if not result["refs_heading_found"]:
            print("⚠️ 找不到參考文獻標題(參考文獻/References…),整份當正文處理——結果不可靠")
        print(f"文內引用 {len(cited)} 個編號|列表 {len(listed)} 筆")
        print(f"引了沒列:{result['cited_not_listed'] or '無'}")
        print(f"列了沒引:{result['listed_not_cited'] or '無'}")
        print(f"列表重複編號:{dupes or '無'}")
        print(f"列表跳號:{gaps or '無'}")
        print("結論:" + ("❌ 有問題,見上" if issues else "✅ 三向核對全部通過"))

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
