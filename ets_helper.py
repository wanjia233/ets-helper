# -*- coding: utf-8 -*-
"""
E听说 作业答案悬浮窗  (ets_helper)

工作原理
--------
1. 答案来源：客户端把每套作业的数据缓存到 %APPDATA%\\ETS\\<作业号>\\ 下，
   其中 content_<id>\\content.json 就是题面/原文/参考范文，template_*\\res.json
   给出各部分的顺序、名称和分值。本程序只读这些文件，不改动任何东西。
2. 当前进度来源：客户端日志 %INSTALL%\\logs\\ets_YYYY-MM-DD.log 每推进一步就记一行
   FLOW_PRAC exam_step JSON，含 set_id(作业号) / entity_id(部分) / filetype
   (action.record = 正在录音)。本程序 tail 该日志来判断"你现在做到哪一部分"。
3. 显示：一个置顶、半透明、默认鼠标穿透的悬浮窗叠在 E听说窗口右侧。

用法
----
    pythonw ets_helper.py          # 正常启动（无控制台）
    python  ets_helper.py --list   # 列出检测到的作业
    python  ets_helper.py --dump 342479   # 把某套作业的答案打印出来
    python  ets_helper.py --selftest      # 自检，结果写入 _selftest.txt
"""

import ctypes
import ctypes.wintypes as wt
import base64
import glob
import html
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from ctypes import POINTER, byref, sizeof, wintypes

APP_NAME = "E听说作业助手"
VERSION = "1.0"

# ---------------------------------------------------------------- 路径
#
# 打包成 exe 以后：
#   * 只读资源（词典索引、cmudict）在 PyInstaller 的临时解包目录 sys._MEIPASS；
#   * 配置、日志、发音缓存要写在 exe 旁边，不能写进临时目录（每次运行都不同）。
# 如果 exe 放在只读位置（比如 Program Files），自动退到 %LOCALAPPDATA%\ETSHelper。

FROZEN = bool(getattr(sys, "frozen", False))
_RES_DIR = getattr(sys, "_MEIPASS", "") or os.path.dirname(os.path.abspath(__file__))
_EXE_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
            else os.path.dirname(os.path.abspath(__file__)))


def _pick_data_dir():
    """优先把数据写在 exe 旁边（U 盘/绿色版友好），写不了就用 LOCALAPPDATA。"""
    for cand in (_EXE_DIR,
                 os.path.join(os.environ.get("LOCALAPPDATA")
                              or os.path.expanduser("~"), "ETSHelper")):
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".write_test")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return cand
        except Exception:
            continue
    return _EXE_DIR


DATA_DIR = _pick_data_dir()

HERE = _EXE_DIR                     # 兼容老代码：可写目录（配置/日志/缓存）
RES_DIR = _RES_DIR                  # 只读资源目录
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
HELPER_LOG = os.path.join(DATA_DIR, "helper.log")


def resource_path(name):
    """找一个随程序发布的只读资源：先看解包目录，再看 exe 旁边。"""
    for d in (RES_DIR, DATA_DIR):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return os.path.join(RES_DIR, name)


def writable_path(name):
    """可写文件：优先复用已有的（打包前生成的索引），否则放数据目录。"""
    p = os.path.join(DATA_DIR, name)
    if os.path.isfile(p):
        return p
    r = os.path.join(RES_DIR, name)
    if os.path.isfile(r):
        return r
    return p

# ---------------------------------------------------------------- 日志


def log(msg):
    try:
        with io.open(HELPER_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------- 配置

DEFAULT_CONFIG = {
    "cache_dir": "",          # 空 = 自动检测 %APPDATA%\ETS
    "log_dir": "",            # 空 = 从注册表/常见路径自动检测
    "hotkeys": {
        "toggle_show": "Ctrl+Alt+H",
        "next_part": "Ctrl+Alt+Right",
        "prev_part": "Ctrl+Alt+Left",
        "next_paper": "Ctrl+Alt+Down",
        "prev_paper": "Ctrl+Alt+Up",
        "toggle_click": "Ctrl+Alt+T",
        "toggle_auto": "Ctrl+Alt+A",
        "font_up": "Ctrl+Alt+=",
        "font_down": "Ctrl+Alt+-",
        "scroll_down": "Ctrl+Alt+PageDown",
        "scroll_up": "Ctrl+Alt+PageUp",
        "cycle_side": "Ctrl+Alt+M",
        "cycle_width": "Ctrl+Alt+W",
        "toggle_theme": "Ctrl+Alt+D",
        "quit": "Ctrl+Alt+Q",
    },
    "font_family": "Microsoft YaHei UI",
    "title_font": "华文中宋",
    "font_px": 19,
    "title_px": 25,
    "theme": "dark",
    "side": "right",
    "width_ratio": 0.40,
    "poll_ms": 400,
    "follow_log": True,
    "ipa_to_word": True,
    "window": {},
    "click_through_default": False,
}


def save_config(cfg):
    """把配置写回去（保留热键等所有字段）。"""
    try:
        with io.open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
        return True
    except Exception:
        log("config save failed: " + traceback.format_exc())
        return False


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with io.open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "hotkeys" and isinstance(v, dict):
                cfg["hotkeys"].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        save_config(cfg)
    except Exception:
        log("config load failed: " + traceback.format_exc())
    return cfg


# ---------------------------------------------------------------- 路径检测
#
# 作业数据的位置是固定的：%APPDATA%\ETS（客户端自己定死了）。
# 但**安装目录在哪完全说不准**：可能装在 D:\etingshuo、C:\Program Files\某中文目录、
# U 盘、甚至绿色版随便解压到一个文件夹。所以日志目录必须靠"找"。
#
# 发现策略从可靠到兜底：
#   1. 正在运行的客户端进程 -> 它的 exe 所在目录\logs
#   2. 注册表：卸载项（InstallLocation / DisplayIcon / UninstallString / Inno）、App Paths
#   3. 开始菜单 / 桌面快捷方式指向的 exe
#   4. 常见目录 + 有界全盘搜索（找 ets_*.log 这个特征文件，而不是猜目录名）
# 找到后写进 config.json，下次直接用。

CLIENT_EXES = ("etsshell.exe", "ets.exe", "etingshuo.exe", "ehear.exe")


def detect_cache_dir(cfg):
    """作业缓存目录：客户端固定写在 %APPDATA%\\ETS，这里只做兜底探测。"""
    cands = []
    if cfg.get("cache_dir"):
        cands.append(cfg["cache_dir"])
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env) or ""
        if base:
            for name in ("ETS", "ets", "E听说", "ETSCache"):
                cands.append(os.path.join(base, name))
    cands.append(os.path.expandvars(r"%APPDATA%\ETS"))
    for c in cands:
        if c and os.path.isdir(c):
            # 必须像个缓存目录（有 content_* 或词典），否则可能是空壳
            if glob.glob(os.path.join(c, "content_*")) \
                    or glob.glob(os.path.join(c, "*", "content_*")) \
                    or os.path.isdir(os.path.join(c, "pc_xst_dict")) \
                    or glob.glob(os.path.join(c, "*", "content.*")):
                return c
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return cands[0] if cands else ""


def _logs_ok(path):
    """一个目录算不算客户端的日志目录：得有 ets_YYYY-*.log。"""
    if not path or not os.path.isdir(path):
        return False
    try:
        return bool(glob.glob(os.path.join(path, "ets_*.log")))
    except Exception:
        return False


def _process_exe_paths(names):
    """列出正在运行的、名字匹配的进程的可执行文件路径。"""
    out = []
    try:
        psapi = ctypes.WinDLL("psapi")
        arr = (wt.DWORD * 4096)()
        needed = wt.DWORD()
        if not psapi.EnumProcesses(byref(arr), ctypes.sizeof(arr), byref(needed)):
            return out
        count = needed.value // ctypes.sizeof(wt.DWORD)
        for i in range(count):
            pid = arr[i]
            if not pid:
                continue
            p = _proc_exe(pid)
            if p and os.path.basename(p).lower() in names:
                out.append(p)
    except Exception:
        pass
    return out


def _registry_candidates():
    """注册表里所有能推出安装目录的值。"""
    out = []
    try:
        import winreg
    except Exception:
        return out

    appdata = os.environ.get("APPDATA") or ""
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as k:
                n = winreg.QueryInfoKey(k)[0]
                for i in range(n):
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        continue
                    try:
                        with winreg.OpenKey(k, sub) as sk:
                            try:
                                name = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                            except OSError:
                                name = ""
                            if not ("听说" in name or "ETS" in name.upper()
                                    or "ETING" in name.upper()):
                                continue
                            for val in ("InstallLocation", "Inno Setup: App Path",
                                        "InstallDir", "DisplayIcon", "UninstallString"):
                                try:
                                    raw = str(winreg.QueryValueEx(sk, val)[0])
                                except OSError:
                                    continue
                                raw = raw.strip().strip('"')
                                if ".exe" in raw.lower():
                                    raw = os.path.dirname(raw)
                                raw = raw.rstrip("\\,")
                                if raw and os.path.isdir(raw):
                                    out.append(raw)
                    except OSError:
                        continue
        except OSError:
            continue

    # App Paths：HKLM\...\App Paths\Ets.exe 的默认值就是 exe 全路径
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for arch in ("", r"\WOW6432Node"):
            for exe in ("Ets.exe", "ETSShell.exe"):
                key = r"SOFTWARE%s\Microsoft\Windows\CurrentVersion\App Paths\%s" % (arch, exe)
                try:
                    with winreg.OpenKey(hive, key) as k:
                        raw = str(winreg.QueryValueEx(k, "")[0]).strip('"')
                        if raw and os.path.isfile(raw):
                            out.append(os.path.dirname(raw))
                except OSError:
                    continue
    return out


