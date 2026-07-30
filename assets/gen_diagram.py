# -*- coding: utf-8 -*-
"""產生 lit-review 架構圖(SVG，深淺雙版本，GitHub README 用)。"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))

THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "hairline": "#e1e0d9", "baseline": "#c3c2b7",
        "c": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
        "fill_alpha": "0.07",
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
        "hairline": "#2c2c2a", "baseline": "#383835",
        "c": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
        "fill_alpha": "0.12",
    },
}

LANG = "zh"          # 由 main 迴圈覆寫
W, H = 1274, 622
COL_Y, COL_H, COL_W, GAP = 148, 302, 210, 30
XS = [52 + i * (COL_W + GAP) for i in range(5)]

COLS_ZH = [
    ("輸入", "INPUT", [
        ("論文草稿", 0), ("研究主題", 0), ("單筆引用", 0),
        ("—", 2),
        ("20 個指令或自然語言：", 1),
        ("check · find · write", 1), ("verify · annotate …", 1),
        ("無指令時自動推斷模式", 1),
    ]),
    ("三模式+工具組", "MODES", [
        ("A|找文獻", 0), ("B|查核引用", 0), ("C|文獻支撐寫作", 0),
        ("—", 2),
        ("研究生工具組：", 1),
        ("matrix · map · gap", 1), ("counter · strength", 1),
        ("claims · integrity", 1), ("glossary · rehearse …", 1),
    ]),
    ("檢索引擎", "ENGINE", [
        ("lit_api.py|純標準函式庫", 1),
        ("Semantic Scholar", 0), ("⇄ OpenAlex 429 備援", 0),
        ("Crossref 書目權威", 0), ("arXiv(掛掉退 S2)", 0),
        ("撤稿 · 版本解析", 0),
        ("snowball · batch", 1), ("brief/pick 省 token 漏斗", 1),
    ]),
    ("驗證層", "VERIFY", [
        ("三層隔離：", 0), ("證據 · 角色 · 注入", 1),
        ("雙路徑身分閘門", 0),
        ("對抗式自查", 0),
        ("證據層級", 0), ("[摘要] [全文 p.X] [?]", 1),
        ("50 案例迴歸測試", 0),
    ]),
    ("產出", "OUTPUT", [
        ("查核報告", 0), ("給作者修改清單", 0),
        ("RIS · BibTeX · EN-XML", 0), ("帶引用文章", 0),
        ("文獻矩陣", 0), ("筆記卡 · 領域地圖", 0),
    ]),
]

COLS_EN = [
    ("Input", "INPUT", [
        ("thesis draft", 0), ("research topic", 0), ("single citation", 0),
        ("—", 2),
        ("20 commands, or plain language:", 1),
        ("check · find · write", 1), ("verify · annotate …", 1),
        ("mode inferred when unspecified", 1),
    ]),
    ("Modes + toolkit", "MODES", [
        ("A|find literature", 0), ("B|audit citations", 0), ("C|grounded writing", 0),
        ("—", 2),
        ("grad toolkit:", 1),
        ("matrix · map · gap", 1), ("counter · strength", 1),
        ("claims · integrity", 1), ("glossary · rehearse …", 1),
    ]),
    ("Retrieval engine", "ENGINE", [
        ("lit_api.py|stdlib only", 1),
        ("Semantic Scholar", 0), ("⇄ OpenAlex on 429", 0),
        ("Crossref (authority)", 0), ("arXiv (falls back to S2)", 0),
        ("retractions · versions", 0),
        ("snowball · batch", 1), ("brief/pick token funnel", 1),
    ]),
    ("Verification", "VERIFY", [
        ("three firewalls:", 0), ("evidence · role · injection", 1),
        ("dual-path identity gate", 0),
        ("adversarial self-audit", 0),
        ("evidence levels", 0), ("[abstract] [full text p.X] [?]", 1),
        ("50-case regression suite", 0),
    ]),
    ("Output", "OUTPUT", [
        ("audit report", 0), ("author fix list", 0),
        ("RIS · BibTeX · EN-XML", 0), ("cited article", 0),
        ("literature matrix", 0), ("note cards · field map", 0),
    ]),
]



def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build(theme):
    t = THEMES[theme]
    p = []
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="system-ui,-apple-system,\'Segoe UI\',\'Noto Sans TC\',sans-serif">')
    p.append(f'<rect width="{W}" height="{H}" rx="14" fill="{t["surface"]}"/>')
    # 標題
    p.append(f'<text x="52" y="64" font-size="30" font-weight="700" fill="{t["ink"]}">lit-review</text>')
    sub = ("evidence-layer skill for LLM agents" if LANG == "en"
           else "給 LLM agent 的文獻證據層 skill")
    tag = ("retrieve first · write second · self-audit last — every citation survives the follow-up question"
           if LANG == "en" else "先檢索、後寫作、寫完自查 —— 每個引用都經得起追問")
    p.append(f'<text x="212" y="64" font-size="15" fill="{t["ink2"]}">{esc(sub)}</text>')
    p.append(f'<text x="52" y="96" font-size="15" fill="{t["ink2"]}">{esc(tag)}</text>')
    # 箭頭 marker
    p.append(f'<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{t["muted"]}"/></marker></defs>')
    # 欄位
    cols = COLS_EN if LANG == "en" else COLS_ZH
    for i, (zh, en, items) in enumerate(cols):
        x, c = XS[i], t["c"][i]
        p.append(f'<rect x="{x}" y="{COL_Y}" width="{COL_W}" height="{COL_H}" rx="12" fill="{c}" fill-opacity="{t["fill_alpha"]}" stroke="{c}" stroke-width="1.6"/>')
        p.append(f'<rect x="{x+8}" y="{COL_Y}" width="{COL_W-16}" height="5" rx="2.5" fill="{c}"/>')
        p.append(f'<text x="{x+16}" y="{COL_Y+40}" font-size="17" font-weight="700" fill="{t["ink"]}">{esc(zh)}</text>')
        p.append(f'<text x="{x+COL_W-16}" y="{COL_Y+40}" font-size="11" letter-spacing="1.5" text-anchor="end" fill="{t["muted"]}">{en}</text>')
        y = COL_Y + 74
        for text, kind in items:
            if text == "—":
                p.append(f'<line x1="{x+16}" y1="{y-8}" x2="{x+COL_W-16}" y2="{y-8}" stroke="{t["hairline"]}" stroke-width="1"/>')
                y += 10
                continue
            if kind == 0:  # 主項：色點+主墨
                p.append(f'<circle cx="{x+21}" cy="{y-4}" r="3" fill="{c}"/>')
                p.append(f'<text x="{x+34}" y="{y}" font-size="13" fill="{t["ink"]}">{esc(text)}</text>')
            else:  # 次項：次墨
                p.append(f'<text x="{x+34}" y="{y}" font-size="12.5" fill="{t["ink2"]}">{esc(text)}</text>')
            y += 25 if kind == 0 else 23
    # 欄間箭頭
    ay = COL_Y + COL_H / 2
    for i in range(4):
        x1, x2 = XS[i] + COL_W + 4, XS[i + 1] - 4
        p.append(f'<line x1="{x1}" y1="{ay}" x2="{x2-8}" y2="{ay}" stroke="{t["muted"]}" stroke-width="2.2"/>')
        p.append(f'<path d="M {x2} {ay} L {x2-9} {ay-5.5} L {x2-9} {ay+5.5} Z" fill="{t["muted"]}"/>')
    # 自查迴圈：驗證層底 → 三模式底
    vx, mx = XS[3] + COL_W / 2, XS[1] + COL_W / 2
    ly = COL_Y + COL_H + 30
    p.append(f'<path d="M {vx} {COL_Y+COL_H+3} C {vx} {ly}, {mx} {ly}, {mx} {COL_Y+COL_H+14}" fill="none" stroke="{t["c"][3]}" stroke-width="2" stroke-dasharray="6 5"/>')
    p.append(f'<path d="M {mx} {COL_Y+COL_H+5} L {mx-5.5} {COL_Y+COL_H+15} L {mx+5.5} {COL_Y+COL_H+15} Z" fill="{t["c"][3]}"/>')
    p.append(f'<text x="{(vx+mx)/2}" y="{ly+13}" font-size="12.5" text-anchor="middle" fill="{t["ink2"]}">{esc("fix before delivery if the self-audit fails" if LANG == "en" else "未過自查就修正重來")}</text>')
    # 底部誠實原則
    sy = 528
    p.append(f'<rect x="52" y="{sy}" width="{W-104}" height="56" rx="10" fill="none" stroke="{t["hairline"]}" stroke-width="1.2"/>')
    p.append(f'<text x="{W/2}" y="{sy+24}" font-size="13.5" font-weight="600" text-anchor="middle" fill="{t["ink"]}">{esc("not found ≠ nonexistent    ·    memory may suspect, only evidence convicts    ·    every verdict carries its evidence level" if LANG == "en" else "查不到 ≠ 不存在    ·    記憶只能起疑，不能作證    ·    每個判定附證據層級")}</text>')
    p.append(f'<text x="{W/2}" y="{sy+44}" font-size="11.5" text-anchor="middle" fill="{t["muted"]}">{esc("a lookup that failed is never reported as a citation that does not exist" if LANG == "en" else "not found ≠ nonexistent · memory may suspect, only evidence convicts")}</text>')
    p.append('</svg>')
    return "\n".join(p)


for lang in ("zh", "en"):
    LANG = lang
    for theme in ("light", "dark"):
        suffix = f"-{theme}" if lang == "zh" else f"-en-{theme}"
        out = os.path.join(BASE, f"architecture{suffix}.svg")
        with open(out, "w", encoding="utf-8") as f:
            f.write(build(theme))
        print("written", out)
