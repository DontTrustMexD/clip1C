# -*- coding: utf-8 -*-
"""
clip.py — «скрепка» для Конфигуратора 1С.

Маленькое окно поверх Конфигуратора. Следит за фокусом, в реальном времени
показывает, в каком модуле и в какой процедуре стоит курсор. По хоткею
складывает полный контекст в файл — его читает агент. Ответ агента
вставляет обратно в редактор.

    Ctrl+Alt+A   снять контекст  -> <workdir>\context\context.json + prompt.md
    Ctrl+Alt+V   вставить ответ  <- <workdir>\answer.bsl

Требуется:  python -m pip install comtypes
Запуск:     python clip.py
Если 1С запущена от администратора — Python тоже от администратора.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

# --- переносимость -----------------------------------------------------------
# comtypes — чистый python, поэтому он лежит колесом в vendor\ и распаковывается
# сам при первом запуске. На новой машине pip не нужен.
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")


def _bootstrap_vendor():
    if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
        sys.path.insert(0, _VENDOR)
    try:
        import comtypes  # noqa: F401
        return
    except ImportError:
        pass
    import glob
    import zipfile
    wheels = sorted(glob.glob(os.path.join(_VENDOR, "comtypes-*.whl")))
    if not wheels:
        return
    try:
        with zipfile.ZipFile(wheels[0]) as z:
            z.extractall(_VENDOR)
    except Exception as e:
        print("не удалось распаковать comtypes из vendor:", e)


_bootstrap_vendor()

try:
    import comtypes
    import comtypes.client
except ImportError:
    print("Нет comtypes. Положите колесо в vendor\\ или выполните:")
    print("    python -m pip install comtypes")
    sys.exit(1)

import bslls

UIA_MOD = comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation  # noqa: E402

# ----------------------------------------------------------------------------
# конфигурация
# ----------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "clip.config.json")

DEFAULTS = {
    "workdir": "",   # пусто — папка со скриптом
    "answerFile": "answer.bsl",
    # необязательный внешний навигатор по конфигурации: любой локальный сервис,
    # который умеет отвечать про метаданные, граф вызовов и карточки методов.
    # Скрепка только проверяет, жив ли он, и упоминает его в промпте для агента.
    # ЗАЩИТА: читаем текст только из этих процессов. Пустой список означал бы
    # чтение чего угодно — браузера, почты, мессенджера, — поэтому он не пустой.
    "editorProcesses": ["1cv8.exe", "1cv8c.exe", "1cv8s.exe"],
    "navigatorUrl": "",            # напр. http://127.0.0.1:8765; пусто — индикатора нет
    "navigatorName": "",           # подпись в шапке; пусто — просто «навигатор»
    "pollMs": 700,
    "stripIndentOnInsert": True,
    "claudeCommand": "",           # напр. "claude" — когда CLI разрешат
    "claudeMcpConfig": "",         # напр. "D:\\git\\clip\\mcp.json"
    "bslLsJar": "",                # пусто — ищем сами (рядом и у Phoenix BSL)
    "javaCommand": "",             # пусто — java из PATH или JAVA_HOME
    "bslLsEnabled": True,
    "diagMinSeverity": 2,          # 1 только ошибки, 2 +замечания, 4 всё подряд
    "diagOnlyCurrentMethod": True, # показывать только то, что в текущем методе
    "bslLsConfig": "",             # пусто — .bsl-language-server.json рядом со скриптом
    "lineOffset": 0,               # поправка нумерации строк под Конфигуратор
    "geometry": "",                # где и какого размера было окно в прошлый раз
    "compact": False,              # свёрнутый вид: только строка состояния
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    else:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # путь из конфига мог приехать с другой машины — не доверяем ему вслепую
    if not cfg.get("workdir") or not os.path.isdir(cfg["workdir"]):
        cfg["workdir"] = HERE
    jar = cfg.get("bslLsJar")
    if jar and not os.path.exists(jar):
        cfg["bslLsJar"] = ""          # нет на этой машине — будем искать сами
    return cfg


def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def L(n):
    """Номер строки так, как его показывает Конфигуратор.

    Внутри всё считается от текста, полученного через UI Automation; если его
    нумерация разошлась с редактором, разницу компенсирует lineOffset."""
    return n + int(CFG.get("lineOffset", 0))


CFG = load_config()
CONTEXT_DIR = os.path.join(CFG["workdir"], "context")
LIVE_PATH = None  # заполняется после создания каталога
os.makedirs(CONTEXT_DIR, exist_ok=True)
LIVE_PATH = os.path.join(CONTEXT_DIR, "live.bsl")

# ----------------------------------------------------------------------------
# WinAPI
# ----------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.GetFocus.restype = wt.HWND
user32.IsWindowUnicode.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageA.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]

user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]

WM_CHAR = 0x0102
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY = 0x0312
HK_CAPTURE, HK_INSERT, HK_TOGGLE = 1, 2, 3


def window_text(hwnd):
    if not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def window_class(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def focused_hwnd():
    """HWND контрола с клавиатурным фокусом в активном окне (не своём)."""
    fg = user32.GetForegroundWindow()
    if not fg:
        return None, ""
    target_tid = user32.GetWindowThreadProcessId(fg, None)
    our_tid = kernel32.GetCurrentThreadId()
    attached = False
    try:
        if target_tid != our_tid:
            attached = bool(user32.AttachThreadInput(our_tid, target_tid, True))
        h = user32.GetFocus()
    finally:
        if attached:
            user32.AttachThreadInput(our_tid, target_tid, False)
    return (h or fg), window_text(fg)


def process_of_window(hwnd):
    """Имя exe-файла процесса, которому принадлежит окно."""
    if not hwnd:
        return ""
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        size = wt.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)


def is_editor_window():
    """Пускать ли чтение: активное окно должно принадлежать 1С.

    Без этой проверки скрепка читала бы любой текст в фокусе — страницу
    браузера, письмо, переписку — и писала бы его на диск и в языковой сервер.
    """
    fg = user32.GetForegroundWindow()
    if not fg:
        return False, ""
    exe = process_of_window(fg)
    allowed = [p.lower() for p in CFG.get("editorProcesses", [])]
    return (exe in allowed), exe


def post_text(hwnd, text):
    """Посимвольная вставка через WM_CHAR — обходит клавиатурный хук 1С."""
    unicode_win = bool(user32.IsWindowUnicode(hwnd))
    for ch in text:
        if ch == "\n":
            code = 13
        elif ch == "\r":
            continue
        else:
            code = ord(ch)
        if unicode_win:
            user32.PostMessageW(hwnd, WM_CHAR, code, 0)
        else:
            try:
                b = ch.encode("cp1251") if code > 127 else bytes([code])
                code_a = b[0] if code > 127 else code
            except UnicodeEncodeError:
                continue
            user32.PostMessageA(hwnd, WM_CHAR, code_a, 0)
        time.sleep(0.0015)


# ----------------------------------------------------------------------------
# UI Automation: чтение модуля
# ----------------------------------------------------------------------------

PAT_TEXT = 10014
ENDPOINT_START, ENDPOINT_END = 0, 1
TEXTUNIT_CHARACTER = 0
TEXTUNIT_LINE = 3

# сколько строк метода класть в промпт целиком, прежде чем резать окном
MAX_METHOD_LINES = 400
WINDOW_LINES = 150

METHOD_RE = re.compile(
    r"^[ \t]*(Процедура|Функция|Procedure|Function)\s+([A-Za-zА-Яа-яЁё_][\w]*)",
    re.IGNORECASE | re.MULTILINE,
)
METHOD_END_RE = re.compile(
    r"^[ \t]*(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)",
    re.IGNORECASE | re.MULTILINE,
)
REGION_RE = re.compile(r"^[ \t]*#Область\s+([^\r\n]+)", re.IGNORECASE | re.MULTILINE)


def parse_methods(text):
    """Все процедуры/функции модуля с границами."""
    ends = [m.end() for m in METHOD_END_RE.finditer(text)]
    methods = []
    for m in METHOD_RE.finditer(text):
        start = m.start()
        end = next((e for e in ends if e > start), len(text))
        methods.append({
            "kind": m.group(1),
            "name": m.group(2),
            "start": start,
            "end": end,
            "line": text.count("\n", 0, start) + 1,
        })
    return methods


MAX_CALLEES = 6
MAX_CALLERS = 4
DEP_FULL_LINES = 60      # метод короче — кладём целиком
DEP_HEAD_LINES = 25      # длиннее — только начало

CALL_RE = re.compile(r"([A-Za-zА-Яа-яЁё_][\w]*)\s*\(")


def _trim(src):
    lines = src.splitlines()
    if len(lines) <= DEP_FULL_LINES:
        return src
    return "\n".join(lines[:DEP_HEAD_LINES] +
                     [f"    // … ещё {len(lines) - DEP_HEAD_LINES} строк …"])


def nearest_dependencies(text, methods, current):
    """Кого зовёт текущий метод и кто зовёт его — в пределах этого модуля."""
    if not current:
        return [], []

    by_name = {m["name"].lower(): m for m in methods}
    self_lc = current["name"].lower()
    body = text[current["start"]:current["end"]]

    callees, seen = [], set()
    for m in CALL_RE.finditer(body):
        nm = m.group(1).lower()
        if nm == self_lc or nm in seen:
            continue
        target = by_name.get(nm)
        if target is None:
            continue
        seen.add(nm)
        callees.append(target)
        if len(callees) >= MAX_CALLEES:
            break

    own_call = re.compile(r"\b" + re.escape(current["name"]) + r"\s*\(", re.IGNORECASE)
    callers = []
    for m in methods:
        if m["name"].lower() == self_lc:
            continue
        if own_call.search(text[m["start"]:m["end"]]):
            callers.append(m)
            if len(callers) >= MAX_CALLERS:
                break

    return callees, callers


def enclosing_region(text, offset):
    last = None
    for m in REGION_RE.finditer(text):
        if m.start() <= offset:
            last = m.group(1).strip()
        else:
            break
    return last


class Reader:
    """Плюс к чтению умеет вернуть курсор в редактор на нужную строку."""

    """Читает модуль под курсором. Дорогое чтение всего текста кэшируется:
    на каждом тике смотрим только строку под кареткой, а весь модуль
    перечитываем, лишь когда строка сменилась и прошёл минимальный интервал."""

    def __init__(self):
        self.iuia = comtypes.client.CreateObject(CUIAutomation, interface=IUIAutomation)
        self._cache = None          # последний полный контекст
        self._cache_at = 0.0
        self._line_sig = None       # текст строки под кареткой на момент кэша
        self.last_foreign = ""      # чей процесс был в фокусе, если это не 1С
        self._tp = None             # TextPattern редактора: нужен, чтобы прыгать к строке
        self._top_hwnd = None       # окно Конфигуратора, чтобы поднять его на передний план
        self.min_full_ms = 1200     # подстраивается под замеренное время чтения
        self.last_read_ms = 0.0
        self.last_full_ms = 0.0

    def _text_pattern(self, elem):
        try:
            raw = elem.GetCurrentPattern(PAT_TEXT)
            if not raw:
                return None
            return raw.QueryInterface(UIA_MOD.IUIAutomationTextPattern)
        except Exception:
            return None

    @staticmethod
    def _caret(tp):
        """Диапазон-каретка (начало выделения) или None."""
        try:
            sel = tp.GetSelection()
            if sel and sel.Length:
                return sel.GetElement(0)
        except Exception:
            pass
        return None

    @staticmethod
    def _line_under_caret(caret):
        """Дешёвая проба: текст строки под кареткой. Не зависит от размера модуля."""
        try:
            r = caret.Clone()
            r.ExpandToEnclosingUnit(TEXTUNIT_LINE)
            return r.GetText(600) or ""
        except Exception:
            return None

    def read(self, force=False):
        t0 = time.perf_counter()

        # Первым делом — чей это процесс. Всё остальное только после «да».
        ok, exe = is_editor_window()
        self.last_foreign = "" if ok else exe
        if not ok:
            self.last_read_ms = (time.perf_counter() - t0) * 1000
            return None

        try:
            elem = self.iuia.GetFocusedElement()
        except Exception:
            return None
        if elem is None:
            return None

        tp = self._text_pattern(elem)
        if tp is None:
            return None

        caret = self._caret(tp)
        line_sig = self._line_under_caret(caret) if caret is not None else None
        now = time.time()

        # строка не менялась и кэш свежий — отдаём как есть, ничего не читая
        if (not force and self._cache is not None and line_sig is not None
                and line_sig == self._line_sig and now - self._cache_at < 30):
            self.last_read_ms = (time.perf_counter() - t0) * 1000
            return self._cache

        # строка сменилась, но читать весь модуль ещё рано — не дёргаем 1С
        if (not force and self._cache is not None
                and (now - self._cache_at) * 1000 < self.min_full_ms):
            self.last_read_ms = (time.perf_counter() - t0) * 1000
            return self._cache

        ctx = self._read_full(tp, caret)
        if ctx is None:
            self.last_read_ms = (time.perf_counter() - t0) * 1000
            return None

        self._cache = ctx
        self._cache_at = now
        self._line_sig = line_sig
        self.last_read_ms = (time.perf_counter() - t0) * 1000
        return ctx

    def goto_line(self, line):
        """Выделить строку в редакторе Конфигуратора и показать её."""
        if self._tp is None or not self._cache:
            return False, "редактор ещё не читался"
        text = self._cache["text"]
        rows = text.splitlines(keepends=True)
        if not (1 <= line <= len(rows)):
            return False, "строки нет в модуле"

        start = sum(len(r) for r in rows[:line - 1])
        length = max(1, len(rows[line - 1].rstrip("\r\n")))

        try:
            doc = self._tp.DocumentRange
            r = doc.Clone()
            r.MoveEndpointByUnit(ENDPOINT_START, TEXTUNIT_CHARACTER, start)
            r.MoveEndpointByRange(ENDPOINT_END, r, ENDPOINT_START)
            r.MoveEndpointByUnit(ENDPOINT_END, TEXTUNIT_CHARACTER, length)
            if self._top_hwnd:
                user32.ShowWindow(self._top_hwnd, SW_RESTORE)
                user32.SetForegroundWindow(self._top_hwnd)
            r.ScrollIntoView(True)
            r.Select()
            return True, ""
        except Exception as e:
            return False, f"редактор не даёт выделять: {e}"

    def _read_full(self, tp, caret):
        t0 = time.perf_counter()
        try:
            doc = tp.DocumentRange
            text = doc.GetText(-1) or ""
        except Exception:
            return None

        if len(text) < 40:          # поля тулбаров и статусбар — не модуль
            return None

        offset = 0
        if caret is not None:
            try:
                probe = doc.Clone()
                probe.MoveEndpointByRange(ENDPOINT_END, caret, ENDPOINT_START)
                offset = len(probe.GetText(-1) or "")
            except Exception:
                pass

        before = text[:offset]
        methods = parse_methods(text)
        current = None
        for mth in methods:
            if mth["start"] <= offset <= mth["end"]:
                current = mth
                break
        if current is None:
            for mth in methods:
                if mth["start"] <= offset:
                    current = mth

        _, title = focused_hwnd()
        self._tp = tp
        self._top_hwnd = user32.GetForegroundWindow()

        self.last_full_ms = (time.perf_counter() - t0) * 1000
        # чем дороже чтение, тем реже его повторяем: минимум 1.2 с, дальше x8
        self.min_full_ms = max(1200, self.last_full_ms * 8)

        return {
            "windowTitle": title,
            "textLength": len(text),
            "text": text,
            "cursorOffset": offset,
            "cursorLine": before.count("\n") + 1,
            "cursorColumn": len(before) - (before.rfind("\n") + 1) + 1,
            "region": enclosing_region(text, offset),
            "currentMethod": current,
            "methodCount": len(methods),
            "methodNames": [m["name"] for m in methods],
        }


# ----------------------------------------------------------------------------
# сохранение контекста
# ----------------------------------------------------------------------------

PROMPT_HEADER = """# Контекст из Конфигуратора 1С