def _shortcut_candidates():
    """开始菜单/桌面里指向客户端的快捷方式（解析 .lnk 里的路径字符串）。"""
    out = []
    roots = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env) or ""
        if base:
            roots.append(os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs"))
            roots.append(os.path.join(base, "Microsoft", "Windows", "Start Menu"))
    for env in ("USERPROFILE", "PUBLIC"):
        base = os.environ.get(env) or ""
        if base:
            roots.append(os.path.join(base, "Desktop"))
    pat = re.compile(r"[A-Za-z]:\\[^\x00\"<>|]{2,240}?\.exe", re.I)
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(".lnk"):
                    continue
                lp = os.path.join(dirpath, fn)
                try:
                    data = open(lp, "rb").read()
                except Exception:
                    continue
                hit = None
                for enc in ("utf-16-le", "mbcs"):
                    try:
                        m = pat.search(data.decode(enc, "ignore"))
                    except Exception:
                        m = None
                    if m:
                        hit = m.group(0)
                        break
                if not hit or hit in seen:
                    continue
                seen.add(hit)
                if os.path.basename(hit).lower() in CLIENT_EXES or "ets" in os.path.basename(hit).lower():
                    out.append(os.path.dirname(hit))
    return out


def _search_logs_on_disk(budget_s=6.0):
    """兜底：在可能的位置有界搜索 ets_*.log，找到就返回所在目录。

    不按目录名猜（安装目录可能叫什么都有），直接找日志文件这个特征。
    """
    t0 = time.time()
    roots = []
    for env in ("LOCALAPPDATA", "PROGRAMDATA", "ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(env)
        if v and os.path.isdir(v):
            roots.append(v)
    for drive in "CDEFGH":
        d = drive + ":\\"
        if os.path.isdir(d):
            roots.append(d)
    skip = {"windows", "$recycle.bin", "system volume information", "node_modules",
            "appdata", "winsxs", "assembly", "$windows.~bt", "temp"}

    visited = set()
    for root in roots:
        if time.time() - t0 > budget_s:
            break
        stack = [(root, 0)]
        while stack:
            if time.time() - t0 > budget_s:
                break
            d, depth = stack.pop()
            if depth > 4:
                continue
            try:
                real = os.path.realpath(d)
            except Exception:
                real = d
            if real.lower() in visited:
                continue
            visited.add(real.lower())
            try:
                if _logs_ok(d):
                    return d
            except Exception:
                pass
            try:
                entries = os.scandir(d)
            except Exception:
                continue
            with entries as it:
                for e in it:
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    name = e.name.lower()
                    if name in skip or name.startswith("."):
                        continue
                    stack.append((e.path, depth + 1))
    return ""


def _install_dir_candidates():
    """所有"可能是安装目录"的线索，去重后返回。"""
    out = []
    for p in _process_exe_paths(set(CLIENT_EXES)):
        out.append(os.path.dirname(p))
    out.extend(_registry_candidates())
    out.extend(_shortcut_candidates())
    seen = set()
    uniq = []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen and os.path.isdir(p):
            seen.add(k)
            uniq.append(p)
    return uniq


def find_log_dir(verbose=False):
    """完整探测一遍。返回 (日志目录, 依据说明)。"""
    notes = []

    # 1) 运行中的客户端
    for inst in _process_exe_paths(set(CLIENT_EXES)):
        d = os.path.join(os.path.dirname(inst), "logs")
        if _logs_ok(d):
            return d, "运行中的客户端 %s" % inst
        notes.append("进程 %s 下没有 logs" % inst)

    # 2) 注册表 / 快捷方式推出的安装目录
    for inst in _install_dir_candidates():
        for sub in ("logs", ""):
            d = os.path.join(inst, sub) if sub else inst
            if _logs_ok(d):
                return d, "安装目录线索 %s" % inst
    if _install_dir_candidates():
        notes.append("注册表/快捷方式给的目录里没有 ets_*.log")

    # 3) 全盘有界搜索
    d = _search_logs_on_disk()
    if d:
        return d, "磁盘搜索命中"

    if verbose:
        return "", "；".join(notes) or "没有任何线索"
    return "", ""


def detect_log_dir(cfg, verbose=False):
    """给外部用的入口：先看配置，再自动找，找到就记住。"""
    if cfg.get("log_dir") and _logs_ok(cfg["log_dir"]):
        return cfg["log_dir"]
    found, why = find_log_dir(verbose=verbose)
    if found:
        cfg["log_dir"] = found
        try:
            save_config(cfg)
        except Exception:
            pass
        log("log_dir 自动找到：%s（依据：%s）" % (found, why))
        return found
    if cfg.get("log_dir"):
        log("config 里的 log_dir 无效，自动探测也没找到")
    return ""


# ---------------------------------------------------------------- 文本处理

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>")
_BLOCK_END_RE = re.compile(r"(?i)<\s*/\s*(p|div|li|tr|h[1-6]|table)\s*>")


def html_to_text(s, keep_spacing=False):
    if not s:
        return ""
    s = str(s)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_END_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for line in s.split("\n"):
        line = line.strip()
        if keep_spacing:
            line = re.sub(r"[ \t]{3,}", "   ", line)
        else:
            line = re.sub(r"[ \t]+", " ", line)
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out).strip()


def squash(s, n=60):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 数据层

CN_TYPE = {
    "collector.read": "朗读",
    "collector.repeat": "听读",
    "collector.picture": "话题表达",
}


# ---------------------------------------------------------------- 音标 → 单词
#
# 单词朗读那部分题面给的是音标（/bɜːθ/ /blaɪnd/ …），照着音标念很别扭。
# 本地本来就有客户端带的词典 %APPDATA%\ETS\pc_xst_dict\pc_xst_dict.json
# （2.8 万条，含英/美音标 + 中文释义），拿它反查就能把音标还原成单词。
# 首次运行会把索引压成一个小缓存 ipa_index.json，之后秒开。

_IPA_DROP = dict.fromkeys(map(ord, "ˈˌ.‿,;:()[]/ \t\r\n'’'\""), None)
_IPA_REPL = [
    ("ɡ", "g"), ("ɹ", "r"), ("ɚ", "ər"), ("ɝ", "ɜr"), ("ʤ", "dʒ"), ("ʧ", "tʃ"),
    ("ː", ""), ("ɔː", "ɔ"), ("ɑː", "ɑ"), ("iː", "i"), ("uː", "u"), ("ɜː", "ɜ"),
    ("ʧr", "tʃr"), ("dʒr", "dʒr"),
]
IPA_RE = re.compile(r"/[^/\s]{1,40}/")
_LABEL_RE = re.compile(r"^\s*(英|美|BrE|AmE|UK|US)\s*")


def ipa_key(s):
    """把音标串压成一个可比较的 key：去重音/去长音/统一变体符号。"""
    if not s:
        return ""
    s = s.strip().strip("[]/").strip()
    s = _LABEL_RE.sub("", s)
    for a, b in _IPA_REPL:
        s = s.replace(a, b)
    s = s.translate(_IPA_DROP)
    return s.strip().lower()


def _lev(a, b, limit):
    """带上限的编辑距离，超过 limit 直接返回 limit+1。"""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    if la == 0:
        return lb
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur[j] = v
            if v < best:
                best = v
        if best > limit:
            return limit + 1
        prev = cur
    return prev[lb]


_IPA_VOWELS = set("əɪɛæɑɒɔʊuʌɜaɐɘɵeio")


def _skeleton(key):
    """音标的辅音骨架。元音写法各家差别大（ə/ɪ、长短音），辅音一致才说明是同一个词。"""
    return "".join(ch for ch in key if ch not in _IPA_VOWELS)


# CMU 词典用的是 ARPAbet 音标，这里转成 IPA（美音）
_ARPABET = {
    "AA": "ɑ", "AE": "æ", "AH": "ʌ", "AO": "ɔ", "AW": "aʊ", "AY": "aɪ",
    "EH": "ɛ", "ER": "ər", "EY": "eɪ", "IH": "ɪ", "IY": "i", "OW": "oʊ",
    "OY": "ɔɪ", "UH": "ʊ", "UW": "u",
    "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "F": "f", "G": "g",
    "HH": "h", "JH": "dʒ", "K": "k", "L": "l", "M": "m", "N": "n",
    "NG": "ŋ", "P": "p", "R": "r", "S": "s", "SH": "ʃ", "T": "t",
    "TH": "θ", "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ",
}


def _pretty_ipa(raw):
    """把各家音标串整理成统一显示格式：/ˈbɜːθ/。

    ETS 词典里是 "/bɜ:θ/"（带斜杠、用冒号表示长音），iciba 用 ' 表示重音。
    """
    s = str(raw or "").strip().strip("[]").strip()
    s = s.strip("/").strip()
    s = s.replace("'", "ˈ").replace(":", "ː")
    s = re.sub(r"\s+", " ", s)
    return "/%s/" % s if s else ""


def arpabet_to_ipa(phones):
    """ARPAbet → IPA（美音）。重音数字转成 ˈ / ˌ，显示时才像音标。"""
    out = []
    for p in phones:
        base = p.rstrip("012")
        stress = p[-1] if p[-1:] in ("0", "1", "2") else ""
        sym = ""
        if base == "AH" and stress == "0":
            sym = "ə"
        else:
            sym = _ARPABET.get(base, "")
        if base in _ARPABET:
            if stress == "1":
                out.append("ˈ")
            elif stress == "2":
                out.append("ˌ")
        out.append(sym)
    return "".join(out)


_IPA_BRACKET = re.compile(r"\[(.*?)\]")

# 教材多用人教版/外研版的英式拼写，而 cmudict 是美式。这里按词根生成英式别名，
# 让 colour / centre / travelling 这类词也能被音标查到（音标与美式同）。
_BRE_AME_ROOTS = [
    "color", "favor", "honor", "labor", "neighbor", "behavior", "humor",
    "rumor", "flavor", "harbor", "vapor", "armor", "endeavor", "splendor",
    "vigor", "odor", "tremor", "ardor", "candor", "clamor", "rancor",
    "rigor", "savor", "valor", "favourite", "colour", "behaviour",
]
_BRE_AME_PAIRS = [
    # 英式 -> 美式
    ("centre", "center"), ("metre", "meter"), ("theatre", "theater"),
    ("litre", "liter"), ("fibre", "fiber"), ("calibre", "caliber"),
    ("defence", "defense"), ("offence", "offense"), ("licence", "license"),
    ("practise", "practice"), ("programme", "program"),
    ("catalogue", "catalog"), ("dialogue", "dialog"), ("analogue", "analog"),
    ("judgement", "judgment"), ("acknowledgement", "acknowledgment"),
    ("travelled", "traveled"), ("travelling", "traveling"),
    ("traveller", "traveler"), ("cancelled", "canceled"),
    ("cancelling", "canceling"), ("modelled", "modeled"),
    ("modelling", "modeling"), ("labelled", "labeled"),
    ("labelling", "labeling"), ("signalled", "signaled"),
    ("fuelled", "fueled"), ("jewellery", "jewelry"), ("grey", "gray"),
    ("tyre", "tire"), ("aluminium", "aluminum"), ("cheque", "check"),
    ("kerb", "curb"), ("mum", "mom"), ("plough", "plow"),
]


def _bre_aliases(lower_word):
    """给一个美式拼写返回可能的英式拼写。"""
    out = []
    for root in _BRE_AME_ROOTS:
        ame = root.replace("our", "or")
        for suf in ("", "s", "ed", "ing", "ful"):
            if lower_word == ame + suf:
                out.append(root + suf)
    for bre, ame in _BRE_AME_PAIRS:
        for suf in ("", "s", "ed", "ing"):
            if lower_word == ame + suf:
                out.append(bre + suf)
    return out


def _split_ipa_variants(text):
    """把 "英 [əˈbaʊt]" / "美 [dɪˈrɛkʃən, daɪ-]" 拆成纯音标串。

    以 "-" 结尾的是"前缀变体"（如 daɪ- 表示首音节可读 /daɪ/），单独没法用，丢掉。
    """
    out = []
    if not text:
        return out
    text = re.sub(r"^\s*(英|美|BrE|AmE|UK|US)\s*", "", text.strip())
    for part in re.split(r"[,;，；]|\s{2,}", text):
        part = part.strip()
        if not part:
            continue
        m = _IPA_BRACKET.search(part)
        s = (m.group(1) if m else part).strip().strip("[]")
        if not s or s.endswith("-"):
            continue
        if not re.search(r"[a-zæɑɒɔəɜɪʊʌθðʃʒŋɡɹ]", s, re.I):
            continue
        out.append(s)
    return out


class IpaDict(object):
    """音标反查词典。第一次要解析 12MB 的原始词典，结果缓存成 ipa_index.json。"""

    CACHE_NAME = "ipa_index.json"
    WORD_IPA_NAME = "word_ipa.json"
    CACHE_VERSION = 4        # 构建逻辑变了就加一，旧缓存自动失效重建

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.dict_path = os.path.join(cache_dir, "pc_xst_dict", "pc_xst_dict.json")
        self.cache_path = os.path.join(DATA_DIR, self.CACHE_NAME)
        self.bundled_cache = os.path.join(RES_DIR, self.CACHE_NAME)
        self.word_ipa_path = os.path.join(DATA_DIR, self.WORD_IPA_NAME)
        self.bundled_word_ipa = os.path.join(RES_DIR, self.WORD_IPA_NAME)
        self.index = None
        self.trusted = set()     # 这些 key 里有客户端词典（中考/高考词表）的词
        self._word_ipa = None    # 单词 -> 带重音的音标（选中取词时用）
        self._word_audio = None  # 单词 -> 真人发音 mp3 地址
        self._buckets = None
        self._memo = {}
        self.error = ""

    # --- 载入
    def ensure(self):
        """预加载：优先用预生成的 ipa_index.json（几十毫秒），没有才现场解析词典。"""
        if self.index is not None:
            return True
        t0 = time.time()
        loaded = self._load_cache()
        src = "cache"
        if loaded is None:
            built = self._build()
            src = "dict"
            if built is None:
                self.index = {}
                return False
            index, trusted, word_ipa, word_audio = built
            self._save_cache(index, trusted, word_ipa, word_audio)
        else:
            index, trusted = loaded
        self.index = index
        self.trusted = trusted
        self._make_buckets()
        log("ipa index ready: %d keys, %d trusted (%s, %.2fs)" % (
            len(self.index), len(self.trusted), src, time.time() - t0))
        return True

    def ensure_word_ipa(self):
        """单词->音标 / 单词->发音音频。单独一个文件，用到时才读（不拖慢启动）。"""
        if self._word_ipa is not None:
            return self._word_ipa
        data = None
        audio = None
        for path in (self.word_ipa_path, self.bundled_word_ipa):
            try:
                with io.open(path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("version") == self.CACHE_VERSION:
                data = obj.get("map")
                audio = obj.get("audio")
                break
        self._word_ipa = data if isinstance(data, dict) else {}
        self._word_audio = audio if isinstance(audio, dict) else {}
        log("word_ipa loaded: %d words, %d with audio" % (
            len(self._word_ipa), len(self._word_audio)))
        return self._word_ipa

    def word_ipa(self, word):
        """取一个单词的显示用音标，没有就返回空串。"""
        w = (word or "").strip().lower()
        if not w:
            return ""
        return self.ensure_word_ipa().get(w, "")

    def word_audio(self, word):
        """取一个单词的真人发音音频地址，没有就返回空串。"""
        w = (word or "").strip().lower()
        if not w:
            return ""
        self.ensure_word_ipa()
        return (self._word_audio or {}).get(w, "")

    def _load_cache(self):
        """先读数据目录里的索引，没有就用打包时带进来的那份。"""
        for path in (self.cache_path, self.bundled_cache):
            try:
                cache_st = os.path.getmtime(path)
            except OSError:
                continue
            try:
                dict_st = os.path.getmtime(self.dict_path)
            except OSError:
                # 词典本身不在了（换机器、客户端重装/清理）也能用：索引是随程序带的
                dict_st = 0
            if dict_st and cache_st < dict_st:
                continue
            try:
                with io.open(path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("version") != self.CACHE_VERSION:
                continue
            idx = obj.get("index")
            if not isinstance(idx, dict):
                continue
            trusted = obj.get("trusted") or {}
            if not isinstance(trusted, dict):
                trusted = {}
            if not trusted:      # 老缓存没有这个字段：全当可信，退化成"精确优先"
                trusted = {k: len(v) for k, v in idx.items() if isinstance(v, list)}
            return idx, trusted
        return None

    def _save_cache(self, index, trusted, word_ipa=None, word_audio=None):
        try:
            with io.open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"version": self.CACHE_VERSION, "index": index,
                           "trusted": trusted}, f,
                          ensure_ascii=False, separators=(",", ":"))
        except Exception:
            log("ipa cache save failed: " + traceback.format_exc())
        if word_ipa is not None:
            try:
                with io.open(self.word_ipa_path, "w", encoding="utf-8") as f:
                    json.dump({"version": self.CACHE_VERSION, "map": word_ipa,
                               "audio": word_audio or {}}, f,
                              ensure_ascii=False, separators=(",", ":"))
            except Exception:
                log("word_ipa save failed: " + traceback.format_exc())

    def _build(self):
        """合并三本词库构建索引（优先级从高到低）：

        1. 客户端自带词典 pc_xst_dict.json（2.8 万条，英/美双音标）
        2. 客户端 iciba 词库 worddict_data.json（4.5 千条，含部分带 "-" 的前缀变体）
        3. cmudict.dict（13.5 万条，ARPAbet，覆盖全部初高中课标词汇）

        同一个音标 key 下，先来的词排在前面，所以常见释义由前两本决定。
        同时产出 word_ipa（单词 -> 带重音的音标），供选中取词弹窗用。
        """
        index = {}
        added = {}
        seen = {}          # key -> 小写词集合，避免 Christmas / christmas 这种重复
        word_keys = {}     # 小写词 -> 它的第一个 key（补派生形式用）
        trusted = {}       # key -> 该 key 里来自客户端词表的词数（这些词排在列表最前）
        word_ipa = {}      # 小写词 -> "/音标/"（显示用，带重音符号）
        word_audio = {}    # 小写词 -> 真人发音 mp3 地址

        def take_audio(word, *urls):
            lw = word.lower()
            if lw in word_audio:
                return
            for u in urls:
                u = (u or "").strip()
                if u.startswith("http"):
                    word_audio[lw] = u
                    return

        def push(word, ipa_raw, src):
            k = ipa_key(ipa_raw)
            if not k:
                return
            lst = index.setdefault(k, [])
            low = seen.setdefault(k, set())
            lw = word.lower()
            ok_src = src in ("ets", "iciba")
            if ok_src and lw not in word_ipa:
                pretty = _pretty_ipa(ipa_raw)
                if pretty:
                    word_ipa[lw] = pretty
            if lw in low:
                return
            if len(lst) >= 6:
                return
            lst.append(word)
            low.add(lw)
            word_keys.setdefault(lw, k)
            if ok_src:
                trusted[k] = trusted.get(k, 0) + 1
            added[src] = added.get(src, 0) + 1

        # 1) 客户端词典
        n1 = 0
        if os.path.isfile(self.dict_path):
            try:
                with io.open(self.dict_path, "r", encoding="utf-8-sig") as f:
                    entries = json.load(f)
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    w = (e.get("Word") or "").strip()
                    if not w or len(w) > 40:
                        continue
                    take_audio(w, e.get("AudioUrl_EN"), e.get("AudioUrl_US"))
                    for field in ("Spell_EN", "Spell_US"):
                        m = _IPA_BRACKET.search(e.get(field) or "")
                        if m:
                            push(w, m.group(1), "ets")
                            n1 += 1
            except Exception:
                log("ets dict parse failed: " + traceback.format_exc())

        # 2) iciba 词库（base64 的 "英 [..]" / "美 [..]"）
        n2 = 0
        for path in (os.path.join(self.cache_dir, "common", "material", "word",
                                  "worddict_data.json"),):
            if not os.path.isfile(path):
                continue
            try:
                raw = io.open(path, "r", encoding="utf-8", errors="replace").read()
                body = raw[raw.find("`") + 1: raw.rfind("`")]
                data = json.loads(body)
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    for w, rec in item.items():
                        if not isinstance(rec, dict):
                            continue
                        take_audio(w, rec.get("url_en"), rec.get("url_us"))
                        for field in ("spell_en", "spell_us"):
                            b64 = rec.get(field) or ""
                            try:
                                text = base64.b64decode(b64).decode("utf-8", "replace")
                            except Exception:
                                continue
                            for ipa in _split_ipa_variants(text):
                                push(w, ipa, "iciba")
                                n2 += 1
            except Exception:
                log("iciba dict parse failed: " + traceback.format_exc())

        # 3) cmudict（ARPAbet -> IPA，美音；覆盖面最广）
        n3 = 0
        cmu = resource_path("cmudict.dict")
        if os.path.isfile(cmu):
            try:
                with io.open(cmu, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith(";;;"):
                            continue
                        parts = line.split(" ")
                        if len(parts) < 2:
                            continue
                        w = parts[0]
                        if not re.match(r"^[a-z][a-z'\-]{1,20}$", w):
                            continue           # 跳过 a(2)、缩写、标点词
                        if w.startswith("'") or w.endswith("'"):
                            continue           # 所有格/缩写，不参与反查
                        ipa = arpabet_to_ipa(parts[1:])
                        if ipa:
                            word_ipa.setdefault(w, _pretty_ipa(ipa))
                            push(w, ipa, "cmu")
                            n3 += 1
            except Exception:
                log("cmudict parse failed: " + traceback.format_exc())

        # 4) 补派生形式：英式 -ise 家族 + 连字符合成词
        n4 = 0

        def push_key(word, k):
            if not k:
                return False
            lst = index.setdefault(k, [])
            low = seen.setdefault(k, set())
            lw = word.lower()
            if lw in low or len(lst) >= 6:
                return False
            lst.append(word)
            low.add(lw)
            return True

        for w in list(word_keys.keys()):
            base_k = word_keys[w]
            if "-" in w and len(w) <= 24:
                keys = [word_keys.get(p) for p in w.split("-")]
                if all(keys) and push_key(w, "".join(keys)):
                    n4 += 1
                continue
            # 英式拼写别名（colour / centre / travelling / organise …）
            for bre in _bre_aliases(w):
                if push_key(bre, base_k):
                    n4 += 1
                    if w in word_ipa:
                        word_ipa.setdefault(bre, word_ipa[w])
            # 美式 -ize 家族 -> 英式 -ise 家族
            for suf, rep in (("ization", "isation"), ("izing", "ising"),
                             ("ized", "ised"), ("izer", "iser"), ("ize", "ise")):
                if w.endswith(suf):
                    uk = w[: -len(suf)] + rep
                    if push_key(uk, base_k):
                        n4 += 1
                        if w in word_ipa:
                            word_ipa.setdefault(uk, word_ipa[w])
                    break

        trusted = {k: v for k, v in trusted.items() if k in index and v > 0}
        self.error = ""
        log("ipa build: ets=%d iciba=%d cmu=%d derived=%d keys=%d trusted=%d "
            "word_ipa=%d audio=%d" % (
                n1, n2, n3, n4, len(index), len(trusted),
                len(word_ipa), len(word_audio)))
        if not index:
            self.error = "三本词库都没读到内容"
            return None
        return index, trusted, word_ipa, word_audio

    def _make_buckets(self):
        self._buckets = {}
        for k in self.index:
            self._buckets.setdefault(k[:1], []).append(k)

    # --- 查询
    def lookup(self, ipa, limit=3):
        if self.index is None and not self.ensure():
            return []
        k = ipa_key(ipa)
        if not k:
            return []
        if k in self._memo:
            return self._memo[k]

        exact = list(self.index.get(k, []))
        trusted = self.trusted
        if k in trusted:
            # 可信 key：只取最前面那几个客户端词表里的词，别把 cmudict 的生僻同音词带出来
            words = exact[:trusted[k]]
        else:
            # 精确命中只来自 cmudict 时，别急着用它：先去模糊匹配里找客户端词表的词，
            # 否则 /ˈkæpɪtəl/ 会被冷僻的 capitol 顶掉常见的 capital。
            scored = self._ranked(k)
            picked = [s for s in scored if s[3] in trusted]
            if picked:
                best = (picked[0][0], picked[0][1])
                keys = [s[3] for s in picked if (s[0], s[1]) == best][:2]
                words = []
                for kk in keys:
                    words.extend(self.index.get(kk, []))
            else:
                words = exact
                if not words and scored:
                    best = (scored[0][0], scored[0][1])
                    keys = [s[3] for s in scored if (s[0], s[1]) == best][:2]
                    for kk in keys:
                        words.extend(self.index.get(kk, []))
        out = []
        for w in words:
            if w not in out:
                out.append(w)
        out = out[:limit]
        self._memo[k] = out
        return out

    def _ranked(self, key):
        """两级模糊匹配，返回按 (辅音差, 编辑距离, 长度差, key) 排序的候选。"""
        sk = _skeleton(key)
        scored = self._scan(key, sk, 1)
        if not scored:
            scored = [s for s in self._scan(key, sk, 2) if s[0] == 0]
        scored.sort()
        return scored

    def _fuzzy(self, key):
        """兼容旧调用：只要词。"""
        scored = self._ranked(key)
        if not scored:
            return []
        best = (scored[0][0], scored[0][1])
        keys = [s[3] for s in scored if (s[0], s[1]) == best][:2]
        out = []
        for c in keys:
            for w in self.index.get(c, []):
                if w not in out:
                    out.append(w)
        return out

    def _scan(self, key, sk_q, limit):
        cands = [c for c in self._buckets.get(key[:1], [])
                 if abs(len(c) - len(key)) <= limit]
        scored = []
        for c in cands:
            d = _lev(key, c, limit)
            if d > limit:
                continue
            sk = _lev(sk_q, _skeleton(c), 4)
            scored.append((sk, d, abs(len(c) - len(key)), c))
        return scored


class Part(object):
    """作业的一部分（一个 content_xxx）。"""

    def __init__(self, index, content_id):
        self.index = index            # 0-based
        self.content_id = content_id  # "338224"
        self.name = ""                # "单词朗读"
        self.score = ""               # "4"
        self.ctype = ""               # collector.read
        self.qtype = ""               # read_chapter / read_sentence / topic
        self.blocks = []              # [{"kind": "h"/"t"/"s", "text": str, "n": int}]
        self.error = ""

    @property
    def label(self):
        t = self.name or CN_TYPE.get(self.ctype, "第%d部分" % (self.index + 1))
        score = "（%s分）" % self.score if self.score else ""
        return "%s%s" % (t, score)

    def active_block_index(self, sent_no):
        """把 1-based 句号映射到 blocks 里的下标。"""
        if not sent_no:
            return -1
        n = 0
        for i, b in enumerate(self.blocks):
            if b["kind"] == "s":
                n += 1
                if n == sent_no:
                    return i
        return -1

    def question_block_index(self, qno):
        """角色扮演：题号 -> blocks 下标。"""
        if not qno:
            return -1
        for i, b in enumerate(self.blocks):
            if b["kind"] == "q" and int(b.get("n") or 0) == int(qno):
                return i
        return -1

    def plain_text(self):
        lines = []
        for b in self.blocks:
            if b["kind"] == "h":
                lines.append("【%s】" % b["text"])
            elif b["kind"] == "s":
                lines.append("%d. %s" % (b["n"], b["text"]))
            elif b["kind"] == "w":
                word = b["text"] or "（未收录）"
                lines.append("%s  %s" % (word, b.get("sub", "")))
            elif b["kind"] == "q":
                tag = "你要提问" if b.get("role") == "ask" else "你要回答"
                lines.append("\n[%d] %s" % (b["n"], tag))
                if b.get("head"):
                    lines.append("    " + b["head"])
                for c in b.get("cans") or []:
                    lines.append("    * " + c)
            else:
                lines.append(b["text"])
        return "\n".join(lines)


def _convert_ipa_text(text, ipadict):
    """把含音标的文本拆成块：音标 → 单词（kind="w"），其余原样（kind="t"）。

    单词朗读的题面就是一堆 /bɜːθ/ /blaɪnd/ …，一个个反查成单词后一行一个更好念。
    """
    out = []
    found = False
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        pos = 0
        hits = list(IPA_RE.finditer(line))
        if not hits:
            out.append({"kind": "t", "text": line})
            continue
        for m in hits:
            pre = line[pos:m.start()].strip()
            if pre:
                out.append({"kind": "t", "text": pre})
            tok = m.group(0)
            words = ipadict.lookup(tok)
            out.append({"kind": "w",
                        "text": " · ".join(words) if words else "",
                        "sub": tok})
            pos = m.end()
            found = True
        tail = line[pos:].strip()
        if tail:
            out.append({"kind": "t", "text": tail})
    return out if found else []


def _extract_blocks(ctype, info, ipadict=None):
    """按题型从 content.json 的 info 里抽出可读的答案块。"""
    blocks = []

    def add(kind, text):
        text = (text or "").strip()
        if text:
            blocks.append({"kind": kind, "text": text})

    if ctype == "collector.read":
        text = html_to_text(info.get("value"), keep_spacing=True)
        if ipadict is not None and text:
            converted = _convert_ipa_text(text, ipadict)
            if converted:
                blocks.extend(converted)
        if not blocks:
            add("t", text)
        if not blocks:
            add("t", html_to_text(info.get("translate")))

    elif ctype == "collector.repeat":
        fj = info.get("fj") or []
        sents = []
        for item in fj:
            if not isinstance(item, dict):
                continue
            v = html_to_text(item.get("value") or item.get("ai") or "")
            if v:
                sents.append(v)
        if sents:
            for i, v in enumerate(sents, 1):
                blocks.append({"kind": "s", "text": v, "n": i})
        else:
            add("t", html_to_text(info.get("value"), keep_spacing=True))

    elif ctype == "collector.picture":
        std = info.get("std") or []
        ref = ""
        if isinstance(std, list) and std and isinstance(std[0], dict):
            ref = html_to_text(std[0].get("value") or std[0].get("ai") or "")
        kp = re.sub(r"(?i)<\s*/\s*br\s*>", "\n", str(info.get("keypoint") or ""))
        kp = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", kp)
        kp = html_to_text(kp)
        topic = html_to_text(info.get("value"))
        if ref:
            add("h", "参考范文（照着说）")
            # 打上 role 标记：录音时只高亮这一段，要点/题目不高亮
            blocks.append({"kind": "t", "text": ref, "role": "ref"})
        if kp:
            add("h", "要点提示")
            add("t", kp)
        if topic:
            add("h", "题目")
            add("t", topic)

    elif ctype == "collector.3q5a":
        # 角色扮演：3 个"你要提问" + 5 个"你要回答"
        qs = info.get("question") or []
        for i, q in enumerate(qs):
            if not isinstance(q, dict):
                continue
            role = (q.get("role") or "").strip().lower()
            ask = html_to_text(q.get("ask") or "")
            cands = []
            for s in (q.get("std") or []):
                if isinstance(s, dict):
                    v = html_to_text(s.get("value") or s.get("ai") or "")
                    if v and v not in cands:
                        cands.append(v)
                if len(cands) >= 3:
                    break
            if not ask and not cands:
                continue
            try:
                n = int(q.get("xh") or (i + 1))
            except Exception:
                n = i + 1
            blocks.append({
                "kind": "q",
                "n": n,
                "role": "ask" if role == "a" else "answer",
                "head": ask,
                "cans": cands,
            })
        transcript = html_to_text(info.get("value"))
        if transcript:
            add("h", "听力原文（参考）")
            add("t", transcript)

    if not blocks:
        # 兜底：把长得像答案的字符串都捞出来
        found = []

        def walk(o, depth=0):
            if depth > 4 or len(found) > 30:
                return
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, str) and k in ("value", "ai", "text", "content", "answer", "key"):
                        t = html_to_text(v)
                        if len(t) >= 25:
                            found.append(t)
                    else:
                        walk(v, depth + 1)
            elif isinstance(o, list):
                for v in o:
                    walk(v, depth + 1)

        walk(info)
        seen = set()
        for t in found:
            if t in seen:
                continue
            seen.add(t)
            add("t", t)

    return blocks


class Paper(object):
    def __init__(self, pid, path):
        self.pid = pid
        self.path = path
        self.mtime = 0.0
        self.parts = []
        self.template = ""
        self.total_score = ""
        self.error = ""

    @property
    def title(self):
        return self.title_with("")

    def title_with(self, name=""):
        """标题只放作业名（有的话），部分/分数放到副标题，避免占三行。"""
        return (name.strip() if name else ("作业 %s" % self.pid))

    def part_by_content_id(self, cid):
        for p in self.parts:
            if p.content_id == str(cid):
                return p
        return None


class PaperStore(object):
    def __init__(self, cache_dir, ipadict=None):
        self.cache_dir = cache_dir
        self.papers = []
        self._cache = {}
        self.ipadict = ipadict

    # ---------- 扫描
    def scan(self):
        papers = []
        if not self.cache_dir or not os.path.isdir(self.cache_dir):
            self.papers = []
            return []
        for name in os.listdir(self.cache_dir):
            full = os.path.join(self.cache_dir, name)
            if not os.path.isdir(full) or not name.isdigit():
                continue
            if not glob.glob(os.path.join(full, "content_*")):
                continue
            p = Paper(name, full)
            try:
                self._load(p)
            except Exception as e:
                p.error = str(e)
                log("load paper %s failed: %s" % (name, traceback.format_exc()))
            papers.append(p)
        papers.sort(key=lambda x: x.mtime, reverse=True)
        self.papers = papers
        return papers

    def _load(self, paper):
        # 模板目录：template_*
        tdirs = [d for d in glob.glob(os.path.join(paper.path, "template_*")) if os.path.isdir(d)]
        meta = []
        for td in sorted(tdirs):
            res = os.path.join(td, "res.json")
            if os.path.isfile(res):
                try:
                    with io.open(res, "r", encoding="utf-8-sig") as f:
                        j = json.load(f)
                    meta = (j.get("exam_type_list") or []) if isinstance(j, dict) else []
                    paper.total_score = str(j.get("set_score") or "") if isinstance(j, dict) else ""
                    paper.template = os.path.basename(td)
                    break
                except Exception:
                    continue

        cdirs = []
        for d in glob.glob(os.path.join(paper.path, "content_*")):
            if not os.path.isdir(d):
                continue
            m = re.match(r"content_(\d+)$", os.path.basename(d))
            if m:
                cdirs.append((int(m.group(1)), d))
        cdirs.sort(key=lambda x: x[0])

        for i, (cid, d) in enumerate(cdirs):
            part = Part(i, str(cid))
            m = meta[i] if i < len(meta) else {}
            if isinstance(m, dict):
                part.name = (m.get("exam_type_name") or "").strip()
                part.score = str(m.get("exam_type_score") or "").strip()
                part.ctype = (m.get("exam_type_collect") or "").strip()
                try:
                    ql = (m.get("exam_list") or [])[0].get("question_list") or []
                    if ql:
                        part.qtype = (ql[0].get("evaluation_type") or "").strip()
                except Exception:
                    pass
            payload = None
            for fname in ("content.json", "content2.json"):
                fp = os.path.join(d, fname)
                if os.path.isfile(fp):
                    try:
                        with io.open(fp, "r", encoding="utf-8-sig") as f:
                            payload = json.load(f)
                        break
                    except Exception as e:
                        part.error = str(e)
            if not isinstance(payload, dict):
                part.blocks = [{"kind": "t", "text": "（这个部分的数据读不出来）"}]
                paper.parts.append(part)
                continue
            ctype = (payload.get("structure_type") or part.ctype or "").strip()
            part.ctype = ctype or part.ctype
            info = payload.get("info") or {}
            part.blocks = _extract_blocks(part.ctype, info if isinstance(info, dict) else {},
                                          self.ipadict)
            if not part.blocks:
                part.blocks = [{"kind": "t", "text": "（这一部分没找到文字内容）"}]
            paper.parts.append(part)

        mt = 0.0
        for root, dirs, files in os.walk(paper.path):
            for fn in files:
                try:
                    mt = max(mt, os.path.getmtime(os.path.join(root, fn)))
                except OSError:
                    pass
        paper.mtime = mt

    def find(self, pid):
        for p in self.papers:
            if p.pid == str(pid):
                return p
        return None


# ---------------------------------------------------------------- 日志跟随

_JSON_RE = re.compile(r"\[FLOW_PRAC\] (\{.*)$")
_PAGE_ENTER_RE = re.compile(r"\[UI_PAGE\] enter (.+)$")
_PAGE_LEAVE_RE = re.compile(r"\[UI_PAGE\] leave (.+)$")
_SENT_RE = re.compile(r"fj(\d+)\.mp3", re.I)
# 角色扮演的每题音频：ques1aswaudio.mp3 / ques4askaudio.mp3，编号就是题号
_QUES_RE = re.compile(r"ques(\d+)(?:ask|asw)audio\.mp3", re.I)
# 步骤 order 形如 51_custom_order_612640_4_51，中间那个数字就是题号（0 表示导语）
_ORDER_Q_RE = re.compile(r"_custom_order_\d+_(\d+)_\d+$")
# 录音步骤的文件名形如 {{st2}}_4，后缀也是题号
_REC_Q_RE = re.compile(r"\{\{st\d+\}\}_(\d+)$")
_TITLE_RE = re.compile(r"([^,{}\"]{2,80}?),\s*set_id:\s*(\d+)")


class LogWatcher(object):
    """tail ets_*.log，从 exam_step 行推断当前作业 / 部分 / 播放到第几句。"""

    def __init__(self, log_dir):
        self.log_dir = log_dir
        self.path = None
        self.pos = 0
        self.buf = b""
        self.state = {
            "set_id": None, "entity_id": None, "filetype": None,
            "filename": "", "sent": 0, "qn": 0, "recording": False,
            "active": False, "page": "", "ts": 0.0,
            "titles": {},          # set_id -> 作业名（从日志里捞）
        }
        self._papers_seen = []

    def _candidates(self):
        if not self.log_dir or not os.path.isdir(self.log_dir):
            return []
        files = glob.glob(os.path.join(self.log_dir, "ets_*.log"))
        files.sort(key=lambda p: os.path.basename(p))
        return files

    def attach(self):
        """挑选要跟随的日志文件，并从尾部往前找到最后一条 exam_step 播种状态。"""
        cands = self._candidates()
        if not cands:
            self.path = None
            return False
        # 从最新的日志往前找，找到含 exam_step 的文件就播种（今天的日志常常还没做题）
        for f in reversed(cands[-4:]):
            step = self._seed_backwards(f)
            if step:
                self.state["set_id"] = step.get("set_id") or self.state["set_id"]
                self.state["entity_id"] = step.get("entity_id") or self.state["entity_id"]
                self.state["seed_file"] = os.path.basename(f)
                log("seeded from %s: %s/%s" % (
                    os.path.basename(f), self.state["set_id"], self.state["entity_id"]))
                break
        self.path = cands[-1]
        try:
            self.pos = os.path.getsize(self.path)
        except OSError:
            self.pos = 0
        self.buf = b""
        log("log watcher attached: %s @%d" % (self.path, self.pos))
        return True

    def _seed_backwards(self, path, max_bytes=8 * 1024 * 1024, chunk=256 * 1024):
        """从文件末尾按块向前读：找最后一条 exam_step，同时回填作业名。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        pos = size
        carry = b""
        read = 0
        found = None
        budget = 0
        fh = None
        try:
            fh = open(path, "rb")
            while pos > 0 and read < max_bytes:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + carry
                lines = data.split(b"\n")
                carry = lines[0]
                read += step
                for raw in reversed(lines[1:]):
                    if b"set_id: " in raw and len(raw) < 4000:
                        text = raw.decode("utf-8", "replace")
                        tm = _TITLE_RE.search(text)
                        if tm:
                            raw_name = tm.group(1).strip()
                            name = raw_name.split("-")[-1].strip() if "-" in raw_name else raw_name
                            name = name.replace("页面跳转", "").strip(" :")
                            if name and len(name) <= 40:
                                self.state["titles"].setdefault(tm.group(2), name)
                    if b"exam_step" in raw:
                        try:
                            text = raw.decode("utf-8", "replace")
                        except Exception:
                            text = ""
                        m = _JSON_RE.search(text)
                        if m:
                            try:
                                j = json.loads(m.group(1))
                            except Exception:
                                j = None
                            if j and j.get("set_id"):
                                found = j
                                # 不立刻返回：作业名那行通常在 exam_step 之前，
                                # 再往前扫一段才能捞到它。
                                budget = 4000
                                continue
                    if found:
                        budget -= 1
                        if budget <= 0:
                            return found
        except OSError:
            return found
        finally:
            if fh:
                fh.close()
        return found

    def poll(self):
        if not self.path:
            return self.state
        # 跨天：切换到更新的日志
        cands = self._candidates()
        if cands and cands[-1] != self.path:
            self.path = cands[-1]
            try:
                self.pos = os.path.getsize(self.path)
            except OSError:
                self.pos = 0
            self.buf = b""
            log("log rotated -> %s" % self.path)
            return self.state
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return self.state
        if size < self.pos:  # 文件被截断/重写
            self.pos = 0
            self.buf = b""
        if size == self.pos:
            return self.state
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read(size - self.pos)
                self.pos = fh.tell()
        except OSError:
            return self.state
        self._feed(chunk)
        return self.state

    def _feed(self, chunk):
        if not chunk:
            return
        data = self.buf + chunk
        parts = data.split(b"\n")
        self.buf = parts.pop() if parts else b""
        if len(self.buf) > 512 * 1024:  # 防止异常长行撑爆
            self.buf = b""
        for raw in parts:
            if not raw:
                continue
            self._line(raw)

    def _line(self, raw):
        try:
            line = raw.decode("utf-8", "replace")
        except Exception:
            return
        if len(line) > 8000:
            return
        st = self.state

        # 作业真名：日志里形如 "听说模拟-区域精选-佛山市高一定制题型模拟试题6,set_id: 306833"
        # 放在最前面处理，进入/离开作业页的行里都带这个名字。
        tm = _TITLE_RE.search(line)
        if tm:
            raw_name = tm.group(1).strip()
            sid = tm.group(2)
            name = raw_name.split("-")[-1].strip() if "-" in raw_name else raw_name
            name = name.replace("页面跳转", "").strip(" :")
            if name and len(name) <= 40:
                st["titles"][sid] = name

        m = _JSON_RE.search(line)
        if m:
            try:
                j = json.loads(m.group(1))
            except Exception:
                return
            sid = str(j.get("set_id") or "")
            eid = str(j.get("entity_id") or "")
            if sid:
                st["set_id"] = sid
            if eid and eid != st.get("entity_id"):
                # 换到另一部分：上一部分的句子号/题号作废
                st["sent"] = 0
                st["qn"] = 0
            if eid:
                st["entity_id"] = eid
            st["filetype"] = str(j.get("filetype") or "")
            st["filename"] = str(j.get("filename") or "")
            st["fileorder"] = str(j.get("order") or "")
            st["ts"] = time.time()
            st["recording"] = st["filetype"] == "action.record"
            if st["set_id"] and st["set_id"] not in self._papers_seen:
                self._papers_seen.append(st["set_id"])
            fn = st["filename"]
            sm = _SENT_RE.search(fn)
            if sm:
                st["sent"] = int(sm.group(1))
            elif "content.mp3" in fn.lower():
                st["sent"] = 0

            # 角色扮演的题号：三路信号，优先级从早到晚
            #   1) 步骤 order 里的题号 —— 题目页面一出现就知道，最早最准
            #   2) quesN…audio 音频编号
            #   3) 录音文件名后缀 {{st2}}_N
            # 只用 1) 也不够稳（导语步骤是 0），所以后两者做补充/校订。
            om = _ORDER_Q_RE.search(st["fileorder"])
            if om and int(om.group(1)) > 0:
                st["qn"] = int(om.group(1))
            qm = _QUES_RE.search(fn)
            if qm:
                st["qn"] = int(qm.group(1))
            rm = _REC_Q_RE.search(fn)
            if rm and st["recording"]:
                # 提问环节的音频是在录完之后才播的，靠音频会晚一题；
                # 录音这一步的后缀才是"现在这题"。
                st["qn"] = int(rm.group(1))
            # 注意：录音这一步要保留上一句的编号 —— 听读是"先播第 N 句，然后你复述"，
            # 录音时把 sent 清零就会导致录音期间高亮消失。
            return

        m = _PAGE_ENTER_RE.search(line)
        if m:
            st["page"] = m.group(1).strip()
            st["active"] = True
            st["ts"] = time.time()
            return

        m = _PAGE_LEAVE_RE.search(line)
        if m:
            st["page"] = ""
            st["active"] = False
            st["recording"] = False
            st["ts"] = time.time()
            return

        if "[FLOW_PRAC]" in line and ("录音结束" in line or "练习流程结束" in line):
            st["recording"] = False


# ---------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
LWA_ALPHA = 0x00000002


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT),
                ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
                ("time", wt.DWORD), ("pt", POINT), ("lPrivate", wt.DWORD)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _frame_hwnd(root):
    """Tk 的 toplevel 在 Windows 上有一层 wrapper 窗口；扩展样式必须设在外层
    （wm frame），设在 winfo_id() 那个 WS_CHILD 内层上没有任何效果。"""
    try:
        return int(root.wm_frame(), 16)
    except Exception:
        return root.winfo_id()

# 显式声明，避免 ctypes 默认 int 传参在 64 位下截断句柄
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD]
user32.SetLayeredWindowAttributes.restype = wt.BOOL
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetWindowRect.argtypes = [wt.HWND, POINTER(RECT)]
user32.GetWindowLongW.restype = ctypes.c_long


def _proc_exe(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


ETS_EXES = {"etsshell.exe", "ets.exe", "etingshuo.exe"}


def find_ets_window():
    """找到 E听说 主窗口矩形。返回 (hwnd, (l, t, r, b)) 或 (None, None)。"""
    best = {"area": 0, "hwnd": None, "rect": None}

    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, byref(pid))
            if not pid.value:
                return True
            exe = _proc_exe(pid.value).lower()
            if exe not in ETS_EXES:
                return True
            r = RECT()
            if not user32.GetWindowRect(hwnd, byref(r)):
                return True
            w, h = r.right - r.left, r.bottom - r.top
            if w < 300 or h < 200:
                return True
            area = w * h
            if area > best["area"]:
                best.update(area=area, hwnd=hwnd,
                            rect=(r.left, r.top, r.right, r.bottom))
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return best["hwnd"], best["rect"]


def set_click_through(hwnd, enable):
    """置为工具窗+置顶，可选鼠标穿透。

    实测结论（必须记下来，否则以后又会踩）：
      * 带 WS_EX_LAYERED 且用 SetLayeredWindowAttributes 设成 **255（不透明）** 时，
        背景和文字都正常绘制，同时 WS_EX_TRANSPARENT 让点击穿透生效 —— 这是唯一
        既能画出来又能穿透的组合；
      * 只要 alpha < 255，Tk 画的背景填充就会整块消失，只剩文字，变成"半透明幽灵窗"；
      * 不加 LAYERED 时背景正常，但 WS_EX_TRANSPARENT 不生效，会挡住鼠标。
    所以这里固定 alpha=255（不透明），不做半透明。
    """
    if not hwnd:
        return
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    want = ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW
    if enable:
        want |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    else:
        want &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    if want != ex:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, want)
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)


def dpi_scale():
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        pass
    try:
        hdc = user32.GetDC(None)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        user32.ReleaseDC(None, hdc)
        return (dpi or 96) / 96.0
    except Exception:
        return 1.0


def keep_topmost(hwnd):
    if not hwnd:
        return
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def set_dpi_aware():
    """按显示器实际 DPI 工作，文字才清晰（否则 150% 缩放下会被位图拉伸变糊）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------------------------------------------------------- 热键

VK = {
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pageup": 0x21, "pagedown": 0x22, "home": 0x24, "end": 0x23,
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09,
    "insert": 0x2D, "delete": 0x2E, "back": 0x08,
    "=": 0xBB, "-": 0xBD, "[": 0xDB, "]": 0xDD, ";": 0xBA,
    "`": 0xC0, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
}


def parse_hotkey(spec):
    mods = 0
    vk = None
    for tok in re.split(r"\s*\+\s*", spec.strip()):
        low = tok.lower()
        if low in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif low == "alt":
            mods |= MOD_ALT
        elif low == "shift":
            mods |= MOD_SHIFT
        elif low in VK:
            vk = VK[low]
        elif len(low) == 1:
            vk = ord(low.upper())
        elif re.match(r"^f\d{1,2}$", low):
            vk = 0x70 + int(low[1:]) - 1
        elif low.startswith("vk_"):
            vk = int(low[3:], 16)
    if vk is None:
        return None
    return (mods | MOD_NOREPEAT, vk)


class HotkeyManager(object):
    def __init__(self, root):
        self.root = root
        self.map = {}
        self.byname = {}      # action -> 实际注册成功的按键串
        self._next = 1
        self.failed = []

    def register(self, action, spec):
        parsed = parse_hotkey(spec)
        if not parsed:
            self.failed.append((action, spec, "无法解析"))
            return False
        mods, vk = parsed
        hid = self._next
        if user32.RegisterHotKey(None, hid, mods, vk):
            self._next += 1
            self.map[hid] = action
            self.byname[action] = spec
            return True
        return False

    def register_with_fallback(self, action, fn, spec):
        """默认键被占用时，依次试 Ctrl+Shift+键、Alt+Shift+键。"""
        cands = [spec]
        m = re.match(r"^(Ctrl|Alt|Shift)(\+(?:Ctrl|Alt|Shift))*\+(.+)$", spec.strip())
        if m:
            key = spec.strip().split("+")[-1]
            for mods in ("Ctrl+Shift", "Alt+Shift", "Ctrl+Alt+Shift"):
                alt = mods + "+" + key
                if alt not in cands:
                    cands.append(alt)
        self._fns = getattr(self, "_fns", {})
        self._fns[action] = fn
        for cand in cands:
            if self.register(action, cand):
                if cand != spec:
                    log("hotkey %s 改用备用键 %s（原键 %s 被占用）" % (action, cand, spec))
                return True
        self.failed.append((action, spec, "全部候选键都被占用"))
        return False

    def pump(self):
        msg = MSG()
        dispatch = getattr(self, "_fns", {})
        while user32.PeekMessageW(byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_HOTKEY:
                action = self.map.get(int(msg.wParam))
                fn = dispatch.get(action) if action else None
                if fn:
                    try:
                        fn()
                    except Exception:
                        log("hotkey action failed: " + traceback.format_exc())
            else:
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
        self.root.after(40, self.pump)


# ---------------------------------------------------------------- 悬浮窗

import tkinter as tk  # noqa: E402  (放到 win32 之后，避免 DPI 设置前初始化)

# ---------------------------------------------------------------- 主题
#
# 两套配色，键名一致，运行时整体切换（不用重启）。
PALETTES = {
    "dark": {
        "bg": "#0d1218", "bg2": "#141b23", "bar": "#101820",
        "btn": "#1b2836", "btn_hi": "#24384a", "border": "#22303c",
        "fg": "#e6edf3", "dim": "#7d8b99",
        "accent": "#5fc9ff", "warm": "#ffd479", "green": "#6ee7a8",
        "hl": "#1f4b63", "on_hl": "#ffffff",
        "rec": "#12354a", "on_rec": "#ffffff",
        "qhint": "#9fb6c8", "word": "#d8ecff", "ipa": "#6f8496",
        "sb_thumb": "#31465c", "sb_thumb_hi": "#3f5c78", "sb_trough": "#141b23",
    },
    "light": {
        "bg": "#faf8f3", "bg2": "#efeae0", "bar": "#e8e2d6",
        "btn": "#dbd4c6", "btn_hi": "#c9c0ae", "border": "#d3cabb",
        "fg": "#1f262d", "dim": "#6d7885",
        "accent": "#0b6ca8", "warm": "#9a5f06", "green": "#17724a",
        "hl": "#ffe7a3", "on_hl": "#20272e",
        "rec": "#cfe8f7", "on_rec": "#16303f",
        "qhint": "#465663", "word": "#17456e", "ipa": "#7b8692",
        "sb_thumb": "#b9ae9c", "sb_thumb_hi": "#a1957f", "sb_trough": "#efeae0",
    },
}
THEME_NAMES = {"dark": "黑暗", "light": "白天"}
POPUP_SECONDS = 7      # 选中取词的小窗停留多久自动关闭

# ---------------------------------------------------------------- 选中取词的发音
#
# 用系统自带的 SAPI（PowerShell 调 System.Speech）合成 wav，缓存到 tts_cache 目录，
# 再用 winsound 异步播放。第一次合成约 0.5 秒，之后同一个词是瞬发的。
# 没有 pywin32 / comtypes 也能用，不联网。

TTS_DIR = os.path.join(DATA_DIR, "voice_cache")
_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]{0,30}$")
_voice_state = {"checked": False, "has_tts": False, "logged": False}


def clean_selection(text):
    """把选区整理成一个单词；不是单个英文词就返回空。"""
    s = (text or "").strip()
    s = s.strip(" .,;:!?\"'()[]{}<>“”‘’…·")
    if not s:
        return ""
    if not _WORD_RE.match(s):
        return ""
    return s


def audio_cache_path(word, ext="mp3"):
    key = re.sub(r"[^a-z0-9]+", "_", word.lower()).strip("_")[:40]
    return os.path.join(TTS_DIR, (key or "w") + "." + ext)


def tts_cache_path(word):
    return audio_cache_path(word, "wav")


def has_tts_voice():
    """本机有没有 SAPI 语音。精简版 Windows 可能一个都没有，那就只能靠词典音频。"""
    if _voice_state["checked"]:
        return _voice_state["has_tts"]
    _voice_state["checked"] = True
    try:
        import winreg
        for hive, path in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Speech\Voices\Tokens"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Speech\Voices\Tokens"),
        ):
            try:
                with winreg.OpenKey(hive, path) as k:
                    if winreg.QueryInfoKey(k)[1] > 0:
                        _voice_state["has_tts"] = True
                        break
            except OSError:
                continue
    except Exception:
        pass
    if not _voice_state["has_tts"] and not _voice_state["logged"]:
        _voice_state["logged"] = True
        log("本机没有 SAPI 语音，发音只走词典音频")
    return _voice_state["has_tts"]


