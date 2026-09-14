# -*- coding: utf-8 -*-
"""
fetch_bslls.py — скачивает BSL Language Server в папку проекта.

Нужен только на машине, где не стоит Phoenix BSL: скрепка сначала ищет его
лаунчер, и лишь потом — jar рядом с собой.

    python fetch_bslls.py

Имя файла не угадывается: скрипт спрашивает GitHub API, какой ассет лежит
в последнем релизе, и берёт тот, что заканчивается на -exec.jar.
"""

import json
import os
import sys
import urllib.request

API = "https://api.github.com/repos/1c-syntax/bsl-language-server/releases/latest"
HERE = os.path.dirname(os.path.abspath(__file__))


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} ТБ"


def main():
    print("Спрашиваю GitHub про последний релиз…")
    req = urllib.request.Request(API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "clip-bslls-fetch",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print(f"Не получилось: {e}")
        print("\nСкачайте вручную со страницы релизов и положите файл сюда:")
        print("  https://github.com/1c-syntax/bsl-language-server/releases")
        print(f"  {HERE}")
        return 1

    tag = data.get("tag_name", "?")
    assets = data.get("assets", [])
    target = None
    for a in assets:
        if a["name"].lower().endswith("-exec.jar"):
            target = a
            break
    if target is None:
        print(f"В релизе {tag} нет файла *-exec.jar. Что там есть:")
        for a in assets:
            print(f"  {human(a['size']):>8}  {a['name']}")
        return 1

    dest = os.path.join(HERE, target["name"])
    if os.path.exists(dest):
        print(f"Уже есть: {dest}")
        return 0

    print(f"Качаю {target['name']} ({human(target['size'])}) из {tag}…")
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(target["browser_download_url"],
                                       headers={"User-Agent": "clip-bslls-fetch"}),
                timeout=600) as r, open(tmp, "wb") as f:
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                pct = done * 100 // max(1, target["size"])
                print(f"  {pct:>3}%  {human(done)}", end="\r")
        os.replace(tmp, dest)
    except Exception as e:
        print(f"\nОборвалось: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return 1

    print(f"\nГотово: {dest}")
    print("Скрепка подхватит его сама — путь прописывать не нужно.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
