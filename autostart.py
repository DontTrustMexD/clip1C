# -*- coding: utf-8 -*-
"""
autostart.py — включить или выключить запуск скрепки вместе с Windows.

    python autostart.py on
    python autostart.py off
    python autostart.py status

Кладёт .bat в папку автозагрузки текущего пользователя. Права администратора
не нужны, на другие учётные записи не влияет. Запуск идёт через pythonw,
поэтому чёрное окно консоли не мелькает.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STARTUP = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                       "Start Menu", "Programs", "Startup")
BAT = os.path.join(STARTUP, "clip-1c.bat")


def pythonw():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def on():
    if not os.path.isdir(STARTUP):
        print(f"Не нашёл папку автозагрузки: {STARTUP}")
        return 1
    body = (
        "@echo off\r\n"
        f'cd /d "{HERE}"\r\n'
        f'start "" "{pythonw()}" "{os.path.join(HERE, "clip.py")}"\r\n'
    )
    with open(BAT, "w", encoding="cp866", errors="replace") as f:
        f.write(body)
    print(f"Включено. Файл: {BAT}")
    return 0


def off():
    if os.path.exists(BAT):
        os.remove(BAT)
        print("Выключено.")
    else:
        print("И так не было включено.")
    return 0


def status():
    print(("включено: " + BAT) if os.path.exists(BAT) else "выключено")
    return 0


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    sys.exit({"on": on, "off": off, "status": status}.get(cmd, status)())