def synth_wav(word, path):
    """调系统 SAPI 合成单词读音到 wav。没有语音包时直接返回 False。"""
    if not has_tts_voice():
        return False
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.Rate = -1;"
        "$s.SetOutputToWaveFile('%s');"
        "$s.Speak('%s');"
        "$s.Dispose()"
    ) % (path.replace("'", "''"), word.replace("'", "''"))
    try:
        creation = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-Command", ps],
            timeout=15, creationflags=creation,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        log("tts failed: " + traceback.format_exc())
        return False
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 1500
    except OSError:
        return False


def play_audio(path):
    """播放 wav/mp3：wav 用 winsound，mp3 用 MCI（winsound 不认 mp3）。"""
    try:
        if not (os.path.isfile(path) and os.path.getsize(path) > 500):
            return False
        if path.lower().endswith(".wav"):
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
            return True
        winmm = ctypes.WinDLL("winmm")
        buf = ctypes.create_unicode_buffer(256)
        alias = "v%d" % (int(time.time() * 1000) % 1000000)
        err = winmm.mciSendStringW('open "%s" type mpegvideo alias %s' % (path, alias),
                                   buf, 255, None)
        if err != 0:
            err = winmm.mciSendStringW('open "%s" alias %s' % (path, alias),
                                       buf, 255, None)
        if err != 0:
            log("mci open failed: %s %s" % (err, buf.value))
            return False
        winmm.mciSendStringW("play %s" % alias, buf, 255, None)

        def closer():
            time.sleep(3.0)      # 单词音频一般 1 秒内，留够再关，别打断播放
            try:
                winmm.mciSendStringW("close %s" % alias, buf, 255, None)
            except Exception:
                pass
        threading.Thread(target=closer, daemon=True).start()
        return True
    except Exception:
        log("play failed: " + traceback.format_exc())
        return False


