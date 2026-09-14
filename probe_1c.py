# -*- coding: utf-8 -*-
"""
probe_1c.py - разведка: можно ли читать текст модуля и позицию курсора
из окна Конфигуратора 1С через Windows UI Automation.

Ничего не меняет, никуда не отправляет. Только читает и пишет JSON рядом с собой.

Запуск:
    pip install comtypes
    python probe_1c.py

После запуска даётся 6 секунд: переключитесь в Конфигуратор, откройте модуль
и поставьте курсор внутрь любой процедуры.

ВАЖНО: если 1С запущена от имени администратора, Python тоже нужно запустить
от администратора, иначе UI Automation не увидит окно.
"""

import io
import json
import os
import re
import sys
import time

# --- вывод кириллицы в консоль Windows ---------------------------------------
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import comtypes
    import comtypes.client
except ImportError:
    print("Нет модуля comtypes. Установите:  pip install comtypes")
    sys.exit(1)

UIA = comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation  # noqa: E402

# идентификаторы паттернов UI Automation
PAT = {
    "Invoke": 10000,
    "Value": 10002,
    "Scroll": 10004,
    "Window": 10009,
    "Text": 10014,
    "LegacyIAccessible": 10018,
}
ENDPOINT_START = 0
ENDPOINT_END = 1

MAX_ELEMENTS = 4000
MAX_DEPTH = 22

CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
}


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def ct_name(code):
    return CONTROL_TYPES.get(code, str(code))


def supported_patterns(elem):
    found = []
    for name, pid in PAT.items():
        try:
            if elem.GetCurrentPattern(pid):
                found.append(name)
        except Exception:
            pass
    return found


def elem_info(elem, depth):
    return {
        "depth": depth,
        "controlType": ct_name(safe(lambda: elem.CurrentControlType, -1)),
        "name": (safe(lambda: elem.CurrentName, "") or "")[:120],
        "className": safe(lambda: elem.CurrentClassName, "") or "",
        "automationId": safe(lambda: elem.CurrentAutomationId, "") or "",
        "hwnd": safe(lambda: elem.CurrentNativeWindowHandle, 0) or 0,
        "patterns": supported_patterns(elem),
    }


def get_text_pattern(elem):
    try:
        raw = elem.GetCurrentPattern(PAT["Text"])
        if not raw:
            return None
        return raw.QueryInterface(UIA.IUIAutomationTextPattern)
    except Exception:
        return None


def document_range(tp):
    for attr in ("DocumentRange", "getDocumentRange", "get_DocumentRange"):
        obj = safe(lambda: getattr(tp, attr))
        if obj is None:
            continue
        return obj() if callable(obj) else obj
    return None


def read_text_element(elem):
    """Пробуем вытащить весь текст, выделение и смещение курсора."""
    tp = get_text_pattern(elem)
    if tp is None:
        return None

    out = {"ok": False}

    doc = document_range(tp)
    if doc is None:
        out["error"] = "DocumentRange недоступен"
        return out

    text = safe(lambda: doc.GetText(-1), None)
    if text is None:
        out["error"] = "GetText вернул ошибку"
        return out

    out["textLength"] = len(text)
    out["head"] = text[:400]

    # --- выделение и позиция курсора ---
    sel_arr = safe(lambda: tp.GetSelection(), None)
    if sel_arr is not None and safe(lambda: sel_arr.Length, 0):
        sel = sel_arr.GetElement(0)
        out["selectionText"] = (safe(lambda: sel.GetText(-1), "") or "")[:200]

        # смещение = длина текста от начала документа до начала выделения
        probe = safe(lambda: doc.Clone(), None)
        if probe is not None:
            try:
                probe.MoveEndpointByRange(ENDPOINT_END, sel, ENDPOINT_START)
                before = probe.GetText(-1) or ""
                offset = len(before)
                out["cursorOffset"] = offset
                out["cursorLine"] = before.count("\n") + 1
                out["cursorColumn"] = len(before) - (before.rfind("\n") + 1) + 1
                out["contextAroundCursor"] = text[max(0, offset - 200):offset + 200]
                out["currentMethod"] = find_method(text, offset)
            except Exception as e:
                out["selectionError"] = repr(e)
    else:
        out["selectionText"] = None

    out["ok"] = out["textLength"] > 0
    return out


