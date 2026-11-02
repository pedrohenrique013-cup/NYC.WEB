"""Server-side helpers for the Mercado Pago Orders API PIX flow."""
import base64
import hashlib
import http.client
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from .security import SECRET_KEY


class MercadoPagoError(Exception):
    def __init__(self, status_code=502):
        super().__init__("Falha na comunicação com o Mercado Pago")
        self.status_code = status_code


def _fernet():
    derived = hashlib.sha256(("nyc.pix.mercadopago.v1:" + SECRET_KEY).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_private(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_private(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise RuntimeError("Não foi possível abrir uma credencial PIX protegida. Verifique a chave local do sistema.") from exc


def mp_request(method: str, path: str, access_token: str, payload: dict | None = None,
               idempotency_key: str | None = None) -> dict:
    """Call a fixed Mercado Pago host; redirects and arbitrary hosts are not followed."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if payload is not None else None
    headers = {"Authorization": "Bearer " + access_token, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    connection = http.client.HTTPSConnection("api.mercadopago.com", timeout=20)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise MercadoPagoError(502)
        if response.status < 200 or response.status >= 300:
            raise MercadoPagoError(response.status)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MercadoPagoError(502) from exc
        if not isinstance(data, dict):
            raise MercadoPagoError(502)
        return data
    except MercadoPagoError:
        raise
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise MercadoPagoError(502) from exc
    finally:
        connection.close()


def create_order(access_token: str, idempotency_key: str, payload: dict) -> dict:
    return mp_request("POST", "/v1/orders", access_token, payload, idempotency_key)


def get_order(access_token: str, provider_order_id: str) -> dict:
    return mp_request("GET", "/v1/orders/" + quote(provider_order_id, safe=""), access_token)


def money_cents(value) -> int | None:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return None
        normalized = amount.quantize(Decimal("0.01"))
        if normalized != amount:
            return None
        return int(normalized * 100)
    except (InvalidOperation, TypeError, ValueError):
        return None


def pix_qr(order: dict) -> dict:
    payments = (order.get("transactions") or {}).get("payments") or []
    for payment in payments:
        method = payment.get("payment_method") or {}
        if method.get("id") == "pix" and method.get("type") == "bank_transfer":
            return {
                "qr_code": str(method.get("qr_code") or ""),
                "qr_code_base64": str(method.get("qr_code_base64") or ""),
                "ticket_url": str(method.get("ticket_url") or ""),
            }
    return {"qr_code": "", "qr_code_base64": "", "ticket_url": ""}


def credited_pix_order(order: dict, expected_cents: int) -> bool:
    """Only accept a fully credited order whose total and PIX transaction match."""
    if order.get("status") != "processed" or order.get("status_detail") != "accredited":
        return False
    if order.get("country_code") and str(order.get("country_code")).upper() not in {"BR", "BRA"}:
        return False
    if money_cents(order.get("total_amount")) != expected_cents:
        return False
    payments = (order.get("transactions") or {}).get("payments") or []
    matching = []
    for payment in payments:
        method = payment.get("payment_method") or {}
        if method.get("id") == "pix" and method.get("type") == "bank_transfer":
            matching.append(money_cents(payment.get("amount")))
    return bool(matching) and None not in matching and sum(matching) == expected_cents


def normalized_order_status(order: dict) -> str:
    status = str(order.get("status") or "").lower()
    detail = str(order.get("status_detail") or "").lower()
    if status == "processed" and detail == "accredited":
        return "credito_confirmado"
    if status == "processed":
        return "inconsistente"
    if status in {"refunded", "charged_back"}:
        return "inconsistente"
    if status in {"canceled", "failed"}:
        return "encerrado"
    if status == "action_required" and detail in {"waiting_transfer", "waiting_payment"}:
        return "aguardando_pagamento"
    return "processando"