def speak_word(word, url=None, on_status=None):
    """后台发音：客户端词典的真人音频优先，其次本机 TTS，都没有就只显示音标。"""

    def status(text):
        if on_status:
            try:
                on_status(text)
            except Exception:
                pass

    def work():
        mp3 = audio_cache_path(word, "mp3")
        wav = tts_cache_path(word)
        if os.path.isfile(mp3) and os.path.getsize(mp3) > 500:
            status("正在播放")
            play_audio(mp3)
            return
        if os.path.isfile(wav) and os.path.getsize(wav) > 1500:
            status("正在播放")
            play_audio(wav)
            return
        if url:
            status("下载发音…")
            try:
                os.makedirs(TTS_DIR, exist_ok=True)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                if len(data) > 500:
                    with open(mp3, "wb") as f:
                        f.write(data)
                    status("正在播放")
                    play_audio(mp3)
                    return
            except Exception as e:
                log("audio download failed (%s): %s" % (word, e))
        if has_tts_voice():
            status("合成中…")
            os.makedirs(TTS_DIR, exist_ok=True)
            if synth_wav(word, wav):
                status("正在播放")
                play_audio(wav)
                return
        status("这个词没有音频")

    threading.Thread(target=work, daemon=True).start()


def round_corners(hwnd, pref=2):
    """Win11 的圆角（DWMWCP_ROUND=2）。Win10 上会失败，退回直角。"""
    try:
        val = ctypes.c_int(pref)
        return ctypes.WinDLL("dwmapi").DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), 33, byref(val), ctypes.sizeof(val)) == 0
    except Exception:
        return False

