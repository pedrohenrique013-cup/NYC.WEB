"""Central de inicialização local do NYC Gestão."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

START_DIR = Path(__file__).resolve().parent
PROJECT_DIR = START_DIR.parent
BACKEND_DIR = PROJECT_DIR / "backend"
PAGE = START_DIR / "central.html"
LOG_DIR = PROJECT_DIR / "logs"
BACKUP_DIR = PROJECT_DIR / "dados" / "backups"
EXPECTED_API_VERSION = "21.1.0"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env", override=False)

from sqlalchemy import inspect  # noqa: E402

from app.database import SessionLocal, engine  # noqa: E402
from app.main import startup  # noqa: E402
from app.models import Company, Employee, PlatformAccount, Product, Sale  # noqa: E402
from recuperar_controle import _database_target  # noqa: E402


def api_health() -> dict[str, object]:
    try:
        with urlopen("http://127.0.0.1:8000/health", timeout=0.6) as response:
            data = json.loads(response.read().decode("utf-8"))
            version = str(data.get("version", ""))
            online = response.status == 200
            return {
                "online": online,
                "app": data.get("app", "NYC Gestão"),
                "version": version,
                "expected_version": EXPECTED_API_VERSION,
                "compatible": online and version == EXPECTED_API_VERSION,
            }
    except (URLError, TimeoutError, OSError, ValueError):
        return {
            "online": False,
            "app": "NYC Gestão",
            "version": "",
            "expected_version": EXPECTED_API_VERSION,
            "compatible": False,
        }


def incompatible_api_message(api: dict[str, object]) -> str:
    version = str(api.get("version") or "desconhecida")
    return (
        f"Foi detectado um servidor antigo ou incompatível (versão {version}) na porta 8000. "
        f"Esta instalação exige a API {EXPECTED_API_VERSION}. Se a Central antiga estiver aberta, clique em "
        "“Parar sistema”; caso contrário, feche a janela ou terminal que iniciou o servidor. "
        "Fechar só a aba do navegador não encerra o servidor. "
        "A Central não interrompe nem apaga dados de um servidor que não iniciou."
    )


def database_status() -> dict[str, object]:
    target, sqlite_path = _database_target()
    result: dict[str, object] = {
        "target": target,
        "type": "sqlite" if sqlite_path is not None else "server",
        "path": str(sqlite_path) if sqlite_path is not None else "",
        "state": "unavailable",
        "message": "Aguardando verificação da base.",
        "counts": {},
        "owner_emails": [],
        "employee_count": 0,
        "can_prepare": False,
    }

    if sqlite_path is not None and not sqlite_path.is_file():
        result.update(
            state="missing",
            message="Arquivo de banco ainda não existe. Só prepare se esta for uma instalação nova ou se você confirmou o caminho.",
            can_prepare=True,
        )
        return result

    try:
        tables = set(inspect(engine).get_table_names())
    except Exception:
        result.update(
            state="unavailable",
            message="Não foi possível conectar. Confira backend/.env e o serviço do banco. Nada foi criado.",
        )
        return result

    if not tables:
        result.update(
            state="empty",
            message="A base está vazia. Prepare somente se este for o banco correto para uma instalação nova.",
            can_prepare=True,
        )
        return result
    if "platform_accounts" not in tables:
        result.update(
            state="invalid",
            message="A base tem tabelas, mas não corresponde ao formato deste sistema. Nada será inicializado automaticamente.",
        )
        return result

    counts: dict[str, int] = {}
    owner_emails: list[str] = []
    employee_count = 0
    try:
        with SessionLocal() as db:
            for label, model in (("empresas", Company), ("produtos", Product), ("vendas", Sale)):
                if model.__tablename__ in tables:
                    counts[label] = db.query(model).count()
            if "platform_accounts" in tables:
                owner_emails = [row[0] for row in db.query(PlatformAccount.email).filter(PlatformAccount.is_owner.is_(True)).order_by(PlatformAccount.email).all()]
            if "employees" in tables:
                employee_count = db.query(Employee).count()
    except Exception:
        result.update(state="invalid", message="A base foi localizada, mas não pôde ser lida com segurança. Faça uma cópia antes de qualquer ação.")
        return result

    result.update(
        state="ready",
        message="Base reconhecida. Os registros existentes foram lidos sem alteração.",
        counts=counts,
        owner_emails=owner_emails,
        employee_count=employee_count,
        can_prepare=True,
    )
    return result


def create_backup() -> str:
    target, sqlite_path = _database_target()
    if sqlite_path is None:
        raise ValueError("Cópia automática disponível somente para banco SQLite. Faça backup no provedor PostgreSQL antes de migrar.")
    if not sqlite_path.is_file():
        raise ValueError(f"Banco SQLite não encontrado: {sqlite_path}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = BACKUP_DIR / f"nyc_gestao_{stamp}.sqlite3"
    source_db = sqlite3.connect(str(sqlite_path), timeout=10)
    backup_db = sqlite3.connect(str(destination), timeout=10)
    try:
        source_db.backup(backup_db)
    except Exception:
        backup_db.close()
        source_db.close()
        destination.unlink(missing_ok=True)
        raise
    backup_db.close()
    source_db.close()
    return str(destination)


def prepare_database(payload: dict[str, object]) -> dict[str, object]:
    active_api = api_health()
    if active_api["online"]:
        raise ValueError("Pare o sistema antes de preparar o banco. " + (incompatible_api_message(active_api) if not active_api["compatible"] else "Use a Central que iniciou o servidor."))
    status = database_status()
    if status["state"] in ("invalid", "unavailable"):
        raise ValueError(str(status["message"]))
    if not payload.get("confirm"):
        raise ValueError("Confirme que o caminho mostrado pertence a esta instalação.")
    if status["state"] in ("missing", "empty") and not payload.get("new_database_confirm"):
        raise ValueError("Confirme explicitamente a criação da base vazia.")

    backup_path = ""
    if status["state"] == "ready" and status["type"] == "sqlite":
        backup_path = create_backup()
    elif status["state"] == "ready" and status["type"] == "server" and not payload.get("remote_backup_confirm"):
        raise ValueError("Antes de preparar PostgreSQL, faça backup no provedor e confirme abaixo.")

    startup()
    after = database_status()
    if after["state"] != "ready":
        raise RuntimeError("A preparação foi executada, mas a base ainda não passou na verificação. Consulte backend/logs e não inicie vendas.")
    return {"ok": True, "database": after, "backup": backup_path, "message": "Banco preparado e verificado."}


def launch_accounts_manager() -> str:
    active_api = api_health()
    if active_api["online"]:
        if not active_api["compatible"]:
            raise ValueError(incompatible_api_message(active_api))
        raise ValueError("Pare o NYC Gestão V21.1 pela Central antes de criar ou recuperar contas.")
    manager = BACKEND_DIR / "gestor_acessos_gui.py"
    required = (manager, BACKEND_DIR / "gestor_acessos_core.py", BACKEND_DIR / "gestor_acessos.html")
    if not all(path.is_file() for path in required):
        raise RuntimeError("Gestor de contas incompleto. Reinstale os arquivos da pasta INICIAR_SISTEMA.")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if sys.platform == "win32" else 0
    subprocess.Popen([sys.executable, str(manager), "--mode", "auto"], cwd=str(BACKEND_DIR), creationflags=flags)
    return "Gestor de contas aberto em outra aba local do navegador."


def start_api(server: "CentralServer") -> dict[str, object]:
    existing = api_health()
    if existing["online"]:
        if not existing["compatible"]:
            raise ValueError(incompatible_api_message(existing))
        return {
            "ok": True,
            "online": True,
            "compatible": True,
            "managed": server.api_process is not None and server.api_process.poll() is None,
            "message": "NYC Gestão V21.1 já está ativo em http://127.0.0.1:8000.",
        }

    state = database_status()
    if state["state"] != "ready":
        raise ValueError("Prepare e verifique o banco antes de iniciar o sistema.")
    if not state["owner_emails"]:
        raise ValueError("Crie primeiro uma conta Proprietário em “Contas e acessos”.")

    if server.api_process is not None and server.api_process.poll() is None:
        server.api_process.terminate()
        try:
            server.api_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.api_process.kill()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "api.log"
    try:
        with log_path.open("ab") as log_file:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            server.api_process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
                cwd=str(BACKEND_DIR),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
    except OSError as exc:
        raise RuntimeError(f"Python não conseguiu iniciar a API. Confira {log_path}.") from exc

    for _ in range(24):
        health = api_health()
        if health["compatible"]:
            return {"ok": True, "online": True, "compatible": True, "managed": True, "message": "NYC Gestão V21.1 iniciado em http://127.0.0.1:8000."}
        if health["online"] and not health["compatible"]:
            raise ValueError(incompatible_api_message(health))
        if server.api_process.poll() is not None:
            break
        time.sleep(0.25)
    if server.api_process.poll() is not None:
        raise RuntimeError(f"A API encerrou durante a inicialização. Abra o log: {log_path}")
    return {"ok": True, "online": False, "compatible": False, "managed": True, "message": "API iniciando. Aguarde alguns segundos e atualize o status."}


def stop_api(server: "CentralServer") -> dict[str, object]:
    active_api = api_health()
    if active_api["online"] and (server.api_process is None or server.api_process.poll() is not None):
        if not active_api["compatible"]:
            return {"ok": False, "message": incompatible_api_message(active_api)}
        return {"ok": False, "message": "O sistema foi iniciado fora desta Central. Feche a janela que iniciou o servidor."}
    if server.api_process is not None and server.api_process.poll() is None:
        server.api_process.terminate()
        try:
            server.api_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.api_process.kill()
        server.api_process = None
    return {"ok": True, "message": "Sistema parado."}


def full_status(server: "CentralServer") -> dict[str, object]:
    return {
        "project": str(PROJECT_DIR),
        "python": sys.version.split()[0],
        "env_exists": (BACKEND_DIR / ".env").is_file(),
        "database": database_status(),
        "api": api_health(),
        "managed_api": server.api_process is not None and server.api_process.poll() is None,
        "logs": str(LOG_DIR / "api.log"),
    }


class CentralServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server: CentralServer

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _host_ok(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return self.headers.get("Host", "") == expected and self.client_address[0] in ("127.0.0.1", "::1")

    def _cookie_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie.get("NYC-Startup-Session").value if cookie.get("NYC-Startup-Session") else ""
        except Exception:
            return ""

    def _authorized(self, token: str | None = None) -> bool:
        if not self._host_ok():
            return False
        supplied = token if token is not None else self.headers.get("X-Startup-Token", "")
        return hmac.compare_digest(str(supplied), self.server.setup_token)

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            token = parse_qs(parsed.query).get("token", [""])[0] or self._cookie_token()
            if not self._authorized(token):
                self._send(403, b"Sessao encerrada. Execute novamente a Central de Inicializacao.", "text/plain; charset=utf-8")
                return
            page = PAGE.read_text(encoding="utf-8").replace("__MODE__", self.server.mode).replace("__TOKEN__", self.server.setup_token)
            cookie = f"NYC-Startup-Session={self.server.setup_token}; HttpOnly; SameSite=Strict; Path=/"
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Set-Cookie": cookie})
            return
        if parsed.path == "/api/status":
            if not self._authorized():
                self._json(403, {"error": "Sessão local inválida."})
                return
            try:
                self._json(200, full_status(self.server))
            except Exception:
                self._json(500, {"error": "Não foi possível ler o status da instalação."})
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(403, {"error": "Sessão local inválida."})
            return
        origin = self.headers.get("Origin")
        expected_origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        if origin and origin != expected_origin:
            self._json(403, {"error": "Origem inválida."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16_384:
                self._json(413, {"error": "Solicitação inválida."})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Dados inválidos.")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "Dados inválidos."})
            return

        try:
            if self.path == "/api/database/prepare":
                result = prepare_database(payload)
            elif self.path == "/api/database/backup":
                result = {"ok": True, "backup": create_backup(), "message": "Cópia de segurança criada."}
            elif self.path == "/api/accounts/open":
                result = {"ok": True, "message": launch_accounts_manager()}
            elif self.path == "/api/system/start":
                result = start_api(self.server)
            elif self.path == "/api/system/stop":
                result = stop_api(self.server)
            elif self.path == "/api/central/close":
                result = {"ok": True, "message": "Central fechada. O sistema ficará ativo se já estiver iniciado."}
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(404, {"error": "Ação não encontrada."})
                return
            self._json(200, result)
        except (ValueError, RuntimeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "A ação não foi concluída. Consulte a mensagem e o log da instalação."})


def main() -> int:
    parser = argparse.ArgumentParser(description="Central local de inicialização do NYC Gestão")
    parser.add_argument("--mode", choices=("normal",), default="normal")
    args = parser.parse_args()
    if not PAGE.is_file():
        print("central.html não foi encontrado na pasta INICIAR_SISTEMA.")
        return 1
    server = CentralServer(("127.0.0.1", 0), Handler)
    server.setup_token = secrets.token_urlsafe(32)
    server.mode = args.mode
    server.api_process = None
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={server.setup_token}"
    print("NYC Gestão — Central de Inicialização. Mantenha esta janela aberta.")
    print(f"Se o navegador não abrir sozinho, acesse: {url}")
    webbrowser.open(url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if server.api_process is not None and server.api_process.poll() is not None:
            server.api_process = None
        server.server_close()
    print("Central encerrada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
