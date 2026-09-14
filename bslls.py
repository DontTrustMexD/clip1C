# -*- coding: utf-8 -*-
"""
bslls.py — тонкий LSP-клиент к BSL Language Server.

Держит один живой процесс `java -jar bsl-language-server.jar` (без флагов он
стартует как language server на stdio) и шлёт ему текст модуля. Сервер сам
присылает диагностику — JVM не перезапускается, поэтому проверка идёт
за десятки миллисекунд, а не за секунды.

Наружу: BslLs(jar).start(), .sync(path, text), колбэк on_diagnostics(list).
"""

import json
import os
import subprocess
import threading
import time
from urllib.parse import quote

JAR_PATTERNS = ("bsl-language-server",)
LAUNCHER_NAMES = ("bsl-language-server.exe",)

SEVERITY = {1: "ошибка", 2: "замечание", 3: "инфо", 4: "подсказка"}


def find_jar(extra_dirs=()):
    """Ищем сервер: сначала exe-лаунчер со своей JRE, потом голый jar.

    Лаунчер предпочтительнее — он тащит собственный рантайм, и Java из PATH
    не нужна вовсе."""
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")

    candidates = list(extra_dirs) + [
        os.path.dirname(os.path.abspath(__file__)),
        os.path.join(pf, "phoenixbsl"),
        os.path.join(pf86, "phoenixbsl"),
        os.path.join(pf, "Phoenix BSL"),
        os.path.join(pf86, "Phoenix BSL"),
        os.path.join(la, "Programs", "phoenixbsl") if la else "",
        os.path.join(la, "Phoenix BSL") if la else "",
        os.path.join(home, "phoenixbsl"),
    ]

    fallback_jar = None
    for base in candidates:
        if not base or not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            if root[len(base):].count(os.sep) >= 4:
                dirs[:] = []
                continue
            for fn in files:
                low = fn.lower()
                if low in LAUNCHER_NAMES:
                    return os.path.join(root, fn)      # лаунчер выигрывает сразу
                if (fallback_jar is None and low.endswith(".jar")
                        and any(pat in low for pat in JAR_PATTERNS)):
                    fallback_jar = os.path.join(root, fn)
    return fallback_jar


def find_java(configured=""):
    if configured and os.path.exists(configured):
        return configured
    jh = os.environ.get("JAVA_HOME")
    if jh:
        exe = os.path.join(jh, "bin", "java.exe")
        if os.path.exists(exe):
            return exe
    return "java"


def path_to_uri(path):
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p, safe="/:")