# 当前生效的一套（_build_ui 等直接读这些模块级名字）
BG = "#0d1218"
BG2 = "#141b23"
BAR = "#101820"
BAR_BTN = "#1b2836"
BAR_BTN_HI = "#24384a"
FG = "#e6edf3"
DIM = "#7d8b99"
ACCENT = "#5fc9ff"
WARM = "#ffd479"
GREEN = "#6ee7a8"
HL = "#1f4b63"
REC_BG = "#12354a"     # 录音中整段答案的底色（比逐句高亮淡一点）
BORDER = "#22303c"


def use_palette(name):
    """把模块级的颜色变量切到指定主题。"""
    global BG, BG2, BAR, BAR_BTN, BAR_BTN_HI, FG, DIM, ACCENT, WARM, GREEN, HL, REC_BG, BORDER
    p = PALETTES.get(name) or PALETTES["dark"]
    BG, BG2, BAR = p["bg"], p["bg2"], p["bar"]
    BAR_BTN, BAR_BTN_HI, BORDER = p["btn"], p["btn_hi"], p["border"]
    FG, DIM = p["fg"], p["dim"]
    ACCENT, WARM, GREEN = p["accent"], p["warm"], p["green"]
    HL, REC_BG = p["hl"], p["rec"]
    return p


class SlimScrollbar(tk.Canvas):
    """自绘细滑条：ttk 的滚动条在 clam 主题下染不上色，露出来一块浅灰很难看。

    提供和 Scrollbar 一样的 set(first, last) / command(...) 接口，直接当滚动条用。
    """

    def __init__(self, master, command=None, width=None, pal=None, **kw):
        self.px = int(width or 10)
        self.pal = pal or PALETTES["dark"]
        tk.Canvas.__init__(self, master, width=self.px, bg=self.pal["sb_trough"],
                           bd=0, highlightthickness=0, takefocus=0, **kw)
        self.command = command
        self.first = 0.0
        self.last = 1.0
        self._drag_off = None
        self._hot = False
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def set_theme(self, pal):
        self.pal = pal
        self.configure(bg=pal["sb_trough"])
        self._redraw()

    # --- 与 Scrollbar 兼容
    def set(self, first, last):
        try:
            self.first, self.last = float(first), float(last)
        except Exception:
            return
        self._redraw()

    def _geom(self):
        h = self.winfo_height()
        w = self.winfo_width()
        f = max(0.0, min(1.0, self.first))
        l = max(0.0, min(1.0, self.last))
        y0 = f * h
        y1 = l * h
        min_h = min(h, max(18, int(h * 0.06)))
        if y1 - y0 < min_h:
            y1 = min(h, y0 + min_h)
        return w, h, y0, y1

    def _redraw(self):
        self.delete("all")
        w, h, y0, y1 = self._geom()
        if h <= 2:
            return
        if self.last - self.first >= 0.999:
            return                       # 内容能一屏放下就不画拇指
        self.create_rectangle(2, int(y0), w - 2, int(y1),
                              fill=(self.pal["sb_thumb_hi"] if self._hot
                                    else self.pal["sb_thumb"]),
                              outline="")

    def _on_enter(self, ev):
        self._hot = True
        self._redraw()

    def _on_leave(self, ev):
        self._hot = False
        self._redraw()

    def _on_press(self, ev):
        w, h, y0, y1 = self._geom()
        if y0 <= ev.y <= y1:
            self._drag_off = ev.y - y0
            self.configure(cursor="sb_v_double_arrow")
        else:
            self._drag_off = (y1 - y0) / 2.0
            self._scroll_to(ev.y)
        return "break"

    def _on_drag(self, ev):
        if self._drag_off is None:
            return "break"
        self._scroll_to(ev.y)
        return "break"

    def _on_release(self, ev):
        self._drag_off = None
        self.configure(cursor="")

    def _scroll_to(self, y):
        w, h, y0, y1 = self._geom()
        if h <= 2 or not self.command:
            return
        frac = (y - (self._drag_off or 0)) / float(h)
        frac = max(0.0, min(1.0, frac))
        try:
            self.command("moveto", frac)
        except Exception:
            pass


