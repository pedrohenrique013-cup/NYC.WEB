from datetime import datetime
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base

class Company(Base):
    __tablename__ = 'companies'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Employee(Base):
    __tablename__ = 'employees'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    full_name: Mapped[str] = mapped_column(String(140), default='')
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default='user')
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Product(Base):
    __tablename__ = 'products'
    __table_args__ = (UniqueConstraint('company_id', 'sku', name='uq_product_company_sku'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    sku: Mapped[str] = mapped_column(String(80), index=True)
    barcode: Mapped[str] = mapped_column(String(80), default='', index=True)
    name: Mapped[str] = mapped_column(String(180), index=True)
    category: Mapped[str] = mapped_column(String(100), default='Geral')
    unit: Mapped[str] = mapped_column(String(20), default='un')
    cost_price: Mapped[float] = mapped_column(Float, default=0)
    sale_price: Mapped[float] = mapped_column(Float, default=0)
    stock: Mapped[float] = mapped_column(Float, default=0)
    min_stock: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Contact(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    name: Mapped[str] = mapped_column(String(180), index=True)
    document: Mapped[str] = mapped_column(String(40), default='')
    phone: Mapped[str] = mapped_column(String(40), default='')
    email: Mapped[str] = mapped_column(String(180), default='')
    address: Mapped[str] = mapped_column(String(240), default='')
    notes: Mapped[str] = mapped_column(Text, default='')

class Customer(Contact):
    __tablename__ = 'customers'
class Supplier(Contact):
    __tablename__ = 'suppliers'

class Sale(Base):
    __tablename__ = 'sales'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'))
    total: Mapped[float] = mapped_column(Float, default=0)
    discount_total: Mapped[float] = mapped_column(Float, default=0)
    payment_method: Mapped[str] = mapped_column(String(40), default='Dinheiro')
    pix_verification: Mapped[str] = mapped_column(String(30), default='nao_aplicavel')
    status: Mapped[str] = mapped_column(String(30), default='concluida')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class SaleItem(Base):
    __tablename__ = 'sale_items'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey('sales.id'))
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id'))
    product_name: Mapped[str] = mapped_column(String(180))
    quantity: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float] = mapped_column(Float)
    subtotal: Mapped[float] = mapped_column(Float)
    discount_amount: Mapped[float] = mapped_column(Float, default=0)

class CashSession(Base):
    __tablename__ = 'cash_sessions'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'))
    opening_amount: Mapped[float] = mapped_column(Float, default=0)
    closing_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='aberto')
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlatformAccount(Base):
    __tablename__ = 'platform_accounts'
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class PlatformAccess(Base):
    __tablename__ = 'platform_access'
    __table_args__ = (UniqueConstraint('account_id', 'company_id', name='uq_platform_access'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('platform_accounts.id'), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)

class PixSettings(Base):
    __tablename__ = 'pix_settings'
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), primary_key=True)
    key: Mapped[str] = mapped_column(String(100))
    merchant_name: Mapped[str] = mapped_column(String(25))
    merchant_city: Mapped[str] = mapped_column(String(15))
    provider: Mapped[str] = mapped_column(String(40), default='manual')
    mp_access_token_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)

class PixOrder(Base):
    __tablename__ = 'pix_orders'
    __table_args__ = (UniqueConstraint('company_id', 'request_id', name='uq_pix_order_company_request'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), index=True)
    employee_role: Mapped[str] = mapped_column(String(20), default='user')
    request_id: Mapped[str] = mapped_column(String(64))
    draft_fingerprint: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    external_reference: Mapped[str] = mapped_column(String(100))
    provider_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    sale_id: Mapped[int | None] = mapped_column(ForeignKey('sales.id'), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    request_cipher: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default='criando', index=True)
    provider_status: Mapped[str] = mapped_column(String(40), default='')
    provider_status_detail: Mapped[str] = mapped_column(String(60), default='')
    qr_code: Mapped[str] = mapped_column(Text, default='')
    qr_code_base64: Mapped[str] = mapped_column(Text, default='')
    ticket_url: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class SaleRequest(Base):
    __tablename__ = 'sale_requests'
    __table_args__ = (UniqueConstraint('company_id', 'request_id', name='uq_sale_request'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'))
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'))
    request_id: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    sale_id: Mapped[int | None] = mapped_column(ForeignKey('sales.id'), nullable=True)
    received_amount: Mapped[float] = mapped_column(Float, default=0)
    change_amount: Mapped[float] = mapped_column(Float, default=0)

class AuthVersion(Base):
    __tablename__ = 'auth_versions'
    subject: Mapped[str] = mapped_column(String(80), primary_key=True)
    revoked_at: Mapped[float] = mapped_column(Float)

class SalePayment(Base):
    __tablename__ = 'sale_payments'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey('sales.id'), index=True)
    method: Mapped[str] = mapped_column(String(40))
    amount: Mapped[float] = mapped_column(Float)

class AdminAudit(Base):
    __tablename__ = 'admin_audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str] = mapped_column(String(80), default='')
    details: Mapped[str] = mapped_column(Text, default='{}')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

class PaymentTerminalSettings(Base):
    __tablename__ = 'payment_terminal_settings'
    company_id: Mapped[int] = mapped_column(ForeignKey('companies.id'), primary_key=True)
    provider: Mapped[str] = mapped_column(String(100))
    terminal_name: Mapped[str] = mapped_column(String(100), default='')
    integration_mode: Mapped[str] = mapped_column(String(20), default='manual')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
