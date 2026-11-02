from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
class PlatformLogin(BaseModel): email: str; password: str
class CompanyIn(BaseModel): name: str = Field(min_length=2, max_length=160); code: str = Field(min_length=2, max_length=40)
class EmployeeLogin(BaseModel): company_id: int; username: str; password: str
class EmployeeCreate(BaseModel): username: str = Field(min_length=2, max_length=100); full_name: str = ''; password: str = Field(min_length=8); role: str = 'user'
class EmployeeOut(BaseModel):
 model_config = ConfigDict(from_attributes=True)
 id: int; username: str; full_name: str; role: str; active: bool
class ProductIn(BaseModel):
 model_config = ConfigDict(allow_inf_nan=False)
 sku: str = Field(min_length=1, max_length=80); barcode: str = Field(default='', max_length=80); name: str = Field(min_length=1, max_length=180); category: str = 'Geral'; unit: str = 'un'; cost_price: float = Field(ge=0); sale_price: float = Field(ge=0); stock: float = Field(ge=0); min_stock: float = Field(ge=0)
class ContactIn(BaseModel): name: str; document: str = ''; phone: str = ''; email: str = ''; address: str = ''; notes: str = ''
class SaleItemIn(BaseModel):
 model_config = ConfigDict(allow_inf_nan=False)
 product_id: int; quantity: float = Field(gt=0, le=1000000)
class SalePaymentIn(BaseModel):
 model_config = ConfigDict(allow_inf_nan=False)
 method: Literal['Dinheiro', 'PIX', 'Cartão de crédito', 'Cartão de débito', 'Transferência']
 amount: float = Field(gt=0, le=1000000000000)
class SaleIn(BaseModel):
 model_config = ConfigDict(allow_inf_nan=False)
 payment_method: Literal['Dinheiro', 'PIX', 'Cartão de crédito', 'Cartão de débito', 'Cartão', 'Transferência'] = 'Dinheiro'
 items: list[SaleItemIn] = Field(min_length=1, max_length=500)
 request_id: str = Field(min_length=16, max_length=64)
 received_amount: float | None = Field(default=None, ge=0)
 expected_total: float = Field(gt=0)
 pix_confirmed: bool = False
 discount_percent: float = Field(default=0, ge=0, le=100)
 payments: list[SalePaymentIn] | None = Field(default=None, min_length=1, max_length=5)
class CashAmount(BaseModel):
 model_config = ConfigDict(allow_inf_nan=False)
 amount: float = Field(ge=0)

class PasswordChange(BaseModel):
 current_password: str
 new_password: str = Field(min_length=8)