class Overlay(object):
    def __init__(self, cfg, store, watcher):
        self.cfg = cfg
        self.store = store
        self.watcher = watcher

        self.auto = bool(cfg.get("follow_log", True))
        self.click_through = bool(cfg.get("click_through_default", True))
        self.visible = True
        self.paper = None
        self.part_idx = 0
        self.font_px = int(cfg.get("font_px", 19))
        self.scale = dpi_scale()
        self.side = str(cfg.get("side", "right"))
        self.width_ratio = float(cfg.get("width_ratio", 0.30))
        self.rect = None
        self.hwnd = None
        self._last_geom = None
        self._tick = 0
        self._hl_tagged = None
        self._rendered = None
        self._hl = None
        self._block_lines = {}
        self.poll_ms = max(120, int(cfg.get("poll_ms", 400)))
        self.manual_geometry = False
        self._rs = None
        self._drag_from = None
        self.grip_w = 6
        self.popup = None
        self._pop_frame = None
        self._pop_after = None
        self._pop_deadline = 0.0
        self._log_retry_ts = 0.0

        self.theme = str(cfg.get("theme") or "dark")
        if self.theme not in PALETTES:
            self.theme = "dark"
        self.pal = use_palette(self.theme)

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        # pythonw 下没有控制台，Tk 回调里的异常会静默吞掉，这里落盘
        self.root.report_callback_exception = self._on_tk_error

        self._build_ui()
        self._apply_saved_window()
        # 关键顺序：必须先让窗口真正映射并绘制一次，再加 WS_EX_LAYERED；
        # 如果在首绘之前就设成分层窗口，Tk 画的背景会整块画不出来（只剩文字）。
        self.root.update()
        self.hwnd = _frame_hwnd(self.root)
        self._apply_style()
        self._place()

        self.hk = HotkeyManager(self.root)
        binds = {
            "toggle_show": self.toggle_show,
            "next_part": lambda: self.step_part(1),
            "prev_part": lambda: self.step_part(-1),
            "next_paper": lambda: self.step_paper(1),
            "prev_paper": lambda: self.step_paper(-1),
            "toggle_click": self.toggle_click,
            "toggle_auto": self.toggle_auto,
            "font_up": lambda: self.adjust_font(1),
            "font_down": lambda: self.adjust_font(-1),
            "scroll_down": lambda: self.scroll(1),
            "scroll_up": lambda: self.scroll(-1),
            "cycle_side": self.cycle_side,
            "cycle_width": self.cycle_width,
            "toggle_theme": self.toggle_theme,
            "quit": self.quit,
        }
        for key, fn in binds.items():
            spec = (cfg.get("hotkeys") or {}).get(key)
            if spec:
                self.hk.register_with_fallback(key, fn, spec)
        for action, spec, why in self.hk.failed:
            log("hotkey %s (%s) 注册失败: %s" % (spec, action, why))
        self.hk.pump()
        self._sync_button_labels()

        self.refresh_all()
        self.root.after(self.poll_ms, self.tick)

    # ---------------- UI
    @staticmethod
    def _on_tk_error(exc_type, exc_value, tb):
        log("tk callback error: " + "".join(
            traceback.format_exception(exc_type, exc_value, tb)))

    def _fonts(self):
        fam = self.cfg.get("font_family", "Microsoft YaHei UI")
        s = getattr(self, "scale", 1.0)
        body = max(11, int(round(self.font_px * s)))
        self.f_body = (fam, -body)
        self.f_head = (fam, -max(12, body + 1), "bold")
        # 作业标题用衬线体（华文中宋比宋体更粗，屏上更打眼；中文衬线 + 拉丁 Times 风格）
        title_fam = self.cfg.get("title_font") or "华文中宋"
        self.f_title = (title_fam,
                        -max(14, int(round(int(self.cfg.get("title_px", 25)) * s))),
                        "bold")
        self.f_small = (fam, -max(10, body - 4))
        # 音标还原出来的单词：Times New Roman 斜体（不加粗）
        self.f_word = ("Times New Roman", -max(12, int(round(body * 1.12))), "italic")
        # 音标本身：Segoe UI 覆盖 IPA 符号比较全
        self.f_ipa = ("Segoe UI", -max(10, int(round(body * 0.86))))

    def _configure_text_tags(self):
        """正文所有 tag 的字体和颜色在这里统一设置（换字号/换主题都走这里）。"""
        p = self.pal
        self.txt.configure(bg=p["bg"], fg=p["fg"], selectbackground=p["hl"],
                           selectforeground=p["on_hl"],
                           insertbackground=p["fg"])
        self.txt.tag_configure("h", foreground=p["accent"], font=self.f_head,
                               spacing1=10, spacing3=4)
        self.txt.tag_configure("t", foreground=p["fg"], font=self.f_body,
                               spacing1=2, spacing3=6)
        # 逐句列表的悬挂缩进按字号算，不然放大字号后第一行和续行会错位
        indent = max(16, int(round(self.font_px * self.scale * 1.15)))
        self.txt.tag_configure("s", foreground=p["fg"], font=self.f_body,
                               spacing1=2, spacing3=6,
                               lmargin1=4, lmargin2=4 + indent)
        self.txt.tag_configure("active", background=p["hl"], foreground=p["on_hl"])
        self.txt.tag_configure("rec", background=p["rec"], foreground=p["on_rec"])
        self.txt.tag_configure("dim", foreground=p["dim"], font=self.f_body)
        self.txt.tag_configure("qh", font=self.f_head, foreground=p["warm"],
                               spacing1=10, spacing3=2)
        self.txt.tag_configure("qhint", font=self.f_body, foreground=p["qhint"],
                               spacing1=1, spacing3=2)
        self.txt.tag_configure("qans", font=self.f_body, foreground=p["fg"],
                               spacing1=1, spacing3=2)
        self.txt.tag_configure("qalt", font=self.f_body, foreground=p["dim"],
                               spacing1=0, spacing3=2)
        self.txt.tag_configure("w", font=self.f_word, foreground=p["word"],
                               spacing1=4, spacing3=2)
        self.txt.tag_configure("ipa", font=self.f_ipa, foreground=p["ipa"],
                               spacing1=0, spacing3=6)

    # ---------------- 选中取词：音标 + 发音
    def _on_text_release(self, ev):
        """鼠标松开时看有没有选中一个英文单词。"""
        try:
            sel = self.txt.get("sel.first", "sel.last")
        except tk.TclError:
            return
        word = clean_selection(sel)
        if not word:
            return
        self.show_word_popup(word, ev.x_root, ev.y_root)

    def _hide_popup(self):
        if self._pop_after is not None:
            try:
                self.root.after_cancel(self._pop_after)
            except Exception:
                pass
            self._pop_after = None
        if self.popup is not None:
            try:
                self.popup.destroy()
            except Exception:
                pass
            self.popup = None
            self._pop_frame = None
        self._pop_deadline = 0.0

    def _touch_popup(self, seconds=None):
        """续命：让弹窗再停留一段（tick 里检查这个截止时间）。"""
        self._pop_deadline = time.time() + (seconds or POPUP_SECONDS)

    def show_word_popup(self, word, x, y):
        """在鼠标附近弹一个小窗：喇叭按钮 + 单词 + 音标，并读出来。"""
        word = clean_selection(word)
        if not word:
            return
        ipa = ""
        url = ""
        try:
            if self.store.ipadict is not None:
                ipa = self.store.ipadict.word_ipa(word)
                url = self.store.ipadict.word_audio(word)
        except Exception:
            ipa = ""
        self._hide_popup()

        p = self.pal
        fam = self.cfg.get("font_family", "Microsoft YaHei UI")
        s = self.scale
        f_word = ("Times New Roman", -max(15, int(round(22 * s))), "italic")
        f_ipa = ("Segoe UI", -max(12, int(round(17 * s))))
        f_hint = (fam, -max(10, int(round(12 * s))))
        f_spk = ("Segoe UI Emoji", -max(16, int(round(22 * s))))

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        frame = tk.Frame(top, bg=p["bg2"], highlightthickness=1,
                         highlightbackground=p["border"], padx=12, pady=8)
        frame.pack(fill="both", expand=True)

        row = tk.Frame(frame, bg=p["bg2"])
        row.pack(fill="x", anchor="w")
        # 喇叭图标：点它就是播放
        lbl_spk = tk.Label(row, text="🔊", font=f_spk, fg=p["accent"], bg=p["bg2"],
                           cursor="hand2", padx=4)
        lbl_spk.pack(side="left", anchor="n")
        col = tk.Frame(row, bg=p["bg2"])
        col.pack(side="left", fill="x", expand=True)
        lbl_w = tk.Label(col, text=word, font=f_word, fg=p["word"], bg=p["bg2"],
                         anchor="w", justify="left")
        lbl_w.pack(anchor="w")
        lbl_i = tk.Label(col, text=(ipa or "（未收录音标）"), font=f_ipa,
                         fg=p["ipa"] if ipa else p["dim"], bg=p["bg2"],
                         anchor="w", justify="left")
        lbl_i.pack(anchor="w")
        lbl_h = tk.Label(frame, text="点 🔊 再听一遍", font=f_hint, fg=p["dim"],
                         bg=p["bg2"], anchor="w")
        lbl_h.pack(anchor="w", pady=(4, 0), fill="x")

        def replay(_ev=None):
            self._touch_popup()          # 点一次就续命，别播到一半就没了
            lbl_h.configure(text="正在播放")
            speak_word(word, url, on_status=lambda s: self._popup_status(lbl_h, s))
            # 状态字占位一会儿后换回操作提示，让人知道可以点喇叭重播
            try:
                top.after(2600, lambda: self._restore_hint(lbl_h))
            except Exception:
                pass
            return "break"

        for w in (lbl_spk, lbl_w, lbl_i, lbl_h, frame, col):
            w.bind("<Button-1>", replay)
        lbl_spk.bind("<Enter>", lambda e: lbl_spk.configure(fg=p["warm"]))
        lbl_spk.bind("<Leave>", lambda e: lbl_spk.configure(fg=p["accent"]))

        top.update_idletasks()
        w = top.winfo_reqwidth()
        h = top.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        px = max(8, min(x + 14, sw - w - 8))
        py = max(8, min(y + 20, sh - h - 8))
        top.geometry("%dx%d+%d+%d" % (w, h, px, py))
        top.update_idletasks()
        try:
            top.lift()
            keep_topmost(_frame_hwnd(top))
        except Exception:
            pass

        # Win11 圆角
        try:
            round_corners(_frame_hwnd(top))
        except Exception:
            pass

        self.popup = top
        self._pop_frame = frame
        # 双重保险：after 定时 + tick 里检查截止时间（Toplevel 被重建时 after 会丢）
        self._pop_after = top.after(int(POPUP_SECONDS * 1000), self._hide_popup)
        self._touch_popup()

        replay()

    @staticmethod
    def _restore_hint(label):
        try:
            label.configure(text="点 🔊 再听一遍")
        except Exception:
            pass

    def _popup_status(self, label, text):
        """后台线程回调 -> 回到 Tk 主线程更新文字。"""
        def apply():
            try:
                label.configure(text=text)
            except Exception:
                pass
        try:
            self.root.after(0, apply)
        except Exception:
            pass

    def _apply_theme(self, name, rebuild=True):
        """整体换肤：把颜色重新刷到所有控件和 tag 上。"""
        self.theme = name if name in PALETTES else "dark"
        self.pal = use_palette(self.theme)
        p = self.pal
        try:
            self.root.configure(bg=p["bg"])
            self.w_shell.configure(bg=p["bg"])
            self.w_outer.configure(bg=p["bg"], highlightbackground=p["border"])
            self.w_head.configure(bg=p["bg2"])
            for w in (self.lbl_title, self.lbl_sub, self.lbl_state):
                w.configure(bg=p["bg2"])
            self.lbl_title.configure(fg=p["accent"])
            self.lbl_sub.configure(fg=p["warm"])
            self.w_body.configure(bg=p["bg"])
            for bar in (self.w_bar1, self.w_bar2):
                bar.configure(bg=p["bar"])
            for b in self.btns.values():
                b.configure(bg=p["btn"], fg=p["fg"],
                            activebackground=p["accent"], activeforeground=p["bg"])
            for g in getattr(self, "grips", {}).values():
                g.configure(bg=p["bg"])
            if hasattr(self.sb, "set_theme"):
                self.sb.set_theme(p)
            self._configure_text_tags()
        except Exception:
            log("apply theme failed: " + traceback.format_exc())
        if rebuild:
            self._rendered = None
            self.render()
            self._sync_button_labels()

    def toggle_theme(self):
        self._apply_theme("light" if self.theme == "dark" else "dark")
        self.cfg["theme"] = self.theme
        self._save_window()

    def _build_ui(self):
        self._fonts()
        fam = self.cfg.get("font_family", "Microsoft YaHei UI")
        self.f_btn = (fam, -max(11, int(round(13 * self.scale))))
        self.f_btn_s = (fam, -max(10, int(round(12 * self.scale))))

        self.grip_w = max(4, int(round(6 * self.scale)))
        gw = self.grip_w

        # 外壳铺满整窗，里面留出 gw 的边给拖拽手柄
        self.w_shell = tk.Frame(self.root, bg=BG, highlightthickness=0)
        self.w_shell.pack(fill="both", expand=True)
        shell = self.w_shell
        self.w_outer = tk.Frame(shell, bg=BG, highlightthickness=1,
                                highlightbackground=BORDER)
        self.w_outer.pack(fill="both", expand=True, padx=gw, pady=gw)
        outer = self.w_outer

        # ---- 标题栏（可拖动整窗）
        self.w_head = tk.Frame(outer, bg=BG2, padx=12, pady=8, cursor="fleur")
        head = self.w_head
        head.pack(fill="x")
        self.lbl_title = tk.Label(head, text="", font=self.f_title, fg=ACCENT,
                                  bg=BG2, anchor="w", justify="left", cursor="fleur")
        self.lbl_title.pack(fill="x")
        self.lbl_sub = tk.Label(head, text="", font=self.f_head, fg=WARM,
                                bg=BG2, anchor="w", justify="left", cursor="fleur")
        self.lbl_sub.pack(fill="x", pady=(2, 0))
        self.lbl_state = tk.Label(head, text="", font=self.f_small, fg=DIM,
                                  bg=BG2, anchor="w", justify="left", cursor="fleur")
        self.lbl_state.pack(fill="x", pady=(2, 0))
        for w in (head, self.lbl_title, self.lbl_sub, self.lbl_state):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Double-Button-1>", lambda e: self.reset_window())

        # ---- 底部按钮栏（快捷键实体化）
        # 注意 pack 顺序：底栏必须先于正文 pack，否则正文 expand 会把底栏挤没。
        self.btns = {}
        self.w_bar2 = tk.Frame(outer, bg=BAR, padx=6, pady=4)
        bar2 = self.w_bar2
        bar2.pack(fill="x", side="bottom")
        row2 = [
            ("toggle_auto", "跟随", self.toggle_auto),
            ("toggle_click", "穿透", self.toggle_click),
            ("toggle_theme", "白天", self.toggle_theme),
            ("font_down", "A−", lambda: self.adjust_font(-1)),
            ("font_up", "A+", lambda: self.adjust_font(1)),
            ("reset", "复位", self.reset_window),
            ("quit", "✕", self.quit),
        ]
        for key, text, cmd in row2:
            b = self._mkbtn(bar2, text, cmd)
            b.pack(side="left", fill="x", expand=True, padx=1)
            self.btns[key] = b

        self.w_bar1 = tk.Frame(outer, bg=BAR, padx=6, pady=4)
        bar1 = self.w_bar1
        bar1.pack(fill="x", side="bottom")
        row1 = [
            ("prev_part", "◀ 部分", lambda: self.step_part(-1)),
            ("next_part", "部分 ▶", lambda: self.step_part(1)),
            ("prev_paper", "◀ 作业", lambda: self.step_paper(-1)),
            ("next_paper", "作业 ▶", lambda: self.step_paper(1)),
        ]
        for key, text, cmd in row1:
            b = self._mkbtn(bar1, text, cmd)
            b.pack(side="left", fill="x", expand=True, padx=1)
            self.btns[key] = b

        # ---- 正文 + 竖向滑条（最后 pack，吃掉剩余空间）
        self.w_body = tk.Frame(outer, bg=BG)
        body = self.w_body
        body.pack(fill="both", expand=True)
        self.txt = tk.Text(body, bg=BG, fg=FG, font=self.f_body, bd=0,
                           highlightthickness=0, wrap="word", padx=12, pady=8,
                           insertwidth=0, cursor="arrow", spacing1=0)
        self.sb = self._make_scrollbar(body)
        self.sb.pack(side="right", fill="y", pady=(2, 2))
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.configure(yscrollcommand=self.sb.set)
        self._configure_text_tags()
        self.txt.configure(state="disabled")
        self.txt.bind("<Button-4>", lambda e: self.txt.yview_scroll(-2, "units"))
        self.txt.bind("<Button-5>", lambda e: self.txt.yview_scroll(2, "units"))
        self.txt.bind("<MouseWheel>",
                      lambda e: self.txt.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        # 选中一个单词 -> 弹出小窗显示音标并朗读
        self.txt.bind("<ButtonRelease-1>", self._on_text_release)
        self.txt.bind("<Button-1>", lambda e: self._hide_popup(), add="+")

        # ---- 边缘拖拽手柄（放在 gw 的留白里，不压内容）
        self._build_grips(shell)

    def _make_scrollbar(self, parent):
        return SlimScrollbar(parent, command=self.txt.yview, pal=self.pal,
                             width=max(9, int(round(10 * self.scale))))

    def _mkbtn(self, parent, text, cmd):
        p = self.pal
        b = tk.Button(parent, text=text, command=self._wrap_click(cmd),
                      font=self.f_btn, bg=p["btn"], fg=p["fg"],
                      activebackground=p["accent"], activeforeground=p["bg"],
                      bd=0, relief="flat", highlightthickness=0,
                      padx=3, pady=3, takefocus=0, cursor="hand2")
        # 悬停色在事件发生时再读调色板，换主题后不用重新绑定
        b.bind("<Enter>", lambda e, w=b: w.configure(bg=self.pal["btn_hi"]))
        b.bind("<Leave>", lambda e, w=b: w.configure(bg=self.pal["btn"]))
        return b

    @staticmethod
    def _wrap_click(cmd):
        """点了按钮之后把焦点还给系统，别一直占着键盘。"""
        def run():
            try:
                cmd()
            except Exception:
                log("button action failed: " + traceback.format_exc())
        return run

    def _build_grips(self, shell):
        gw = self.grip_w
        cur = {
            "n": "size_ns", "s": "size_ns", "w": "size_we", "e": "size_we",
            "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw",
        }
        specs = [
            ("n", dict(x=0, y=0, relwidth=1, height=gw)),
            ("s", dict(x=0, rely=1, relwidth=1, height=gw, anchor="sw")),
            ("w", dict(x=0, y=0, relwidth=0, width=gw, relheight=1)),
            ("e", dict(relx=1, y=0, relwidth=0, width=gw, relheight=1, anchor="ne")),
            ("nw", dict(x=0, y=0, width=gw, height=gw)),
            ("ne", dict(relx=1, y=0, width=gw, height=gw, anchor="ne")),
            ("sw", dict(x=0, rely=1, width=gw, height=gw, anchor="sw")),
            ("se", dict(relx=1, rely=1, width=gw, height=gw, anchor="se")),
        ]
        self.grips = {}
        for name, opts in specs:
            g = tk.Frame(shell, bg=BG, cursor=cur[name])
            g.place(**opts)
            g.bind("<Button-1>", lambda e, n=name: self._resize_start(e, n))
            g.bind("<B1-Motion>", self._resize_move)
            g.bind("<ButtonRelease-1>", self._resize_end)
            self.grips[name] = g

    # ---------------- 拖动 / 缩放
    def _drag_start(self, ev):
        self._drag_from = (ev.x_root, ev.y_root,
                           self.root.winfo_rootx(), self.root.winfo_rooty())
        self.manual_geometry = True
        return "break"

    def _drag_move(self, ev):
        if not getattr(self, "_drag_from", None):
            return
        sx, sy, wx, wy = self._drag_from
        nx = wx + (ev.x_root - sx)
        ny = wy + (ev.y_root - sy)
        self.root.geometry("+%d+%d" % (nx, ny))
        self._last_geom = None

    def _drag_end(self, ev=None):
        self._drag_from = None
        self._after_user_geometry()

    def _resize_start(self, ev, edge):
        self._rs = {
            "edge": edge,
            "sx": ev.x_root, "sy": ev.y_root,
            "x": self.root.winfo_rootx(), "y": self.root.winfo_rooty(),
            "w": self.root.winfo_width(), "h": self.root.winfo_height(),
        }
        self.manual_geometry = True
        return "break"

    def _resize_move(self, ev):
        rs = getattr(self, "_rs", None)
        if not rs:
            return
        dx = ev.x_root - rs["sx"]
        dy = ev.y_root - rs["sy"]
        x, y, w, h = rs["x"], rs["y"], rs["w"], rs["h"]
        edge = rs["edge"]
        min_w, min_h = 280, 200
        if "e" in edge:
            w = max(min_w, w + dx)
        if "s" in edge:
            h = max(min_h, h + dy)
        if "w" in edge:
            nw = max(min_w, w - dx)
            x = x + (w - nw)
            w = nw
        if "n" in edge:
            nh = max(min_h, h - dy)
            y = y + (h - nh)
            h = nh
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._last_geom = None

    def _resize_end(self, ev=None):
        self._rs = None
        self._after_user_geometry()

    def _apply_saved_window(self):
        """上次关掉时窗口的位置/大小，直接还原。"""
        win = self.cfg.get("window") or {}
        try:
            w = int(win.get("w") or 0)
            h = int(win.get("h") or 0)
            x = int(win.get("x") or 0)
            y = int(win.get("y") or 0)
        except Exception:
            return
        if w < 280 or h < 200:
            return
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(-w + 80, min(x, sw - 80))
        y = max(0, min(y, sh - 60))
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.manual_geometry = True
        self._last_geom = "%dx%d+%d+%d" % (w, h, x, y)

    def _after_user_geometry(self):
        """用户手动调过之后：别再自动摆位，把尺寸记到配置里。"""
        self._last_geom = None
        self._save_window()

    def reset_window(self):
        """恢复到"贴着 E听说 自动摆位"的状态。"""
        self.manual_geometry = False
        self._last_geom = None
        self._place()
        self._save_window()

    def _save_window(self):
        try:
            cfg = dict(self.cfg)
            cfg["window"] = {
                "x": int(self.root.winfo_rootx()),
                "y": int(self.root.winfo_rooty()),
                "w": int(self.root.winfo_width()),
                "h": int(self.root.winfo_height()),
            }
            # 不保存"穿透"状态：万一存成穿透，下次启动按钮就点不动了
            self.cfg["window"] = cfg["window"]
            with io.open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
        except Exception:
            log("save window failed: " + traceback.format_exc())

    def _sync_button_labels(self):
        st = self.watcher.state
        if "toggle_auto" in self.btns:
            self.btns["toggle_auto"].configure(
                text=("跟随中" if self.auto else "已锁定"),
                fg=(GREEN if self.auto else WARM))
        if "toggle_click" in self.btns:
            self.btns["toggle_click"].configure(
                text=("已穿透" if self.click_through else "穿透"),
                fg=(WARM if self.click_through else FG))
        if "toggle_theme" in self.btns:
            # 按钮显示"点一下会切到的目标主题"
            self.btns["toggle_theme"].configure(
                text=("白天" if self.theme == "dark" else "黑暗"))

    # ---------------- 位置
    def _place(self):
        """默认贴着 E听说 窗口放；用户手动拖过/缩放过就以他的为准。"""
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        ratio = float(self.cfg.get("width_ratio", 0.30))

        if self.manual_geometry:
            return          # 用户自己摆的位置，别去动它

        hwnd, rect = find_ets_window()
        self.rect = rect
        if rect:
            l, t, r, b = rect
            # E听说 的窗口可能有一部分在屏幕外，先裁到屏幕内再用
            l = max(0, min(l, screen_w - 1))
            r = max(0, min(r, screen_w - 1))
            t = max(0, min(t, screen_h - 1))
            b = max(0, min(b, screen_h - 1))
            if r - l < 320 or b - t < 240:
                rect = None
        if rect:
            w = max(300, int((r - l) * ratio))
            x = l + 12 if self.side == "left" else r - w - 16
            y = t + max(60, int((b - t) * 0.08))
            h = (b - t) - 2 * max(60, int((b - t) * 0.08))
        else:
            w = max(300, int(screen_w * ratio))
            x = 12 if self.side == "left" else screen_w - w - 40
            y = max(60, int(screen_h * 0.08))
            h = screen_h - 2 * y
        # 客户端全屏时按比例算出来会大得离谱，夹一下
        w = min(w, max(360, int(screen_w * 0.46)), 860 * max(1.0, self.scale / 1.5))
        h = max(260, h)
        # 关键：钳制到屏幕范围内，否则窗口会跑到屏幕外"看不见"
        x = max(8, min(x, screen_w - w - 8))
        y = max(8, min(y, screen_h - h - 8))
        geom = "%dx%d+%d+%d" % (w, h, x, y)
        wrap = max(160, w - 40)
        self.lbl_title.configure(wraplength=wrap)
        self.lbl_sub.configure(wraplength=wrap)
        if geom != self._last_geom:
            self.root.geometry(geom)
            self._last_geom = geom

    def _apply_style(self):
        """确保置顶/穿透始终生效（Tk 自己会重写扩展样式）。"""
        if not self.hwnd:
            return
        set_click_through(self.hwnd, self.click_through)

    def _reassert(self):
        if self.hwnd:
            keep_topmost(self.hwnd)
        self._apply_style()

    # ---------------- 数据
    def refresh_all(self):
        if not self.store.papers:
            self.store.scan()
        if self.store.papers and self.paper is None:
            self.paper = self.store.papers[0]
            self.part_idx = 0
        self.render()

    def _ensure_paper(self, pid):
        p = self.store.find(pid)
        if p is None:
            self.store.scan()
            p = self.store.find(pid)
        return p

    def select_event_part(self, state):
        """根据日志状态切到对应作业/部分。手动模式下完全不插手。"""
        if not self.auto:
            return
        sid = state.get("set_id")
        if not sid:
            return
        if self.paper is None or self.paper.pid != str(sid):
            p = self._ensure_paper(sid)
            if p:
                self.paper = p
                self.part_idx = 0
        if self.paper is None:
            return
        eid = state.get("entity_id")
        if not eid:
            return
        p = self.paper.part_by_content_id(eid)
        if p and p.index != self.part_idx:
            self.part_idx = p.index
            self.render()

    def render(self, keep_view=False, anchor=None):
        if self.paper is None:
            self._set_text("还没有在 %s 里找到作业缓存。\n\n"
                           "先用 E听说 打开一次作业，数据就会缓存到本地，"
                           "本程序会自动读到。\n\n缓存目录：\n%s"
                           % (APP_NAME, self.store.cache_dir), None)
            self.lbl_title.configure(text="等作业数据…")
            self.lbl_sub.configure(text="")
            self.lbl_state.configure(text="")
            return

        paper = self.paper
        self.part_idx = max(0, min(self.part_idx, len(paper.parts) - 1))
        part = paper.parts[self.part_idx]

        self.lbl_title.configure(text=paper.title_with(
            (self.watcher.state.get("titles") or {}).get(paper.pid, "")))
        self.lbl_sub.configure(text="◈ 第 %d/%d 部分 · %s" % (
            self.part_idx + 1, len(paper.parts), part.label))
        st = self.watcher.state
        flags = []
        flags.append("自动跟随" if self.auto else "手动锁定")
        if paper.total_score:
            flags.append("共%s分" % paper.total_score)
        if st.get("recording"):
            flags.append("● 正在录音")
        elif st.get("sent"):
            flags.append("▶ 第 %d 句" % st["sent"])
        if self.click_through:
            flags.append("穿透中 → %s 恢复可点" % self.hk.byname.get("toggle_click", "热键"))
        if not self.watcher.log_dir:
            flags.append("未找到客户端日志，只能手动翻")
        self.lbl_state.configure(text=" ｜ ".join(flags), fg=(GREEN if st.get("recording") else DIM))
        self._sync_button_labels()

        # 正文：只有换了部分/作业/字号才重画，避免每 400ms 闪一次
        key = (paper.pid, part.content_id, self.font_px)
        if key != self._rendered:
            self.txt.configure(state="normal")
            self.txt.delete("1.0", "end")
            self._block_lines = {}
            line = 1
            for i, b in enumerate(part.blocks):
                if b["kind"] == "h":
                    self.txt.insert("end", b["text"] + "\n", ("h",))
                elif b["kind"] == "s":
                    self.txt.insert("end", "%d. " % b["n"], ("dim",))
                    self.txt.insert("end", b["text"] + "\n", ("s",))
                elif b["kind"] == "w":
                    word = b.get("text") or ""
                    if word:
                        self.txt.insert("end", word, ("w",))
                        self.txt.insert("end", "   ", ("t",))
                    else:
                        self.txt.insert("end", "（未收录）   ", ("dim",))
                    self.txt.insert("end", b.get("sub", "") + "\n", ("ipa",))
                elif b["kind"] == "q":
                    tag = "你要提问" if b.get("role") == "ask" else "你要回答"
                    self.txt.insert("end", "第 %d 题 · %s\n" % (b["n"], tag), ("qh",))
                    n_lines = 1
                    head = b.get("head") or ""
                    if head:
                        self.txt.insert("end", head + "\n", ("qhint",))
                        n_lines += 1
                    for j, c in enumerate(b.get("cans") or []):
                        if j == 0:
                            self.txt.insert("end", c + "\n", ("qans",))
                        else:
                            self.txt.insert("end", "或 " + c + "\n", ("qalt",))
                        n_lines += 1
                    self._block_lines[i] = (line, line + n_lines - 1)
                    line += n_lines
                    continue
                else:
                    self.txt.insert("end", b["text"] + "\n", ("t",))
                n = b["text"].count("\n") + 1
                self._block_lines[i] = (line, line + n - 1)
                line += n
            self.txt.configure(state="disabled")
            self._rendered = key
            self._hl = None
            if keep_view and anchor:
                try:
                    self.txt.yview(anchor)      # 让原来视口顶部那一行仍在顶部
                except Exception:
                    self.txt.yview_moveto(0.0)
            else:
                self.txt.yview_moveto(0.0)

        # 高亮当前正在播/正在录的那一句（调字号时别让高亮抢走滚动位置）
        self._apply_highlight(part, st, see=not keep_view)

    def _apply_highlight(self, part, st, see=True):
        """高亮正在播/正在录的内容。

        听读题有逐句信息（fjN.mp3）→ 高亮那一句；
        朗读/复述题没有逐句信息 → 录音时把整段答案铺一层底色，
        这样"现在要念的是这一块"一眼就能看出来。
        see=False 时只上色、不动滚动位置（调字号时用）。
        """
        sent_no = int(st.get("sent") or 0)
        qno = int(st.get("qn") or 0)
        if part.ctype == "collector.3q5a":
            bi = part.question_block_index(qno)
        else:
            bi = part.active_block_index(sent_no) if sent_no else -1
        rec_all = bool(st.get("recording")) and bi < 0
        if part.ctype == "collector.3q5a" and qno <= 0:
            rec_all = False      # 角色扮演还没定位到题号时不乱高亮
        sig = (bi, rec_all, part.content_id)
        if sig == self._hl:
            return
        self.txt.configure(state="normal")
        self.txt.tag_remove("active", "1.0", "end")
        self.txt.tag_remove("rec", "1.0", "end")
        if bi >= 0 and bi in self._block_lines:
            a, b = self._block_lines[bi]
            self.txt.tag_add("active", "%d.0" % a, self._range_end(b))
            if see:
                self.txt.see("%d.0" % a)
        elif rec_all:
            # 故事复述这类：只高亮参考范文那一段（要点/题目不高亮）
            spans = []
            for i, blk in enumerate(part.blocks):
                if blk.get("role") == "ref" and i in self._block_lines:
                    spans.append(self._block_lines[i])
            if spans:
                a = min(s[0] for s in spans)
                b = max(s[1] for s in spans)
                self.txt.tag_add("rec", "%d.0" % a, self._range_end(b))
            else:
                self.txt.tag_add("rec", "1.0", "end")
        self.txt.configure(state="disabled")
        self._hl = sig

    def _range_end(self, line):
        """高亮块最后一行的结束位置。

        Tk 的 tag 底色只画到字符为止，短行右半截会留个缺口；把行尾换行符一起圈进去
        才能铺满整行（不能用"end"，会越界）。
        """
        end = "%d.end" % line
        try:
            if self.txt.compare(end, "<", "end"):
                return end + " +1c"
        except Exception:
            pass
        return end

    def _set_text(self, s, _unused):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", s, ("dim",))
        self.txt.configure(state="disabled")

    # ---------------- 行为
    def step_part(self, delta):
        if self.paper is None:
            return
        self.auto = False
        self.part_idx = (self.part_idx + delta) % len(self.paper.parts)
        self.render()

    def step_paper(self, delta):
        if not self.store.papers:
            self.store.scan()
        if not self.store.papers:
            return
        idx = 0
        if self.paper is not None:
            for i, p in enumerate(self.store.papers):
                if p.pid == self.paper.pid:
                    idx = i
                    break
        idx = (idx + delta) % len(self.store.papers)
        self.paper = self.store.papers[idx]
        self.part_idx = 0
        self.auto = False
        self.render()

    def toggle_auto(self):
        self.auto = not self.auto
        if self.auto:
            self.select_event_part(self.watcher.state)
        self.render()

    def toggle_click(self):
        self.click_through = not self.click_through
        self._apply_style()
        self.render()

    def toggle_show(self):
        self.visible = not self.visible
        if self.visible:
            self.root.deiconify()
            self._reassert()
        else:
            self.root.withdraw()

    def adjust_font(self, delta):
        """调字号：所有字体（正文/标题/单词/音标/题目/按钮）一起变，并保持当前位置。

        以前只改了正文和几个 label，单词、音标、角色扮演那几组 tag 的字体没跟着变，
        放大后列宽和缩进错位；而且重绘会把正文滚回顶部，看起来像"位置出错了"。
        """
        try:
            anchor = self.txt.index("@0,0")     # 当前视口顶部是哪一行
        except Exception:
            anchor = None
        self.font_px = max(10, min(34, self.font_px + delta))
        self._fonts()
        fam = self.cfg.get("font_family", "Microsoft YaHei UI")
        self.f_btn = (fam, -max(11, int(round(13 * self.scale))))
        self.lbl_title.configure(font=self.f_title)
        self.lbl_sub.configure(font=self.f_head)
        self.lbl_state.configure(font=self.f_small)
        for b in getattr(self, "btns", {}).values():
            b.configure(font=self.f_btn)
        self._configure_text_tags()
        self.txt.configure(font=self.f_body)
        self.render(keep_view=True, anchor=anchor)

    def scroll(self, delta):
        self.txt.yview_scroll(delta, "pages")

    def cycle_side(self):
        self.side = "left" if self.side != "left" else "right"
        self.manual_geometry = False
        self._last_geom = None
        self._place()

    def cycle_width(self):
        presets = [0.24, 0.30, 0.38, 0.46]
        cur = float(self.cfg.get("width_ratio", 0.30))
        try:
            i = min(range(len(presets)), key=lambda k: abs(presets[k] - cur))
        except Exception:
            i = 1
        self.cfg["width_ratio"] = presets[(i + 1) % len(presets)]
        self.manual_geometry = False
        self._last_geom = None
        self._place()

    def quit(self):
        log("quit via hotkey")
        self._save_window()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------------- 主循环
    def tick(self):
        self._tick += 1
        try:
            if self.cfg.get("follow_log", True):
                self.watcher.poll()
                self.select_event_part(self.watcher.state)
                self.render()
            # 选中取词的小窗到点就收：不依赖单个 after 定时器（Toplevel 重建会丢）
            if self.popup is not None and self._pop_deadline \
                    and time.time() > self._pop_deadline:
                self._hide_popup()
            # 日志目录没找到（比如客户端后启动、或装在别处）：定期再探一次
            if self.cfg.get("follow_log", True) and not self.watcher.log_dir:
                if time.time() - self._log_retry_ts > 15:
                    self._log_retry_ts = time.time()
                    d = detect_log_dir(self.cfg)
                    if d:
                        self.watcher.log_dir = d
                        self.watcher.attach()
                        log("log_dir 后补上了：%s" % d)
                        self.render()
            if self._tick % 5 == 0:
                self._place()
            self._apply_style()          # Tk 会重写扩展样式，每轮补回来
            if self._tick % 8 == 0:
                keep_topmost(self.hwnd)
                # 面板自己是 TOPMOST，会把弹窗压下去；有弹窗时把它一起顶上来
                if self.popup is not None:
                    try:
                        self.popup.lift()
                        keep_topmost(_frame_hwnd(self.popup))
                    except Exception:
                        pass
        except Exception:
            log("tick error: " + traceback.format_exc())
        self.root.after(self.poll_ms, self.tick)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            log("mainloop exited")


# ---------------------------------------------------------------- 自检 / CLI


def _report(text):
    p = os.path.join(DATA_DIR, "_selftest.txt")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def cmd_prebuild(cfg):
    """预先构建音标索引（打包/换机器后跑一次，启动就不用等）。"""
    cache = detect_cache_dir(cfg)
    d = IpaDict(cache)
    d.index = None
    if os.path.exists(d.cache_path):
        try:
            os.remove(d.cache_path)
        except OSError:
            pass
    t0 = time.time()
    ok = d.ensure()
    print("词典: %s" % d.dict_path)
    print("索引: %s" % d.cache_path)
    print("结果: %s  %d 个 key  用时 %.2fs" % (
        "成功" if ok else "失败", len(d.index or {}), time.time() - t0))
    if d.error:
        print("原因:", d.error)
    if ok:
        print("大小: %.1f KB" % (os.path.getsize(d.cache_path) / 1024.0))
    return ok


def cmd_find(cfg):
    """打印自动探测的结果和依据，方便排查"为什么没跟上"。"""
    cache = detect_cache_dir(cfg)
    print("作业缓存目录: %s  (%s)" % (cache, "存在" if os.path.isdir(cache) else "不存在"))

    print("\n安装目录线索（进程/注册表/快捷方式）:")
    insts = _install_dir_candidates()
    if insts:
        for p in insts:
            print("  %s%s" % (p, "   <-- 有 logs" if _logs_ok(os.path.join(p, "logs")) else ""))
    else:
        print("  （没有）")

    print("\n运行中的客户端进程:")
    procs = _process_exe_paths(set(CLIENT_EXES))
    print("\n".join("  " + p for p in procs) if procs else "  （没在运行）")

    found, why = find_log_dir(verbose=True)
    print("\n最终日志目录: %s" % (found or "未找到"))
    print("依据: %s" % why)
    if found:
        cfg["log_dir"] = found
        save_config(cfg)
        print("已写入 config.json")
    return found


def cmd_list(cfg):
    cache = detect_cache_dir(cfg)
    store = PaperStore(cache, IpaDict(cache))
    papers = store.scan()
    lines = ["缓存目录: %s" % cache, "日志目录: %s" % (detect_log_dir(cfg) or "未找到"), ""]
    if not papers:
        lines.append("没找到任何作业缓存。")
    for p in papers:
        lines.append("%s  %s  (%.1fMB, 更新 %s)" % (
            p.pid, p.title,
            sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(p.path) for f in fs) / 1048576.0,
            time.strftime("%Y-%m-%d %H:%M", time.localtime(p.mtime))))
        for part in p.parts:
            lines.append("    [%d] %s  <%s>" % (part.index + 1, part.label, part.ctype))
            first = part.plain_text().split("\n")[0] if part.blocks else ""
            lines.append("        " + squash(first, 70))
    out = "\n".join(lines)
    print(out)
    return out


