# -*- coding: utf-8 -*-
"""突變測試：把已修的缺陷塞回去，確認迴歸測試真的抓得到。

一個永遠全綠的測試套件可能只是沒有偵測力。這支腳本反向驗證：
每個突變都**必須**讓至少一個指定案例失敗，否則該防護等於沒有測試保護。

執行：python evals/mutation_check.py
任何情況下都會復原原始檔(finally)，但仍建議在乾淨的 git 工作樹上跑。
"""
import os
import shutil
import subprocess
import sys

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
]


def run_suite():
    p = subprocess.run([sys.executable, os.path.join(BASE, "test_regression.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=SKILL)
    out = p.stdout or ""
    return {ln.split()[1] for ln in out.splitlines() if ln.startswith("[FAIL]")}, out


def main():
    baseline_fails, out = run_suite()
    if baseline_fails:
        print(f"⚠️ 未突變時已有失敗案例 {sorted(baseline_fails)} —— 先修好再跑突變測試")
        sys.exit(1)
    print("基線：全綠，開始突變\n")

    weak = []
    for desc, fname, orig, mutant, expect in MUTATIONS:
        target = os.path.join(SKILL, "scripts", fname)
        bak = target + ".mutbak"
        shutil.copy(target, bak)
        try:
            src = open(bak, encoding="utf-8").read()
            if orig not in src:
                print(f"⚠️ 跳過(找不到目標片段，程式碼已改動？):{desc}")
                weak.append((desc, "目標片段不存在"))
                continue
            open(target, "w", encoding="utf-8").write(src.replace(orig, mutant, 1))
            fails, _ = run_suite()
            caught = sorted(fails & set(expect))
            if caught:
                print(f"✅ 抓到 | {desc}\n   失敗案例：{caught}")
            else:
                print(f"❌ 漏抓 | {desc}\n   預期 {expect} 應失敗，實際失敗：{sorted(fails) or '無'}")
                weak.append((desc, f"預期 {expect} 未失敗"))
        finally:
            shutil.copy(bak, target)
            os.unlink(bak)

    after, _ = run_suite()
    print(f"\n復原檢查：{'全綠' if not after else f'仍有失敗 {sorted(after)}(復原失敗！)'}")
    if weak or after:
        print(f"\n{len(weak)} 個防護缺乏測試偵測力：")
        for d, why in weak:
            print(f"  - {d}({why})")
        sys.exit(1)
    print(f"\n{len(MUTATIONS)}/{len(MUTATIONS)} 突變全部被偵測 —— 測試套件具備偵測力")


if __name__ == "__main__":
    main()
