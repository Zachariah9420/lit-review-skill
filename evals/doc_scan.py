# -*- coding: utf-8 -*-
"""掃描 repo 的文件與程式是否同步：指令數、測試數、功能清單、雙語鏡像。"""
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

# repo 根目錄從本檔位置推出來，不要寫死在某個人的家目錄：clone 到別的地方
# (或裝成 plugin)時，寫死的路徑會讓這支掃描器安靜地去掃另一棵樹，或掃不到而爆掉。
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(p):
    with open(os.path.join(R, p), encoding="utf-8") as f:
        return f.read()


issues = []

# 1) CLI 子命令 vs 文件宣稱
api = read("scripts/lit_api.py")
cli_cmds = sorted(set(re.findall(r'sub\.add_parser\("([a-z-]+)"', api)))
print(f"■ CLI 子命令（{len(cli_cmds)}）：{' '.join(cli_cmds)}")

skill = read("SKILL.md")
table_cmds = sorted(set(re.findall(r"^\| `([a-z-]+)[ <`]", skill, re.M)))
print(f"■ SKILL.md 指令表（{len(table_cmds)}）：{' '.join(table_cmds)}")
missing_in_doc = [c for c in cli_cmds if c not in skill]
if missing_in_doc:
    issues.append(f"CLI 有但 SKILL.md 未提及：{missing_in_doc}")

# 2) 測試數宣稱
n_cases = len(subprocess.run(
    [sys.executable, os.path.join(R, "evals", "test_regression.py"), "-v"],
    capture_output=True, text=True, encoding="utf-8", cwd=R).stdout.splitlines())
actual = subprocess.run([sys.executable, os.path.join(R, "evals", "test_regression.py")],
                        capture_output=True, text=True, encoding="utf-8", cwd=R).stdout
m = re.search(r"(\d+)/(\d+) passed", actual)
real_n = int(m.group(2)) if m else 0
print(f"■ 迴歸案例實際數：{real_n}")
for f in ("README.md", "README.zh-TW.md", "SKILL.md", "evals/README.md",
          "assets/gen_diagram.py"):
    txt = read(f)
    for claim in re.findall(r"(\d+)\s*(?:frozen cases|個凍結案例|案例迴歸|-case regression)", txt):
        if int(claim) != real_n:
            issues.append(f"{f} 宣稱 {claim} 個測試案例，實際 {real_n}")

# 3) 突變數宣稱
mut = read("evals/mutation_check.py")
n_mut = mut.count('", "lit_api.py"') + mut.count('", "cite_integrity.py"')
print(f"■ 突變數實際：{n_mut}")
for f in ("README.md", "README.zh-TW.md", "SKILL.md", "evals/README.md"):
    txt = read(f)
    for claim in re.findall(r"(\d+)\s*(?:fixed bugs|個已修的 bug|mutations|個突變(?:體)?)", txt):
        if int(claim) != n_mut:
            issues.append(f"{f} 宣稱 {claim} 個突變，實際 {n_mut}")

# 4) 圖上的指令數宣稱
gen = read("assets/gen_diagram.py")
for claim in re.findall(r"(\d+) (?:個指令|commands)", gen):
    if int(claim) != len(table_cmds):
        issues.append(f"架構圖宣稱 {claim} 個指令，SKILL.md 表列 {len(table_cmds)}")

# 5) 新功能是否進了所有該進的文件
for cmd, label in (("fulltext", "全文定位"), ("versions", "版本解析"),
                   ("export-xml", "EndNote XML"), ("retract", "撤稿")):
    for f in ("README.md", "README.zh-TW.md", "USAGE.md", "USAGE.zh-TW.md"):
        if cmd not in read(f):
            issues.append(f"{f} 未提及 {cmd}（{label}）")

# 6) 雙語鏡像：節數是否相當
for a, b in (("README.md", "README.zh-TW.md"), ("USAGE.md", "USAGE.zh-TW.md")):
    na, nb = len(re.findall(r"^## ", read(a), re.M)), len(re.findall(r"^## ", read(b), re.M))
    print(f"■ {a} {na} 節 vs {b} {nb} 節")
    if na != nb:
        issues.append(f"雙語節數不一致：{a}={na}, {b}={nb}")

# 7) git 狀態
st = subprocess.run(["git", "status", "--short"], capture_output=True, text=True,
                    encoding="utf-8", cwd=R).stdout.strip()
print(f"■ git 未提交變更：{st or '(乾淨)'}")

print("\n" + ("=" * 60))
if issues:
    print(f"發現 {len(issues)} 項不同步：")
    for i in issues:
        print("  -", i)
    # 這一行以前不存在。整支掃描器把問題收好、印出來，然後就結束了——Python
    # 回 0。於是它的每一項檢查都只是印給人看的建議，任何 CI、任何 `&&`、任何
    # 「跑過了、綠的」都讀到一個不可能變紅的離開碼。查了東西卻不會失敗的閘門，
    # 比沒有閘門更危險：它會被當成證據。
    sys.exit(1)
else:
    print("文件與程式碼一致")
    sys.exit(0)