def cmd_dump(cfg, pid):
    cache = detect_cache_dir(cfg)
    store = PaperStore(cache, IpaDict(cache))
    store.scan()
    p = store.find(pid)
    if not p:
        print("没有作业 %s，现有：%s" % (pid, [x.pid for x in store.papers]))
        return ""
    out = ["=" * 60, p.title, "=" * 60]
    for part in p.parts:
        out.append("")
        out.append("------ 第%d部分 %s <%s> ------" % (part.index + 1, part.label, part.ctype))
        out.append(part.plain_text())
    text = "\n".join(out)
    print(text)
    return text


def cmd_selftest(cfg):
    res = []
    res.append("version: %s  python %s" % (VERSION, sys.version.replace("\n", " ")))
    cache = detect_cache_dir(cfg)
    logdir = detect_log_dir(cfg)
    res.append("cache_dir: %s  exists=%s" % (cache, os.path.isdir(cache)))
    res.append("log_dir:   %s  exists=%s" % (logdir, os.path.isdir(logdir)))

    store = PaperStore(cache, IpaDict(cache))
    papers = store.scan()
    res.append("papers: %d -> %s" % (len(papers), [p.pid for p in papers]))
    for p in papers:
        res.append("  %s %s parts=%d mtime=%s %s" % (
            p.pid, p.title, len(p.parts),
            time.strftime("%m-%d %H:%M", time.localtime(p.mtime)),
            ("ERR:" + p.error) if p.error else ""))
        for part in p.parts:
            res.append("    [%d] %s <%s> blocks=%d chars=%d" % (
                part.index + 1, part.label, part.ctype, len(part.blocks),
                len(part.plain_text())))

    w = LogWatcher(logdir)
    ok = w.attach()
    res.append("watcher attach: %s path=%s" % (ok, w.path))
    st = w.poll()
    res.append("state after seed: %s" % json.dumps(st, ensure_ascii=False))
    if st.get("set_id"):
        p = store.find(st["set_id"])
        res.append("matched paper: %s" % (p.title if p else "None"))
        if p and st.get("entity_id"):
            part = p.part_by_content_id(st["entity_id"])
            res.append("matched part: %s" % (part.label if part else "None"))

    # UI 能力
    try:
        set_dpi_aware()
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        lbl = tk.Label(root, text="自检", bg="#0d1218", fg="#fff",
                       font=("Microsoft YaHei UI", -20))
        lbl.pack()
        root.geometry("300x120+80+80")
        root.update()
        res.append("tk: ok  hwnd=%s" % root.winfo_id())
        root.destroy()
    except Exception:
        res.append("tk: FAILED " + traceback.format_exc())

    hwnd, rect = find_ets_window()
    res.append("ets window: hwnd=%s rect=%s" % (hwnd, rect))

    p = _report("\n".join(res))
    print("\n".join(res))
    print("\n-> " + p)
    return p


