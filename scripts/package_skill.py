#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把這個 repo 打包成可上傳的 skill ZIP（ChatGPT Skills、或分享給別人）。

    python scripts/package_skill.py                # 產生 lit-review.zip
    python scripts/package_skill.py -o /tmp/x.zip  # 指定輸出位置

為什麼不能直接壓縮 clone 下來的資料夾：
  1. `.git` 會被一起包進去（約 250 KB 的無用歷史，且掃描器可能因此標記）
  2. 資料夾名稱會是 repo 名 `lit-review-skill`，但 skill 的識別名是 `lit-review`
     （SKILL.md frontmatter 的 name）——本腳本會把頂層改成 `lit-review`

打包後會自動跑 evals/zip_check.py，確認沒有把金鑰、個人 email、或本機絕對
路徑包進去。檢查沒過就不會留下 ZIP——寧可不給檔案，也不要給一個外洩的檔案。
"""
import argparse
import os
import subprocess
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_NAME = "lit-review"          # 與 SKILL.md frontmatter 的 name 一致
SKIP_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache", ".idea", ".vscode"}
SKIP_FILES = {".env", ".DS_Store", "Thumbs.db"}
SKIP_EXT = (".pyc", ".pyo", ".zip")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default=os.path.join(os.getcwd(), f"{SKILL_NAME}.zip"))
    args = ap.parse_args()

    out = os.path.abspath(args.output)

    # 優先用 git 追蹤清單：打包內容就等於「使用者 clone 會拿到什麼」，
    # 也自動排除 .gitignore 的東西（本機的預覽圖、快取、.env）。
    files_list, source = [], "git ls-files"
    try:
        r = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            files_list = [os.path.join(REPO, p) for p in r.stdout.splitlines() if p.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    if not files_list:                      # 不是 git repo（例如已解壓的 ZIP）就走檔案掃描
        source = "檔案掃描"
        for root, dirs, fs in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            files_list += [os.path.join(root, f) for f in fs]

    n, raw = 0, 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files_list:
            f = os.path.basename(p)
            if (f in SKIP_FILES or f.endswith(SKIP_EXT)
                or f.startswith(".env") or f.endswith(".env")):
                continue
            if not os.path.isfile(p) or os.path.abspath(p) == out:
                continue
            arc = os.path.join(SKILL_NAME, os.path.relpath(p, REPO)).replace("\\", "/")
            z.write(p, arc)
            n += 1
            raw += os.path.getsize(p)

    size = os.path.getsize(out)
    print(f"打包完成：{out}")
    print(f"  {n} 個檔案（清單來源：{source}）｜原始 {raw / 1024:.0f} KB → 壓縮 {size / 1024:.0f} KB")
    print(f"  頂層資料夾：{SKILL_NAME}/（SKILL.md 位於 {SKILL_NAME}/SKILL.md）\n")

    check = os.path.join(REPO, "evals", "zip_check.py")
    r = subprocess.run([sys.executable, check, out], text=True, encoding="utf-8")
    if r.returncode != 0:
        os.remove(out)
        print("\n❌ 安全檢查未通過，已刪除 ZIP。修正後重新打包。")
        sys.exit(1)
    print("\n可以上傳了。")


if __name__ == "__main__":
    main()
