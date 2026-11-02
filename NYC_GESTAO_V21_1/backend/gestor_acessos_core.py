"""Operações de criação de contas, sem interface gráfica."""
from __future__ import annotations

import re
import secrets

from sqlalchemy import func

from recuperar_controle import _inspect_active_database
from app.database import SessionLocal
from app.models import Company, Employee, PlatformAccount, PlatformAccess
from app.security import hash_password


ACCESS_LABELS = {
    "owner": "Proprietário da Central de Controle — acesso total",
    "platform": "Conta da plataforma — empresas selecionadas",
    "employee": "Funcionário da empresa",
}


def create_access(
    access_type: str,
    *,
    email: str = "",
    username: str = "",
    full_name: str = "",
    company_ids: tuple[int, ...] | list[int] = (),
    company_id: int | None = None,
    employee_role: str = "user",
) -> dict[str, str | int | list[str]]:
    """Create one account; return the randomly generated password once."""
    if access_type not in ACCESS_LABELS:
        raise ValueError("Escolha um tipo de acesso válido.")
    target, tables, _counts = _inspect_active_database()
    if "companies" not in tables and access_type != "owner":
        raise ValueError(f"A base {target} não contém empresas. Nenhuma conta foi criada.")

    temporary_password = secrets.token_urlsafe(18)
    with SessionLocal() as db:
        try:
            if access_type in ("owner", "platform"):
                normalized_email = email.strip().lower()
                if len(normalized_email) > 200 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_email):
                    raise ValueError("Informe um e-mail válido para a conta.")
                if access_type == "platform" and not re.fullmatch(r"[A-Za-z0-9._%+\-]+@gmail\.com", normalized_email, re.I):
                    raise ValueError("Para acesso limitado ao Painel Central, use um endereço Gmail.")
                if db.query(PlatformAccount).filter(func.lower(PlatformAccount.email) == normalized_email).first():
                    raise ValueError("Esse e-mail já tem uma conta. Escolha outro ou use o acesso existente.")

                selected_ids = sorted({int(company) for company in company_ids})
                selected_company_labels: list[str] = []
                if access_type == "platform":
                    if not selected_ids:
                        raise ValueError("Selecione pelo menos uma empresa para o acesso limitado.")
                    selected_companies = (
                        db.query(Company.id, Company.name, Company.code)
                        .filter(Company.id.in_(selected_ids), Company.active.is_(True))
                        .all()
                    )
                    if len(selected_companies) != len(selected_ids):
                        raise ValueError("Uma empresa selecionada não existe ou está inativa. Atualize a lista e tente novamente.")
                    selected_company_labels = [f"{company.name} ({company.code})" for company in selected_companies]

                account = PlatformAccount(
                    email=normalized_email,
                    password_hash=hash_password(temporary_password),
                    is_owner=(access_type == "owner"),
                    active=True,
                    must_change_password=True,
                )
                db.add(account)
                db.flush()
                if access_type == "platform":
                    for selected_id in selected_ids:
                        db.add(PlatformAccess(account_id=account.id, company_id=selected_id))
                db.commit()
                return {
                    "login": normalized_email,
                    "type": ACCESS_LABELS[access_type],
                    "temporary_password": temporary_password,
                    "companies": selected_company_labels,
                }

            normalized_username = username.strip()
            if len(normalized_username) < 2 or len(normalized_username) > 100:
                raise ValueError("O nome de usuário deve ter de 2 a 100 caracteres.")
            if employee_role not in ("admin", "user"):
                raise ValueError("Escolha a função Admin ou Operador.")
            if company_id is None:
                raise ValueError("Selecione a empresa deste funcionário.")
            company = db.get(Company, int(company_id))
            if not company or not company.active:
                raise ValueError("A empresa não existe ou está inativa. Atualize a lista e tente novamente.")
            if (
                db.query(Employee)
                .filter(Employee.company_id == company.id, func.lower(Employee.username) == normalized_username.lower())
                .first()
            ):
                raise ValueError("Esse usuário já existe nesta empresa.")
            employee = Employee(
                company_id=company.id,
                username=normalized_username,
                full_name=full_name.strip()[:140],
                password_hash=hash_password(temporary_password),
                role=employee_role,
                active=True,
                must_change_password=True,
            )
            db.add(employee)
            db.commit()
            return {
                "login": normalized_username,
                "type": f"{ACCESS_LABELS['employee']} — {company.name} ({company.code}), função {employee_role}",
                "temporary_password": temporary_password,
                "companies": [f"{company.name} ({company.code})"],
            }
        except Exception:
            db.rollback()
            raise
