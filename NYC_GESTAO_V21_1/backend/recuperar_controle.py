"""Recupera localmente o acesso ao proprietário do NYC Gestão.

Execute com a API parada, no diretório backend da instalação que atende /controle.
O utilitário não cria banco SQLite vazio nem altera dados operacionais.
"""
from __future__ import annotations

import getpass
import os
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parent
if __name__ == "__main__":
    os.chdir(BACKEND_DIR)

_process_database_url = os.environ.get("DATABASE_URL")
_env_values = dotenv_values(BACKEND_DIR / ".env")
if _process_database_url:
    DATABASE_URL_SOURCE = "variável de ambiente do servidor"
elif _env_values.get("DATABASE_URL"):
    DATABASE_URL_SOURCE = "backend/.env"
else:
    DATABASE_URL_SOURCE = "configuração padrão do sistema"
load_dotenv(BACKEND_DIR / ".env", override=False)

from app.database import DATABASE_URL, SessionLocal, engine  # noqa: E402
from app.models import AuthVersion, Company, PlatformAccount, Product, Sale  # noqa: E402
from app.security import hash_password  # noqa: E402


def _database_target() -> tuple[str, Path | None]:
    """Return a credential-free target description and SQLite file path."""
    url = make_url(DATABASE_URL)
    if url.drivername.startswith("sqlite"):
        raw_path = url.database or ""
        if raw_path in ("", ":memory:"):
            return f"SQLite em memória ({raw_path or 'sem arquivo'})", None
        path = Path(raw_path)
        if not path.is_absolute():
            # The server launcher and this recovery tool both run from backend/.
            path = (BACKEND_DIR / path).resolve()
        return f"SQLite: {path}", path

    host = url.host or "host não informado"
    port = f":{url.port}" if url.port else ""
    database = url.database or "banco não informado"
    return f"{url.drivername} em {host}{port}, banco {database}", None


def _inspect_active_database() -> tuple[str, list[str], dict[str, int]]:
    target, sqlite_path = _database_target()
    if sqlite_path is not None and not sqlite_path.is_file():
        raise RuntimeError(
            f"O banco configurado não existe: {sqlite_path}\n"
            "Nada foi criado nem alterado. Execute a recuperação dentro da mesma "
            "instalação/pasta usada pelo servidor do /controle."
        )

    try:
        table_names = set(inspect(engine).get_table_names())
    except Exception as exc:
        raise RuntimeError(
            f"Não foi possível abrir o banco configurado ({target}).\n"
            "Confira backend/.env e se o servidor está parado. Nenhum dado foi alterado."
        ) from exc

    if "platform_accounts" not in table_names:
        raise RuntimeError(
            f"O banco encontrado ({target}) não contém a tabela de contas do painel.\n"
            "Isso geralmente indica que esta não é a mesma base usada pelo /controle. "
            "Nada foi alterado."
        )

    counts: dict[str, int] = {}
    with SessionLocal() as db:
        for label, model in (
            ("empresas", Company),
            ("produtos", Product),
            ("vendas", Sale),
        ):
            if model.__tablename__ in table_names:
                counts[label] = db.query(model).count()
    return target, sorted(table_names), counts


def _ensure_auth_version_table() -> None:
    # Create only the token-revocation table if an older install lacks it.
    AuthVersion.__table__.create(bind=engine, checkfirst=True)


def _check_credentials(email: str, password: str) -> str:
    normalized_email = email.strip().lower()
    if "@" not in normalized_email or len(normalized_email) > 200:
        raise ValueError("Informe um e-mail válido.")
    if len(password) < 12:
        raise ValueError("A nova senha deve ter pelo menos 12 caracteres.")
    return normalized_email


def recover_owner(email: str, password: str) -> str:
    """Reset an existing owner account, preserving all business records."""
    normalized_email = _check_credentials(email, password)
    _inspect_active_database()
    _ensure_auth_version_table()

    with SessionLocal() as db:
        account = (
            db.query(PlatformAccount)
            .filter(
                PlatformAccount.email == normalized_email,
                PlatformAccount.is_owner.is_(True),
            )
            .first()
        )
        if account is None:
            owners = (
                db.query(PlatformAccount.email)
                .filter(PlatformAccount.is_owner.is_(True))
                .order_by(PlatformAccount.id)
                .all()
            )
            existing = ", ".join(row[0] for row in owners) or "nenhum proprietário cadastrado"
            raise ValueError(
                f"O e-mail {normalized_email} não é proprietário neste banco. "
                f"Contas encontradas: {existing}. Nenhuma conta foi alterada."
            )

        account.password_hash = hash_password(password)
        account.must_change_password = False
        account.active = True
        token_key = f"platform:{account.id}"
        version = db.get(AuthVersion, token_key)
        if version is None:
            db.add(AuthVersion(subject=token_key, revoked_at=time.time()))
        else:
            version.revoked_at = time.time()
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        return account.email