_LOCK_FDS = []


def single_instance():
    """单实例：用独占文件锁。

    以前用命名互斥量 CreateMutexW，但这个名字在本机上被另一个进程以受限 ACL 占着，
    拿到的永远是 ERROR_ACCESS_DENIED，于是程序一启动就误判"已经在运行"直接退出。
    文件锁没有 ACL 问题，进程退出（含崩溃）时由系统自动释放。
    """
    path = os.path.join(DATA_DIR, "helper.lock")
    try:
        import msvcrt
        fh = open(path, "a+")
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        _LOCK_FDS.append(fh)      # 进程存活期间一直持有这个句柄
        return True
    except Exception:
        return True


def main(argv):
    set_dpi_aware()
    cfg = load_config()
    if "--list" in argv:
        cmd_list(cfg)
        return 0
    if "--dump" in argv:
        i = argv.index("--dump")
        pid = argv[i + 1] if i + 1 < len(argv) else ""
        cmd_dump(cfg, pid)
        return 0
    if "--selftest" in argv:
        cmd_selftest(cfg)
        return 0
    if "--prebuild" in argv:
        cmd_prebuild(cfg)
        return 0
    if "--find" in argv:
        cmd_find(cfg)
        return 0

    log("=== start %s ===" % VERSION)
    if not single_instance():
        log("already running, exit")
        return 0
    cache = detect_cache_dir(cfg)
    logdir = detect_log_dir(cfg)
    log("cache=%s log=%s" % (cache, logdir))
    ipadict = None
    if cfg.get("ipa_to_word", True):
        ipadict = IpaDict(cache)
        # 预加载：开窗之前就把词典索引读进内存，之后翻到单词朗读是零等待
        ipadict.ensure()
    store = PaperStore(cache, ipadict)
    t0 = time.time()
    store.scan()
    log("papers: %s (scan %.2fs)" % ([p.pid for p in store.papers], time.time() - t0))
    watcher = LogWatcher(logdir)
    if cfg.get("follow_log", True):
        watcher.attach()
    app = Overlay(cfg, store, watcher)
    app.run()
    return 0


if __name__ == "__main__":
    def _excepthook(et, ev, tb):
        log("uncaught: " + "".join(traceback.format_exception(et, ev, tb)))

    sys.excepthook = _excepthook
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        log("fatal: " + traceback.format_exc())
        raise

