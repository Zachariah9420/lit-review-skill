# -*- coding: utf-8 -*-
"""訊息語言機制，lit_api.py 與 cite_integrity.py 共用（純標準函式庫）。

兩支腳本各自保有自己的 MESSAGES 表（{key: {"zh": ..., "en": ...}}），這裡只放機制：
  * load_dotenv()      讀 cwd 的 .env、再讀 ~/.env（已設的環境變數不覆蓋；唯一例外是 LIT_LANG：它的空值視為未設，由 .env 補上）。
  * normalize()/get_lang()  LIT_LANG → "zh" 或 "en"；空值視為未設；不認得的值退回 zh，
                       並在 stderr 印一次 ASCII 警告（每個行程只印一次）。
  * pop_lang_arg()     把 argv 裡的 --lang X / --lang=X 取出並寫進 LIT_LANG（旗標優先於環境變數與 .env）；
                       值為空（--lang=）時等於沒給旗標：照樣從 argv 拿掉，但什麼都不寫。
  * make_translator()  產生 _T(key, **fmt)：永不拋例外，en 缺項退回 zh，無參數的模板原樣回傳。

語言在**每次呼叫**時讀取（不在 import 時），所以 .env 讀進來之後才建的 argparse 說明文字
也能本地化，同一個行程內改 os.environ 也會立刻生效。

優先順序：--lang > 環境變數 LIT_LANG > .env（cwd，再 ~）> 預設 zh。
LIT_LANG 的空值在每一層都視為未設：殼層的 LIT_LANG=""（POSIX 的 export LIT_LANG=）不擋 .env，
--lang= 不擋環境變數。其他鍵（S2_API_KEY、CROSSREF_MAILTO…）不套這條：殼層已設（即使是空字串）就不被 .env 覆蓋，
與加入英文模式之前相同。
"""
import os
import sys

SUPPORTED = ("zh", "en")
DEFAULT = "zh"
ENV_VAR = "LIT_LANG"
_warned = set()


def load_dotenv():
    # 先讀 cwd(專案層)，再讀 HOME(全域備援)；已設定的環境變數不覆蓋（LIT_LANG 的空值例外，見下）。
    # LIT_LANG 的空值算沒設：殼層的 LIT_LANG="" 若被 setdefault 當成「已設」，.env 的 LIT_LANG=en 就永遠進不來。
    # 注意：刻意不讀 skill 目錄自己的 .env——金鑰放在 skill 資料夾會隨打包外流。
    candidates = [os.path.join(os.getcwd(), ".env"),
                  os.path.join(os.path.expanduser("~"), ".env")]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    # LIT_LANG 空值視為未設是文件承諾；其他鍵維持「已設就不覆蓋」，與 b0db1e0 相同
                    if k == ENV_VAR and not os.environ.get(k):
                        os.environ[k] = v
                    else:
                        os.environ.setdefault(k, v)
        except OSError:
            pass


def normalize(raw):
    """-> (lang, rejected_value_or_None)。''/None → zh，不警告。'EN'、' en '、'en_US'、'en-GB' → en；
    'zh'、'zh-TW'、'zh_tw'、'zh-Hant' → zh。其他 → zh 並回傳被拒絕的原值（由呼叫端警告）。"""
    if raw is None:
        return DEFAULT, None
    s = str(raw).strip().lower()
    if not s:
        return DEFAULT, None
    base = s.replace("_", "-").split("-", 1)[0]
    if base in SUPPORTED:
        return base, None
    return DEFAULT, raw


def get_lang():
    """只讀 os.environ——不做檔案 I/O、不快取。"""
    lang, bad = normalize(os.environ.get(ENV_VAR))
    if bad is not None and bad not in _warned:
        _warned.add(bad)
        # ASCII 警告：cite_integrity 的 stderr 在舊版沒有 reconfigure，而且值本身可能是任何字元。
        print("lit-review: unsupported language %s (from --lang or LIT_LANG; use zh or en); using zh." % ascii(bad),
              file=sys.stderr)
    return lang


def pop_lang_arg(argv):
    """把 '--lang X' / '--lang=X' 從 argv 任何位置（子命令前後皆可）取出，寫進 os.environ['LIT_LANG']，
    回傳清掉旗標後的 argv。明確的旗標勝過環境變數與 .env。值為空（--lang= 或 --lang ""）時等於沒給旗標：
    照樣從 argv 拿掉，但不寫 LIT_LANG——寫進空字串會把環境變數與 .env 的設定一起蓋掉。"""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--lang" and i + 1 < len(argv):
            if argv[i + 1]:
                os.environ[ENV_VAR] = argv[i + 1]
            i += 2
            continue
        if a.startswith("--lang="):
            if a[len("--lang="):]:
                os.environ[ENV_VAR] = a[len("--lang="):]
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def make_translator(messages):
    def T(key, **kw):                 # 永不拋例外
        entry = messages.get(key)
        if entry is None:
            return "[%s]" % key
        tpl = entry.get(get_lang()) or entry["zh"]      # en 缺項 → zh，不會 KeyError
        if not kw:
            return tpl                # 無參數 → 原樣回傳：字面的 { } 與 % 都安全
        try:
            return tpl.format(**kw)
        except (KeyError, IndexError, ValueError):
            return tpl                # 軟性失敗；AST 測試（I18N-02）讓這條路走不到
    return T