def create_first_owner(email: str, password: str) -> str:
    """Create the first owner only when the confirmed database has none."""
    normalized_email = _check_credentials(email, password)
    _inspect_active_database()
    _ensure_auth_version_table()

    with SessionLocal() as db:
        if db.query(PlatformAccount).filter(PlatformAccount.is_owner.is_(True)).first():
            raise ValueError("Já existe um proprietário neste banco; use a opção de redefinir.")
        if db.query(PlatformAccount).filter(PlatformAccount.email == normalized_email).first():
            raise ValueError("Esse e-mail já pertence a outra conta. Escolha outro e-mail.")

        account = PlatformAccount(
            email=normalized_email,
            password_hash=hash_password(password),
            is_owner=True,
            active=True,
            must_change_password=False,
        )
        db.add(account)
        try:
            db.flush()
            db.add(AuthVersion(subject=f"platform:{account.id}", revoked_at=time.time()))
            db.commit()
        except Exception:
            db.rollback()
            raise
        return account.email


def _prompt_password() -> str:
    password = getpass.getpass("Nova senha (mínimo 12 caracteres): ")
    repeated = getpass.getpass("Repita a nova senha: ")
    if password != repeated:
        raise ValueError("As senhas não conferem. Nenhuma alteração foi feita.")
    _check_credentials("validacao@local", password)
    return password


def main() -> int:
    print("NYC Gestão — recuperação do acesso ao /controle")
    print("Feche a janela do servidor antes de continuar.\n")
    try:
        target, tables, counts = _inspect_active_database()
    except RuntimeError as exc:
        print(exc)
        return 1

    print(f"Banco que será usado: {target}")
    print(f"Origem da configuração: {DATABASE_URL_SOURCE}")
    if counts:
        print("Dados localizados: " + ", ".join(f"{value} {name}" for name, value in counts.items()))

    with SessionLocal() as db:
        owners = (
            db.query(PlatformAccount)
            .filter(PlatformAccount.is_owner.is_(True))
            .order_by(PlatformAccount.id)
            .all()
        )

    if owners:
        print("\nProprietários encontrados:")
        for index, owner in enumerate(owners, start=1):
            state = "ativo" if owner.active else "inativo (será reativado)"
            print(f"  {index}. {owner.email} — {state}")
        default_email = owners[0].email if len(owners) == 1 else ""
        entered = input(f"Digite o número ou e-mail da conta [{default_email}]: ").strip()
        if not entered and default_email:
            email = default_email
        elif entered.isdigit() and 1 <= int(entered) <= len(owners):
            email = owners[int(entered) - 1].email
        else:
            email = entered
        if email.strip().lower() not in {owner.email.lower() for owner in owners}:
            print("E-mail não corresponde a um proprietário encontrado. Nenhuma alteração foi feita.")
            return 1
        action = "redefinir a senha de"
    else:
        configured_email = os.getenv("OWNER_EMAIL", "").strip().lower()
        print("\nNenhum proprietário foi encontrado nesta base.")
        entered = input(f"E-mail para criar o primeiro proprietário [{configured_email}]: ").strip()
        email = entered or configured_email
        if not email:
            print("Informe um e-mail para o primeiro proprietário. Nenhuma alteração foi feita.")
            return 1
        action = "criar o primeiro proprietário"

    print(f"\nConfirme o banco acima. Digite SIM para {action} {email}:")
    if input("> ").strip().upper() != "SIM":
        print("Cancelado. Nenhuma alteração foi feita.")
        return 1

    try:
        password = _prompt_password()
        if owners:
            result_email = recover_owner(email, password)
            result = "Senha redefinida"
        else:
            result_email = create_first_owner(email, password)
            result = "Primeiro proprietário criado"
    except (RuntimeError, ValueError) as exc:
        print(f"\n{exc}")
        return 1

    print(f"\n{result} para {result_email}.")
    print("Entre em http://127.0.0.1:8000/controle com esse e-mail e a senha criada agora.")
    print("Se o painel estiver em outro endereço/porta, use o endereço da janela do servidor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
