# -*- coding: utf-8 -*-
"""上傳前檢查：打包的 ZIP 不得含金鑰、個資、或依賴本機的絕對路徑。

分享 skill(上傳 ChatGPT、寄給同學、附在 issue)前跑一次：
    python evals/zip_check.py lit-review.zip
"""
import re
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")
Z = sys.argv[1] if len(sys.argv) > 1 else "lit-review.zip"

PATTERNS = [
    (re.compile(r"s2k-[A-Za-z0-9]{10,}"), "Semantic Scholar 金鑰"),
    (re.compile(r"[\w.+-]+@(?:gmail|outlook|yahoo|hotmail|qq|163)\.com"), "個人 email"),
    (re.compile(r"[Cc]:[\\/]{1,2}Users[\\/]{1,2}[A-Za-z]"), "本機絕對路徑"),
    (re.compile(r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}"), "其他疑似金鑰"),
]

z = zipfile.ZipFile(Z)
names = z.namelist()
hits = {}
for n in names:
    if not n.endswith((".md", ".py", ".json", ".txt", ".svg", ".yml", ".yaml")):
        continue
    try:
        t = z.read(n).decode("utf-8", "replace")
    except Exception:
        continue
    for pat, label in PATTERNS:
        m = pat.search(t)
        if m:
            hits.setdefault(label, []).append(f"{n}: …{m.group(0)[:30]}…")

print(f"檔案數：{len(names)}｜壓縮後 {sum(i.compress_size for i in z.infolist())/1024:.0f} KB")
print("含 .env：", any(n.endswith(".env") for n in names))
print("含 .git：", any(".git/" in n for n in names))
print("含 __pycache__：", any("__pycache__" in n for n in names))
if hits:
    print("\n⚠️ 風險項：")
    for label, items in hits.items():
        print(f"  {label}（{len(items)} 處）")
        for i in items[:3]:
            print(f"    - {i}")
else:
    print("\n✅ 無金鑰／個資／本機絕對路徑")
print("\n頂層結構：")
for n in sorted({n.split("/")[1] for n in names if n.count("/") >= 1})[:15]:
    print("  ", n)