METHOD_RE = re.compile(
    r"^[ \t]*(?:&[^\r\n]*\r?\n[ \t]*)*(Процедура|Функция|Procedure|Function)\s+([A-Za-zА-Яа-яЁё_][\w]*)",
    re.IGNORECASE | re.MULTILINE,
)


def find_method(text, offset):
    """Имя процедуры/функции, внутри которой стоит курсор."""
    current = None
    for m in METHOD_RE.finditer(text):
        if m.start() <= offset:
            current = {"kind": m.group(1), "name": m.group(2), "start": m.start()}
        else:
            break
    return current


def walk(iuia, root):
    """Обход дерева контролов вширь-вглубь с ограничителями."""
    walker = iuia.ControlViewWalker
    collected = []
    text_candidates = []
    stack = [(root, 0)]
    count = 0

    while stack and count < MAX_ELEMENTS:
        elem, depth = stack.pop()
        count += 1
        info = elem_info(elem, depth)
        collected.append(info)
        if "Text" in info["patterns"]:
            text_candidates.append((elem, info))
        if depth >= MAX_DEPTH:
            continue
        child = safe(lambda: walker.GetFirstChildElement(elem), None)
        kids = []
        while child:
            kids.append(child)
            child = safe(lambda: walker.GetNextSiblingElement(child), None)
            if len(kids) > 400:
                break
        for k in reversed(kids):
            stack.append((k, depth + 1))

    return collected, text_candidates, count


def main():
    print("probe_1c.py — разведка окна Конфигуратора 1С")
    for i in range(6, 0, -1):
        print(f"  переключитесь в Конфигуратор и поставьте курсор в модуль... {i}", end="\r")
        time.sleep(1)
    print(" " * 70)

    iuia = comtypes.client.CreateObject(CUIAutomation, interface=IUIAutomation)
    desktop = iuia.GetRootElement()

    report = {"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "windows": []}

    # --- 1. что сейчас в фокусе -----------------------------------------------
    focused = safe(lambda: iuia.GetFocusedElement(), None)
    if focused is not None:
        finfo = elem_info(focused, -1)
        finfo["textRead"] = read_text_element(focused)
        report["focusedElement"] = finfo
        print("Элемент в фокусе:", finfo["controlType"], "|", finfo["className"],
              "| паттерны:", ",".join(finfo["patterns"]) or "нет")
        tr = finfo["textRead"]
        if tr and tr.get("ok"):
            print("  ЕСТЬ ТЕКСТ. Символов:", tr["textLength"],
                  "| курсор:", tr.get("cursorOffset"),
                  "| метод:", (tr.get("currentMethod") or {}).get("name"))

    # --- 2. окна Конфигуратора -------------------------------------------------
    walker = iuia.ControlViewWalker
    child = safe(lambda: walker.GetFirstChildElement(desktop), None)
    targets = []
    while child:
        name = safe(lambda: child.CurrentName, "") or ""
        cls = safe(lambda: child.CurrentClassName, "") or ""
        if ("онфигуратор" in name) or cls.startswith("V8"):
            targets.append((child, name, cls))
        child = safe(lambda: walker.GetNextSiblingElement(child), None)

    if not targets:
        print("\nОкно Конфигуратора не найдено среди окон верхнего уровня.")
        print("Проверьте: Конфигуратор запущен? Не от администратора ли он, "
              "когда Python — нет?")
    for elem, name, cls in targets:
        print(f"\nОкно: {name!r} (класс {cls}) — обходим дерево...")
        collected, text_candidates, count = walk(iuia, elem)
        print(f"  элементов просмотрено: {count}, с TextPattern: {len(text_candidates)}")

        win = {"title": name, "className": cls, "elementsScanned": count,
               "textElements": [], "tree": collected[:600]}

        for el, info in text_candidates[:8]:
            res = read_text_element(el)
            entry = dict(info)
            entry["textRead"] = res
            win["textElements"].append(entry)
            if res and res.get("ok"):
                print(f"  [+] {info['controlType']}/{info['className']}: "
                      f"{res['textLength']} символов, курсор {res.get('cursorOffset')}, "
                      f"метод {(res.get('currentMethod') or {}).get('name')}")
            else:
                print(f"  [-] {info['controlType']}/{info['className']}: текст не прочитан")

        report["windows"].append(win)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "probe_1c_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nПодробный отчёт: {out_path}")
    print("Пришлите этот файл — по нему станет ясно, за что цеплять «скрепку».")


if __name__ == "__main__":
    main()
