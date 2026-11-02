"""Servidor local Python para criar contas e definir permissões no NYC Gestão."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BACKEND_DIR = Path(__file__).resolve().parent
if __name__ == "__main__":
    os.chdir(BACKEND_DIR)

# Esta importação carrega backend/.env antes de criar o engine do banco.
from recuperar_controle import DATABASE_URL_SOURCE, _inspect_active_database, recover_owner  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Company, Employee, PlatformAccount, PlatformAccess  # noqa: E402
from gestor_acessos_core import create_access  # noqa: E402


PAGE = Path(__file__).with_name("gestor_acessos.html")


def status_data() -> dict[str, object]:
    target, tables, counts = _inspect_active_database()
    with SessionLocal() as db:
        companies = []
        if "companies" in tables:
            companies = [
                {"id": c.id, "name": c.name, "code": c.code}
                for c in db.query(Company).filter(Company.active.is_(True)).order_by(Company.name).all()
            ]
        accounts = []
        if "platform_accounts" in tables:
            for account in db.query(PlatformAccount).order_by(PlatformAccount.email).all():
                accesses = db.query(PlatformAccess.company_id).filter_by(account_id=account.id).all()
                accounts.append({
                    "email": account.email,
                    "type": "Proprietário (total)" if account.is_owner else "Plataforma (empresas selecionadas)",
                    "active": bool(account.active),
                    "companies": [row[0] for row in accesses],
                })
        employee_count = db.query(Employee).count() if "employees" in tables else 0
    return {
        "database": target,
        "source": DATABASE_URL_SOURCE,
        "counts": counts,
        "companies": companies,
        "accounts": accounts,
        "employee_count": employee_count,
        "owners": [a["email"] for a in accounts if a["type"] == "Proprietário (total)"],
    }


class AccessServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server: AccessServer

    def log_message(self, _format: str, *args: object) -> None:
        # Do not write the one-time browser token or account data to the console.
        return

    def _host_ok(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return self.headers.get("Host", "") == expected and self.client_address[0] in ("127.0.0.1", "::1")

    def _authorized(self, query_token: str | None = None) -> bool:
        if not self._host_ok():
            return False
        supplied = query_token if query_token is not None else self.headers.get("X-Setup-Token", "")
        return hmac.compare_digest(str(supplied), self.server.setup_token)

    def _send(self, status: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            token = parse_qs(parsed.query).get("token", [""])[0] or self._cookie_token()
            if not self._authorized(token):
                self._send(403, b"Acesso local expirado. Feche esta janela e execute novamente o gestor.", "text/plain; charset=utf-8")
                return
            try:
                html = PAGE.read_text(encoding="utf-8").replace("__MODE__", self.server.mode)
            except OSError:
                self._send(500, b"Arquivo gestor_acessos.html nao encontrado.", "text/plain; charset=utf-8")
                return
            cookie = f"NYC-Access-Session={self.server.setup_token}; HttpOnly; SameSite=Strict; Path=/"
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Set-Cookie": cookie})
            return
        if parsed.path == "/api/status":
            if not self._authorized():
                self._json(403, {"error": "Sessão local inválida."})
                return
            try:
                self._json(200, status_data())
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception:
                self._json(500, {"error": "Não foi possível ler a base configurada. Confira a pasta backend escolhida."})
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
            if length < 1 or length > 32_768:
                self._json(413, {"error": "Solicitação inválida."})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Dados inválidos.")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "Confira os campos e tente novamente."})
            return

        if self.path == "/api/create":
            try:
                created = create_access(
                    str(payload.get("type", "")),
                    email=str(payload.get("email", "")),
                    username=str(payload.get("username", "")),
                    full_name=str(payload.get("full_name", "")),
                    company_ids=payload.get("company_ids", []),
                    company_id=payload.get("company_id"),
                    employee_role=str(payload.get("employee_role", "user")),
                )
                self._json(201, created)
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception:
                self._json(500, {"error": "Não foi possível salvar a conta. Confira os dados e tente novamente."})
            return

        if self.path == "/api/recover-owner":
            try:
                email = recover_owner(str(payload.get("email", "")), str(payload.get("password", "")))
                self._json(200, {"ok": True, "email": email})
            except (RuntimeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception:
                self._json(500, {"error": "Não foi possível redefinir a senha. Confira a base e tente novamente."})
            return

        if self.path == "/api/stop":
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._json(404, {"error": "Ação não encontrada."})

    def _cookie_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie.get("NYC-Access-Session").value if cookie.get("NYC-Access-Session") else ""
        except Exception:
            return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Gestor local de contas e permissões do NYC Gestão")
    parser.add_argument("--mode", choices=("auto", "create", "recover"), default="auto")
    args = parser.parse_args()
    if not PAGE.is_file():
        print("Arquivo gestor_acessos.html não encontrado na pasta backend.")
        return 1
    server = AccessServer(("127.0.0.1", 0), Handler)
    server.setup_token = secrets.token_urlsafe(32)
    server.mode = args.mode
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={server.setup_token}"
    print("Gestor local aberto. Mantenha esta janela enquanto usa o painel.")
    print(f"Se o navegador não abrir sozinho, acesse: {url}")
    webbrowser.open(url, new=2)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("Gestor encerrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