Ты помогаешь писать код прямо в Конфигураторе. Отвечай коротко и по делу.
Прежде чем предлагать правку, проверь окружение доступными инструментами:
карточку метода, граф вызовов, кто ещё зовёт этот код. Не выдумывай
методы БСП и общих модулей — убедись, что они есть в этой конфигурации.

Если предлагаешь код на вставку — положи его отдельным блоком BSL,
без пояснений внутри блока.
"""


def save_context(ctx, note="", diagnostics=None, all_diagnostics=None):
    cur = ctx.get("currentMethod")
    text = ctx["text"]

    method_src = ""
    if cur:
        method_src = text[cur["start"]:cur["end"]]

    rel = ctx["cursorOffset"] - (cur["start"] if cur else 0)
    marked = method_src
    if cur and 0 <= rel <= len(method_src):
        marked = method_src[:rel] + "<КУРСОР>" + method_src[rel:]

    # метод-монстр целиком в промпт не лезет — режем окном вокруг курсора
    trimmed = None
    mlines = marked.splitlines()
    if len(mlines) > MAX_METHOD_LINES:
        caret_line = marked[:marked.find("<КУРСОР>")].count("\n") if "<КУРСОР>" in marked else 0
        lo = max(0, caret_line - WINDOW_LINES)
        hi = min(len(mlines), caret_line + WINDOW_LINES)
        head = mlines[0]                       # сигнатура метода
        chunk = mlines[lo:hi]
        parts = []
        if lo > 0:
            parts.append(head)
            parts.append(f"    // … пропущено {lo - 1} строк …")
        parts.extend(chunk)
        if hi < len(mlines):
            parts.append(f"    // … пропущено {len(mlines) - hi} строк …")
        marked = "\n".join(parts)
        trimmed = {"totalLines": len(mlines), "shown": [lo + 1, hi]}

    all_methods = parse_methods(text)
    callees, callers = nearest_dependencies(text, all_methods, cur)

    payload = {
        "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "windowTitle": ctx["windowTitle"],
        "note": note,
        "cursor": {
            "line": ctx["cursorLine"],
            "column": ctx["cursorColumn"],
            "offset": ctx["cursorOffset"],
        },
        "region": ctx["region"],
        "currentMethod": cur,
        "moduleStats": {
            "chars": ctx["textLength"],
            "methods": ctx["methodCount"],
        },
        "methodNames": ctx["methodNames"],
        "currentMethodSource": method_src,
        "promptTrimmed": trimmed,
        "callees": [m["name"] for m in callees],
        "callers": [m["name"] for m in callers],
        "diagnosticsCount": len(diagnostics or []),
    }

    json_path = os.path.join(CONTEXT_DIR, "context.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # полный модуль — отдельно, чтобы не раздувать json
    module_path = os.path.join(CONTEXT_DIR, "module.bsl")
    with open(module_path, "w", encoding="utf-8") as f:
        f.write(text)

    lines = [PROMPT_HEADER]
    if note:
        lines.append(f"**Вопрос разработчика:** {note}\n")
    lines.append(f"- Окно: `{ctx['windowTitle']}`")
    lines.append(f"- Модуль: {ctx['textLength']} символов, {ctx['methodCount']} методов")
    lines.append(f"- Курсор: строка {L(ctx['cursorLine'])}, колонка {ctx['cursorColumn']}")
    if ctx["region"]:
        lines.append(f"- Область: `{ctx['region']}`")
    if cur:
        lines.append(f"- Текущий метод: `{cur['kind']} {cur['name']}` (строка {L(cur['line'])})")
    if trimmed:
        lines.append(f"- Метод длинный ({trimmed['totalLines']} строк) — ниже показаны "
                     f"строки {trimmed['shown'][0]}–{trimmed['shown'][1]} вокруг курсора; "
                     f"полный текст в `module.bsl`")
    lines.append("\n## Текущий метод (позиция курсора помечена `<КУРСОР>`)\n")
    lines.append("```bsl")
    lines.append(marked if marked else "(курсор вне процедуры)")
    lines.append("```")
    # диагностика BSL Language Server: сперва по текущему методу, потом счётчик
    if diagnostics:
        lo = cur["line"] if cur else 0
        hi = (cur["line"] + len(text[cur["start"]:cur["end"]].splitlines())) if cur else 0
        here = [d for d in diagnostics if lo <= d["line"] <= hi]
        lines.append("\n## BSL Language Server\n")
        if here:
            lines.append("В текущем методе:\n")
            for d in here:
                lines.append(f"- строка {L(d['line'])}: **{d['severity']}** "
                             f"{d['message']} `{d['code']}`")
        else:
            lines.append("В текущем методе замечаний нет.")
        others = len(full or diagnostics) - len(here)
        if others > 0:
            lines.append(f"\nПо всему модулю ещё {others} замечаний "
                         f"(см. `diagnostics.json`).")

    if callees:
        lines.append("\n## Что вызывает текущий метод (из этого же модуля)\n")
        for m in callees:
            lines.append(f"### {m['kind']} {m['name']} — строка {L(m['line'])}\n")
            lines.append("```bsl")
            lines.append(_trim(text[m["start"]:m["end"]]))
            lines.append("```")

    if callers:
        lines.append("\n## Кто вызывает текущий метод\n")
        for m in callers:
            lines.append(f"- `{m['kind']} {m['name']}` — строка {L(m['line'])}")

    lines.append(f"\nПолный текст модуля: `{module_path}` "
                 f"(вызовы за пределы модуля здесь не показаны)")

    full = all_diagnostics if all_diagnostics is not None else diagnostics
    if full is not None:
        with open(os.path.join(CONTEXT_DIR, "diagnostics.json"), "w",
                  encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False, indent=2)

    prompt_path = os.path.join(CONTEXT_DIR, "prompt.md")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return prompt_path


LS_DEFAULT_CONFIG = {
    "$schema": "https://1c-syntax.github.io/bsl-language-server/configuration/schema.json",
    "language": "ru",
    "diagnostics": {
        # по умолчанию сервер считает диагностику только при сохранении файла,
        # а мы файл не сохраняем — поэтому переключаем на набор текста
        "computeTrigger": "onType",
        "mode": "ON",
        "skipSupport": "never",
    },
}


def _write_default_ls_config(path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(LS_DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return None


def navigator_alive(url):
    try:
        host_port = url.split("//", 1)[-1]
        host, _, port = host_port.partition(":")
        port = int(port.split("/")[0] or 80)
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# глобальные хоткеи в отдельном потоке
# ----------------------------------------------------------------------------

def hotkey_loop(events, stop):
    if not user32.RegisterHotKey(None, HK_CAPTURE, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("A")):
        events.put(("error", "не удалось занять Ctrl+Alt+A"))
    if not user32.RegisterHotKey(None, HK_INSERT, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("V")):
        events.put(("error", "не удалось занять Ctrl+Alt+V"))
    if not user32.RegisterHotKey(None, HK_TOGGLE, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("S")):
        events.put(("error", "не удалось занять Ctrl+Alt+S"))

    msg = wt.MSG()
    while not stop.is_set():
        got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
        if got:
            if msg.message == WM_HOTKEY:
                events.put(("hotkey", int(msg.wParam)))
        else:
            time.sleep(0.03)

    user32.UnregisterHotKey(None, HK_CAPTURE)
    user32.UnregisterHotKey(None, HK_INSERT)
    user32.UnregisterHotKey(None, HK_TOGGLE)


# ----------------------------------------------------------------------------
# окно
# ----------------------------------------------------------------------------

BG = "#1e1f22"
FG = "#e6e6e6"
DIM = "#9aa0a6"
OK = "#7ec699"
WARN = "#e0a458"
BAD = "#e06c75"


class Clip:
    def __init__(self, root):
        self.root = root
        self.reader = Reader()
        self.ctx = None
        self.last_ctx = None          # последний контекст, снятый в редакторе
        self.last_ctx_at = 0.0
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.diags = []            # всё, что прислал сервер
        self._diag_sig = None      # чтобы не перерисовывать панель впустую
        self._synced_text = None
        self.ls = None

        root.title("Скрепка 1С")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.geometry(CFG.get("geometry") or "380x470+40+40")
        root.minsize(340, 120)

        mono = tkfont.Font(family="Consolas", size=9)
        bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        ui = tkfont.Font(family="Segoe UI", size=9)

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=10, pady=(8, 2))
        self.dot = tk.Label(head, text="●", fg=BAD, bg=BG, font=bold)
        self.dot.pack(side="left")
        lbl_title = tk.Label(head, text="  Скрепка", fg=FG, bg=BG, font=bold)
        lbl_title.pack(side="left")
        for w in (head, lbl_title, self.dot):
            w.bind("<Double-Button-1>", lambda e: self.toggle_compact())
        self.nav = tk.Label(head, text="", fg=DIM, bg=BG, font=ui)
        self.nav.pack(side="right")

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        self.body = body

        self.l_module = tk.Label(body, text="—", fg=DIM, bg=BG, font=ui,
                                 anchor="w", justify="left", wraplength=310)
        self.l_module.pack(fill="x")
        self.l_method = tk.Label(body, text="курсор не в модуле", fg=FG, bg=BG,
                                 font=mono, anchor="w")
        self.l_method.pack(fill="x", pady=(4, 0))
        posrow = tk.Frame(body, bg=BG)
        posrow.pack(fill="x")
        self.l_pos = tk.Label(posrow, text="", fg=DIM, bg=BG, font=mono, anchor="w")
        self.l_pos.pack(side="left")
        # если нумерация разошлась с Конфигуратором — подкрутить на месте
        tk.Button(posrow, text="+1", command=lambda: self.nudge_lines(1),
                  bg="#33363b", fg=DIM, relief="flat", font=("Segoe UI", 8),
                  padx=4, pady=0, borderwidth=0, cursor="hand2",
                  activebackground="#41454b").pack(side="right")
        tk.Button(posrow, text="−1", command=lambda: self.nudge_lines(-1),
                  bg="#33363b", fg=DIM, relief="flat", font=("Segoe UI", 8),
                  padx=4, pady=0, borderwidth=0, cursor="hand2",
                  activebackground="#41454b").pack(side="right", padx=(0, 3))

        self.l_qhint = tk.Label(body, text="вопрос (необязательно):", fg=DIM, bg=BG,
                                font=ui, anchor="w")
        self.l_qhint.pack(fill="x", pady=(8, 0))
        self.entry = tk.Entry(body, bg="#2b2d31", fg=FG, insertbackground=FG,
                              relief="flat", font=ui)
        self.entry.pack(fill="x", ipady=3)

        diagbar = tk.Frame(body, bg=BG)
        diagbar.pack(fill="x", pady=(10, 2))
        self.diagbar = diagbar
        self.l_diaghead = tk.Label(diagbar, text="BSL LS: выключен", fg=DIM, bg=BG,
                                   font=ui, anchor="w")
        self.l_diaghead.pack(side="left")

        self.var_here = tk.BooleanVar(value=bool(CFG.get("diagOnlyCurrentMethod", True)))
        self.var_sev = tk.IntVar(value=int(CFG.get("diagMinSeverity", 2)))

        self.b_sev = tk.Button(diagbar, command=self.cycle_severity, bg="#33363b",
                               fg=DIM, activebackground="#41454b", relief="flat",
                               font=("Segoe UI", 8), padx=6, pady=0, borderwidth=0,
                               cursor="hand2")
        self.b_sev.pack(side="right")
        tk.Checkbutton(diagbar, text="только этот метод", variable=self.var_here,
                       command=self.render_diags, bg=BG, fg=DIM, font=("Segoe UI", 8),
                       selectcolor="#2b2d31", activebackground=BG, activeforeground=FG,
                       borderwidth=0, highlightthickness=0).pack(side="right", padx=(0, 6))
        self.t_diags = tk.Text(body, height=9, bg="#2b2d31", fg=FG, relief="flat",
                               font=mono, wrap="none", insertbackground=FG,
                               highlightthickness=0)
        self.t_diags.pack(fill="both", expand=True)
        self.t_diags.tag_configure("err", foreground=BAD)
        self.t_diags.tag_configure("warn", foreground=WARN)
        self.t_diags.tag_configure("info", foreground=DIM)
        self.t_diags.tag_configure("here", background="#3a3d42")
        self.t_diags.configure(state="disabled")
        self.t_diags.bind("<Button-1>", self.on_diag_click)
        self.t_diags.configure(cursor="hand2")

        btns = tk.Frame(root, bg=BG)
        btns.pack(fill="x", padx=10, pady=(6, 4))
        self.btns = btns
        self._button(btns, "Снять контекст", self.capture).pack(side="left")
        self._button(btns, "Вставить ответ", self.insert_answer).pack(side="left", padx=(6, 0))
        self._button(btns, "Папка", self.open_folder).pack(side="right")

        self.status = tk.Label(root, text="Ctrl+Alt+A контекст · Ctrl+Alt+V вставить · "
                                          "Ctrl+Alt+S скрыть",
                               fg=DIM, bg=BG, font=ui, anchor="w", wraplength=320,
                               justify="left")
        self.status.pack(fill="x", padx=10, pady=(0, 8))

        threading.Thread(target=hotkey_loop, args=(self.events, self.stop),
                         daemon=True).start()

        self._snapshot_layout()
        self.apply_compact()

        self.start_ls()

        self.tick_nav = 0
        self.poll()
        self.drain()
        root.protocol("WM_DELETE_WINDOW", self.quit)

    def start_ls(self):
        if not CFG.get("bslLsEnabled", True):
            return
        jar = CFG.get("bslLsJar") or bslls.find_jar([CFG["workdir"]])
        if not jar:
            self.l_diaghead.config(
                text="BSL LS: jar не найден — путь в clip.config.json", fg=WARN)
            return
        ls_cfg = CFG.get("bslLsConfig") or os.path.join(HERE, ".bsl-language-server.json")
        if not os.path.exists(ls_cfg):
            ls_cfg = _write_default_ls_config(ls_cfg)

        self.ls = bslls.BslLs(
            jar=jar,
            java=bslls.find_java(CFG.get("javaCommand", "")),
            config_path=ls_cfg,
            log_path=os.path.join(CONTEXT_DIR, "bslls.log"),
            trace_path=os.path.join(CONTEXT_DIR, "lsp.log"),
            on_diagnostics=lambda items: self.events.put(("diag", items)),
            on_status=lambda text: self.events.put(("lsstatus", text)),
        )
        self.ls.start(CFG["workdir"])

    # что прячется в свёрнутом виде; остаётся шапка, метод, позиция и статус
    COMPACT_HIDDEN = ("l_module", "l_qhint", "entry", "diagbar", "t_diags", "btns")

    def _snapshot_layout(self):
        """Запоминаем порядок и параметры упаковки — иначе после сворачивания
        виджеты вернутся не на свои места."""
        self._layout = []
        for master in (self.root, self.body):
            for w in master.pack_slaves():
                try:
                    info = dict(w.pack_info())
                except Exception:
                    continue
                info.pop("in", None)
                self._layout.append((w, info))

    def toggle_compact(self):
        CFG["compact"] = not CFG.get("compact", False)
        self.apply_compact()
        save_config()

    def apply_compact(self):
        compact = CFG.get("compact", False)
        hidden = {getattr(self, n, None) for n in self.COMPACT_HIDDEN}

        for w, _ in self._layout:
            w.pack_forget()
        for w, info in self._layout:
            if compact and w in hidden:
                continue
            try:
                w.pack(**info)
            except Exception:
                pass

        if compact:
            self.root.geometry("300x96")
        else:
            self.root.geometry(CFG.get("geometry") or "380x470")

    def on_diag_click(self, event):
        row = int(self.t_diags.index(f"@{event.x},{event.y}").split(".")[0])
        shown = self.visible_diags()
        if not (1 <= row <= len(shown)):
            return
        ok, err = self.reader.goto_line(shown[row - 1]["line"])
        if ok:
            self.say(f"перешёл на строку {L(shown[row - 1]['line'])}", OK)
        else:
            self.say(err, WARN)

    def toggle_visible(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()
        else:
            self.remember_geometry()
            self.root.withdraw()

    def remember_geometry(self):
        if CFG.get("compact"):
            return
        try:
            CFG["geometry"] = self.root.geometry()
        except Exception:
            pass

    def nudge_lines(self, delta):
        CFG["lineOffset"] = int(CFG.get("lineOffset", 0)) + delta
        save_config()
        self._diag_sig = None
        self.render_diags()
        self.say(f"поправка нумерации строк: {CFG['lineOffset']:+d} (сохранено)", DIM)

    SEV_LABEL = {1: "только ошибки", 2: "+замечания", 4: "всё"}

    def cycle_severity(self):
        order = [1, 2, 4]
        cur = self.var_sev.get()
        self.var_sev.set(order[(order.index(cur) + 1) % len(order)] if cur in order else 2)
        self._diag_sig = None
        self.render_diags()

    def method_bounds(self):
        ctx = self.ctx or self.last_ctx
        if ctx and ctx.get("currentMethod"):
            m = ctx["currentMethod"]
            lo = m["line"]
            return lo, lo + len(ctx["text"][m["start"]:m["end"]].splitlines())
        return -1, -1

    def visible_diags(self):
        lo, hi = self.method_bounds()
        sev = self.var_sev.get()
        out = []
        for d in self.diags:
            if d["severityCode"] > sev:
                continue
            if self.var_here.get() and not (lo <= d["line"] <= hi):
                continue
            out.append(d)
        return out

    def render_diags(self):
        ctx = self.ctx or self.last_ctx
        lo, hi = self.method_bounds()
        shown = self.visible_diags()

        self.b_sev.config(text=self.SEV_LABEL.get(self.var_sev.get(), "+замечания"))

        errors = sum(1 for d in shown if d["severityCode"] == 1)
        head = f"BSL LS: {len(shown)}"
        if len(self.diags) != len(shown):
            head += f" из {len(self.diags)}"
        if errors:
            head += f" · ошибок {errors}"
        self.l_diaghead.config(text=head, fg=BAD if errors else DIM)

        self.t_diags.configure(state="normal")
        self.t_diags.delete("1.0", "end")
        if not shown:
            self.t_diags.insert("end", "  чисто\n", ("info",))
        for d in shown:
            tag = "err" if d["severityCode"] == 1 else (
                "warn" if d["severityCode"] == 2 else "info")
            here = lo <= d["line"] <= hi
            mark = "▸ " if here else "  "
            self.t_diags.insert("end", f"{mark}{L(d['line']):>5}  {d['message']}\n",
                                (tag,) + (("here",) if here else ()))
        self.t_diags.configure(state="disabled")
        for i, d in enumerate(shown, start=1):
            if lo <= d["line"] <= hi:
                self.t_diags.see(f"{i}.0")
                break

    def _button(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg="#33363b", fg=FG,
                         activebackground="#41454b", activeforeground=FG,
                         relief="flat", padx=8, pady=3, font=("Segoe UI", 9),
                         cursor="hand2", borderwidth=0)

    # --- цикл опроса ---------------------------------------------------------
    def poll(self):
        try:
            ctx = self.reader.read()
        except Exception as e:
            ctx = None
            self.say(f"ошибка чтения: {e}", BAD)

        self.ctx = ctx
        if ctx:
            self.last_ctx = ctx
            self.last_ctx_at = time.time()
            self.dot.config(fg=OK)
            title = ctx["windowTitle"]
            self.l_module.config(text=title[:90] or "—")
            cur = ctx["currentMethod"]
            if cur:
                self.l_method.config(text=f"{cur['kind']} {cur['name']}", fg=FG)
            else:
                self.l_method.config(text="вне процедуры", fg=WARN)
            self.l_pos.config(
                text=f"стр {L(ctx['cursorLine'])}:{ctx['cursorColumn']}  "
                     f"· {ctx['textLength']} симв · {ctx['methodCount']} методов "
                     f"· {self.reader.last_read_ms:.0f}/{self.reader.last_full_ms:.0f} мс")
        elif self.last_ctx:
            # фокус ушёл (например, вы печатаете вопрос здесь) — держим последнее
            self.dot.config(fg=WARN)
            old = self.last_ctx
            cur = old["currentMethod"]
            self.l_module.config(text=(old["windowTitle"] or "—")[:90])
            self.l_method.config(
                text=(f"{cur['kind']} {cur['name']}" if cur else "вне процедуры"), fg=DIM)
            ago = int(time.time() - self.last_ctx_at)
            foreign = getattr(self.reader, "last_foreign", "")
            tail = f"· {foreign}" if foreign else ""
            self.l_pos.config(
                text=f"стр {L(old['cursorLine'])}:{old['cursorColumn']}  "
                     f"· запомнено {ago} с назад {tail}")
        else:
            self.dot.config(fg=BAD)
            foreign = getattr(self.reader, "last_foreign", "")
            if foreign:
                self.l_method.config(text=f"чужое окно: {foreign}", fg=DIM)
                self.l_pos.config(text="не читаю — только 1С")
            else:
                self.l_method.config(text="курсор не в модуле", fg=DIM)
                self.l_pos.config(text="")

        if self.ls is not None:
            sig = (len(self.diags), self.method_bounds(),
                   self.var_sev.get(), self.var_here.get())
            if sig != self._diag_sig:
                self._diag_sig = sig
                self.render_diags()

        # отдаём текст модуля языковому серверу, только когда он реально изменился
        if ctx is not None and self.ls is not None and ctx["text"] != self._synced_text:
            try:
                with open(LIVE_PATH, "w", encoding="utf-8") as f:
                    f.write(ctx["text"])
                # пока сервер не готов, sync вернёт False — тогда текст остаётся
                # неотправленным и уйдёт на следующем тике, а не потеряется
                if self.ls.sync(LIVE_PATH, ctx["text"]):
                    self._synced_text = ctx["text"]
            except Exception as e:
                self.say(f"BSL LS: {e}", WARN)

        self.tick_nav += 1
        if self.tick_nav % 10 == 1:
            url = CFG.get("navigatorUrl", "")
            name = CFG.get("navigatorName") or "навигатор"
            if url:
                up = navigator_alive(url)
                self.nav.config(text=f"{name} ✓" if up else f"{name} ✗",
                                fg=OK if up else BAD)
            else:
                self.nav.config(text="")

        self.root.after(CFG["pollMs"], self.poll)

    def drain(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "hotkey":
                    if payload == HK_CAPTURE:
                        self.capture()
                    elif payload == HK_INSERT:
                        self.insert_answer()
                    elif payload == HK_TOGGLE:
                        self.toggle_visible()
                elif kind == "diag":
                    self.diags = payload
                    self._diag_sig = None
                    self.render_diags()
                elif kind == "lsstatus":
                    self.l_diaghead.config(text=payload, fg=DIM)
                elif kind == "error":
                    self.say(payload, WARN)
        except queue.Empty:
            pass
        self.root.after(60, self.drain)

    # --- действия ------------------------------------------------------------
    def capture(self):
        try:
            fresh = self.reader.read(force=True)
        except Exception:
            fresh = None
        if fresh:
            self.ctx = self.last_ctx = fresh
            self.last_ctx_at = time.time()
        ctx = self.ctx or self.last_ctx
        if not ctx:
            self.say("ещё ни разу не видел редактор — щёлкните в модуль", WARN)
            return
        stale = self.ctx is None
        note = self.entry.get().strip()
        try:
            path = save_context(ctx, note, self.visible_diags(), self.diags)
        except Exception as e:
            self.say(f"не сохранилось: {e}", BAD)
            return
        cur = ctx["currentMethod"]
        name = cur["name"] if cur else "модуль"
        if stale:
            ago = int(time.time() - self.last_ctx_at)
            self.say(f"снят запомненный контекст: {name} ({ago} с назад)", WARN)
        else:
            self.say(f"контекст снят: {name} → context\\prompt.md", OK)
        if CFG.get("claudeCommand"):
            threading.Thread(target=self.run_claude, args=(path,), daemon=True).start()

    def run_claude(self, prompt_path):
        """Работает, когда в конфиге прописан claudeCommand (CLI разрешат — включится)."""
        cmd = [CFG["claudeCommand"], "-p"]
        if CFG.get("claudeMcpConfig"):
            cmd += ["--mcp-config", CFG["claudeMcpConfig"]]
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                prompt = f.read()
            res = subprocess.run(cmd, input=prompt, capture_output=True,
                                 text=True, encoding="utf-8", timeout=300)
            out = (res.stdout or res.stderr or "").strip()
            with open(os.path.join(CONTEXT_DIR, "reply.md"), "w", encoding="utf-8") as f:
                f.write(out)
            self.events.put(("error", "ответ агента: context\\reply.md"))
        except FileNotFoundError:
            self.events.put(("error", "claude CLI не найден — уберите claudeCommand из конфига"))
        except Exception as e:
            self.events.put(("error", f"claude: {e}"))

    def insert_answer(self):
        path = os.path.join(CFG["workdir"], CFG["answerFile"])
        if not os.path.exists(path):
            self.say(f"нет файла {CFG['answerFile']} — положите в него код", WARN)
            return
        with open(path, "r", encoding="utf-8") as f:
            code = f.read()
        if not code.strip():
            self.say("файл ответа пуст", WARN)
            return
        if CFG.get("stripIndentOnInsert"):
            code = "\n".join(ln.lstrip("\t ") for ln in code.splitlines())

        # вставляем только в 1С: промах фокусом иначе напечатает код в браузер
        ok, exe = is_editor_window()
        if not ok:
            self.say(f"в фокусе {exe or 'не 1С'} — вставлять туда не буду", WARN)
            return

        hwnd, _ = focused_hwnd()
        if not hwnd:
            self.say("не нашёл окно с фокусом", BAD)
            return
        self.say(f"вставляю {len(code)} символов…", DIM)
        threading.Thread(target=self._do_insert, args=(hwnd, code), daemon=True).start()

    def _do_insert(self, hwnd, code):
        try:
            post_text(hwnd, code)
            self.events.put(("error", "вставлено"))
        except Exception as e:
            self.events.put(("error", f"вставка не удалась: {e}"))

    def open_folder(self):
        os.startfile(CONTEXT_DIR)

    def say(self, text, color=DIM):
        self.status.config(text=text, fg=color)

    def quit(self):
        self.remember_geometry()
        save_config()
        if self.ls is not None:
            self.ls.stop()
        self.stop.set()
        self.root.destroy()


def main():
    if os.name != "nt":
        print("Только Windows.")
        return
    root = tk.Tk()
    Clip(root)
    root.mainloop()


if __name__ == "__main__":
    main()