class BslLs:
    def __init__(self, jar, java="java", log_path=None, on_diagnostics=None,
                 on_status=None, config_path=None, trace_path=None):
        self.jar = jar
        self.java = java
        self.config_path = config_path
        self.log_path = log_path
        self.on_diagnostics = on_diagnostics or (lambda diags: None)
        self.on_status = on_status or (lambda text: None)

        self.proc = None
        self._id = 0
        self._versions = {}
        self._opened = set()
        self._lock = threading.Lock()
        self.ready = False
        self.last_error = ""
        self.trace_path = trace_path
        self.published = 0          # сколько раз сервер присылал диагностику

    def _trace(self, text):
        if not self.trace_path:
            return
        try:
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')}  {text}\n")
        except Exception:
            pass

    # --- запуск ---------------------------------------------------------
    def start(self, root_dir):
        if not self.jar or not os.path.exists(self.jar):
            self.last_error = "jar не найден"
            self.on_status("BSL LS: нет jar")
            return False
        # .exe — это jpackage-лаунчер со своей JRE, его запускаем как есть
        if self.jar.lower().endswith(".exe"):
            cmd = [self.jar]
        else:
            cmd = [self.java, "-Xmx1g", "-jar", self.jar]

        # без своего конфига сервер считает диагностику только по сохранению
        if self.config_path and os.path.exists(self.config_path):
            cmd += ["-c", self.config_path]

        try:
            errlog = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errlog,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            self.last_error = "нечем запустить: нет java и нет лаунчера"
            self.on_status("BSL LS: нет java")
            return False
        except Exception as e:
            self.last_error = repr(e)
            self.on_status(f"BSL LS: {e}")
            return False

        self._trace(f"старт: {' '.join(cmd)}")
        threading.Thread(target=self._reader, daemon=True).start()
        self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": path_to_uri(root_dir),
            "clientInfo": {"name": "clip", "version": "1"},
            "capabilities": {
                "textDocument": {
                    "synchronization": {"dynamicRegistration": False},
                    "publishDiagnostics": {"relatedInformation": False},
                }
            },
        })
        self.on_status("BSL LS: запускается…")
        return True

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self._notify("exit", {})
                self.proc.terminate()
        except Exception:
            pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # --- протокол -------------------------------------------------------
    def _write(self, payload):
        if not self.alive():
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        head = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._lock:
            try:
                self.proc.stdin.write(head + body)
                self.proc.stdin.flush()
            except Exception as e:
                self.last_error = repr(e)

    def _request(self, method, params):
        self._id += 1
        self._write({"jsonrpc": "2.0", "id": self._id, "method": method,
                     "params": params})
        return self._id

    def _notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _reader(self):
        out = self.proc.stdout
        while True:
            try:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        self.on_status("BSL LS: процесс завершился")
                        return
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if not length:
                    continue
                raw = b""
                while len(raw) < length:
                    chunk = out.read(length - len(raw))
                    if not chunk:
                        return
                    raw += chunk
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                continue

            if msg.get("method") == "textDocument/publishDiagnostics":
                pr = msg.get("params") or {}
                self.published += 1
                self._trace(f"<- publishDiagnostics: {len(pr.get('diagnostics') or [])} "
                            f"шт, uri={pr.get('uri')}")
                self._on_publish(pr)
            elif msg.get("method") in ("window/logMessage", "window/showMessage"):
                self._trace(f"<- {msg['method']}: "
                            f"{(msg.get('params') or {}).get('message', '')[:300]}")
            elif "id" in msg and "result" in msg and not self.ready:
                # ответ на initialize
                self.ready = True
                self._trace("<- initialize ok")
                self._notify("initialized", {})
                self.on_status("BSL LS: готов")
            elif "id" in msg and msg.get("method"):
                # сервер что-то спрашивает — отвечаем пустотой, чтобы не завис
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": None})

    def _on_publish(self, params):
        items = []
        for d in params.get("diagnostics", []):
            rng = (d.get("range") or {}).get("start") or {}
            items.append({
                "line": int(rng.get("line", 0)) + 1,       # LSP считает с нуля
                "column": int(rng.get("character", 0)) + 1,
                "severity": SEVERITY.get(d.get("severity"), "инфо"),
                "severityCode": d.get("severity", 3),
                "code": str(d.get("code", "")),
                "message": (d.get("message") or "").replace("\n", " ").strip(),
            })
        items.sort(key=lambda x: (x["line"], x["column"]))
        self.on_diagnostics(items)

    # --- работа с текстом -----------------------------------------------
    def sync(self, path, text):
        """Отдать серверу текст модуля. Первый раз didOpen, дальше didChange.

        Возвращает False, если сервер ещё не готов — вызывающий не должен
        считать текст отправленным."""
        if not self.ready:
            self._trace("sync пропущен: сервер ещё не готов")
            return False
        uri = path_to_uri(path)
        if uri not in self._opened:
            self._opened.add(uri)
            self._versions[uri] = 1
            self._trace(f"-> didOpen {uri} ({len(text)} симв)")
            self._notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": "bsl",
                                 "version": 1, "text": text},
            })
        else:
            self._versions[uri] += 1
            self._trace(f"-> didChange v{self._versions[uri]} ({len(text)} симв)")
            self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": self._versions[uri]},
                "contentChanges": [{"text": text}],
            })
        # подстраховка: если сервер всё-таки настроен на onSave, разбудим его
        self._notify("textDocument/didSave", {
            "textDocument": {"uri": uri},
            "text": text,
        })
        return True
