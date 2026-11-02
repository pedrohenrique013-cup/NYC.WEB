import os, re, io, csv, secrets
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / '.env')
from datetime import datetime, date, time
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, update
from sqlalchemy.exc import IntegrityError
from .pix import brcode, money, merchant, validate_key
import hashlib, json, threading, uuid
from fastapi.responses import JSONResponse
from openpyxl import Workbook, load_workbook
from .database import Base, engine, get_db
from .models import Company, Employee, Product, Customer, Supplier, Sale, SaleItem, CashSession, PlatformAccount, PlatformAccess, PixSettings, PixOrder, SaleRequest, AuthVersion, SalePayment, AdminAudit, PaymentTerminalSettings
from .schemas import PlatformLogin, CompanyIn, EmployeeLogin, EmployeeCreate, ProductIn, ContactIn, SaleIn, CashAmount, PasswordChange, EmployeeOut, SalePaymentIn
from .security import hash_password, verify_password, create_token, decode_token
from .pix_mercadopago import encrypt_private, decrypt_private, create_order as mp_create_order, get_order as mp_get_order, pix_qr as mp_pix_qr, money_cents as mp_money_cents, credited_pix_order, normalized_order_status, MercadoPagoError
from pydantic import BaseModel, Field, EmailStr

app = FastAPI(title='NYC.WEB.BR Gestão API', version='21.1.0')
origins = [x.strip() for x in os.getenv('CORS_ORIGINS', '').split(',')]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=['*'], allow_headers=['*'])
bearer = HTTPBearer(auto_error=False)

_attempts={}
_attempt_lock=threading.Lock()
@app.middleware('http')
async def protection(request:Request,call_next):
    import time as clock
    if request.url.path.endswith('/login'):
        key=(request.client.host if request.client else 'unknown', request.url.path)
        now=clock.monotonic()
        with _attempt_lock:
            for old in list(_attempts):
                if not _attempts[old] or now-_attempts[old][-1]>60: _attempts.pop(old,None)
            hits=_attempts.setdefault(key,[])
            hits[:]=[t for t in hits if now-t<60]
            if len(hits)>=10: return JSONResponse({'detail':'Muitas tentativas. Aguarde um minuto.'},429,headers={'Retry-After':'60'})
            hits.append(now)
    if request.url.path.startswith('/api/') and request.headers.get('content-length','0').isdigit() and int(request.headers.get('content-length','0'))>10_000_000:
        return JSONResponse({'detail':'Arquivo/solicitação muito grande'},413)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Frame-Options']='DENY'
    if request.url.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
    return response

@app.on_event('startup')
def startup():
    Base.metadata.create_all(bind=engine)
    from sqlalchemy import inspect, text
    migrations = {
        'employees': [('must_change_password', 'BOOLEAN DEFAULT FALSE NOT NULL')],
        'products': [('barcode', "VARCHAR(80) DEFAULT '' NOT NULL")],
        'sales': [('discount_total', 'FLOAT DEFAULT 0 NOT NULL'), ('pix_verification', "VARCHAR(30) DEFAULT 'nao_aplicavel' NOT NULL")],
        'sale_items': [('discount_amount', 'FLOAT DEFAULT 0 NOT NULL')],
        'pix_settings': [('provider', "VARCHAR(40) DEFAULT 'manual' NOT NULL"), ('mp_access_token_cipher', 'TEXT')],
    }
    with engine.begin() as con:
        for table, columns in migrations.items():
            present = {c['name'] for c in inspect(con).get_columns(table)}
            for column, declaration in columns:
                if column not in present:
                    con.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}'))
        table_names=set(inspect(con).get_table_names())
        if {'sales','sale_payments'}.issubset(table_names):
            con.execute(text("UPDATE sales SET pix_verification='conferencia_manual' WHERE pix_verification='nao_aplicavel' AND EXISTS (SELECT 1 FROM sale_payments WHERE sale_payments.sale_id=sales.id AND sale_payments.company_id=sales.company_id AND sale_payments.method='PIX')"))
        con.execute(text('CREATE INDEX IF NOT EXISTS ix_products_barcode ON products (barcode)'))
    from .database import SessionLocal
    db=SessionLocal()
    try:
        if not db.query(PlatformAccount).first():
            email=os.getenv('OWNER_EMAIL','').strip().lower()
            password=os.getenv('OWNER_PASSWORD','')
            if email and password and len(password)>=12:
                db.add(PlatformAccount(email=email,password_hash=hash_password(password),is_owner=True,must_change_password=True))
                db.commit()
            else:
                print('CONFIGURAÇÃO OBRIGATÓRIA: configure OWNER_EMAIL e OWNER_PASSWORD (12+ caracteres) no backend/.env')
    finally: db.close()

def token_payload(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if not credentials: raise HTTPException(401, 'Faça login para continuar')
    payload = decode_token(credentials.credentials)
    if not payload or not isinstance(payload.get('iat'), (float,int)) or not str(payload.get('sub','')).isdigit(): raise HTTPException(401, 'Sessão inválida ou expirada')
    from .database import SessionLocal
    with SessionLocal() as session:
        version = session.get(AuthVersion, payload.get('kind', '') + ':' + payload.get('sub', ''))
        if version and payload.get('iat', 0) <= version.revoked_at:
            raise HTTPException(401, 'Senha alterada. Entre novamente.')
    return payload

def platform_only(payload=Depends(token_payload), db:Session=Depends(get_db)):
    if payload.get('kind') != 'platform': raise HTTPException(403, 'Conta principal necessária')
    try: account=db.get(PlatformAccount,int(payload['sub']))
    except (ValueError,TypeError): account=None
    if not account or not account.active: raise HTTPException(401,'Conta inativa ou sessão inválida')
    if account.must_change_password: raise HTTPException(403,'Troque a senha provisória para continuar')
    return {'account':account}

def platform_authenticated(payload=Depends(token_payload),db:Session=Depends(get_db)):
    if payload.get('kind')!='platform': raise HTTPException(403,'Acesso negado')
    try: account=db.get(PlatformAccount,int(payload['sub']))
    except (ValueError,TypeError): account=None
    if not account or not account.active: raise HTTPException(401,'Conta inválida')
    return account

def owner_only(ctx=Depends(platform_only)):
    if not ctx['account'].is_owner: raise HTTPException(403,'Acesso reservado ao proprietário da plataforma')
    return ctx

def company_access(ctx,db,company_id):
    account=ctx['account']
    if account.is_owner:return True
    return db.query(PlatformAccess).filter_by(account_id=account.id,company_id=company_id).first() is not None

def employee_only(request:Request, payload=Depends(token_payload), db: Session = Depends(get_db)):
    if payload.get('kind') != 'employee' or not payload.get('company_id'): raise HTTPException(401, 'Entre na empresa com seu usuário de funcionário')
    emp = db.get(Employee, int(payload['sub']))
    company = db.get(Company, int(payload['company_id']))
    if not company or not company.active: raise HTTPException(401, 'Empresa inativa')
    if not emp or not emp.active or emp.company_id != int(payload['company_id']): raise HTTPException(401, 'Usuário inativo ou empresa inválida')
    if emp.must_change_password and request.url.path!='/api/auth/change-password': raise HTTPException(403,'Troque sua senha provisória antes de continuar')
    return {'employee': emp, 'company_id': emp.company_id, 'role': emp.role}

def admin_only(ctx=Depends(employee_only)):
    if ctx['role'] != 'admin': raise HTTPException(403, 'Ação permitida somente ao administrador da empresa')
    return ctx

def company_query(db, model, company_id): return db.query(model).filter(model.company_id == company_id)

def audit(db, ctx, action, entity, entity_id='', details=None):
    db.add(AdminAudit(company_id=ctx['company_id'], employee_id=ctx['employee'].id,
                      action=action, entity=entity, entity_id=str(entity_id or ''),
                      details=json.dumps(details or {}, ensure_ascii=False, separators=(',', ':'))))

def calculate_sale(db, company_id, items, discount_percent=0):
    grouped={}
    for item in items: grouped[item.product_id]=grouped.get(item.product_id,0)+item.quantity
    lines=[];gross=money(0)
    for product_id,qty in sorted(grouped.items()):
        p=company_query(db,Product,company_id).filter(Product.id==product_id,Product.active==True).with_for_update().first()
        if not p: raise HTTPException(404,f'Produto {product_id} não encontrado nesta empresa')
        if p.stock<qty: raise HTTPException(409,f'Estoque insuficiente para {p.name}')
        subtotal=money(money(p.sale_price)*money_quantity(qty));gross+=subtotal
        lines.append({'product':p,'quantity':qty,'unit_price':p.sale_price,'gross':subtotal})
    if gross<=0: raise HTTPException(400,'Total da venda deve ser positivo')
    discount=money(gross*money_quantity(discount_percent)/100)
    total=gross-discount
    if total<=0: raise HTTPException(400,'O desconto não pode zerar o total da venda')
    remaining=discount
    for i,line in enumerate(lines):
        allocated=remaining if i==len(lines)-1 else (money(line['gross']*discount/gross) if gross else money(0))
        remaining-=allocated
        line['discount']=allocated
        line['subtotal']=line['gross']-allocated
    return lines,gross,discount,total

@app.get('/health')
def health(): return {'app':'NYC.WEB.BR Gestão','status':'online','version':'21.1.0'}

class PlatformAccountIn(BaseModel):
    email: str
    company_ids: list[int] = []

class PlatformPasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12)

class ResetEmployeeIn(BaseModel):
    company_id: int
    employee_id: int

@app.post('/api/platform/login')
def platform_login(data:PlatformLogin,db:Session=Depends(get_db)):
    email=data.email.strip().lower()
    account=db.query(PlatformAccount).filter_by(email=email).first()
    if not account or not account.active or not verify_password(data.password,account.password_hash): raise HTTPException(401,'E-mail ou senha incorretos')
    return {'access_token':create_token(str(account.id),'platform_owner' if account.is_owner else 'platform_user',kind='platform'),'token_type':'bearer','email':account.email,'must_change_password':account.must_change_password,'is_owner':account.is_owner}

@app.post('/api/platform/change-password')
def platform_change_password(data:PlatformPasswordIn,account=Depends(platform_authenticated),db:Session=Depends(get_db)):
    if not verify_password(data.current_password,account.password_hash):raise HTTPException(400,'Senha atual incorreta')
    if verify_password(data.new_password,account.password_hash):raise HTTPException(400,'Escolha uma senha diferente')
    revoke(db, 'platform', account.id)
    account.password_hash=hash_password(data.new_password)
    account.must_change_password=False
    db.commit()
    return {'ok':True}

@app.get('/api/platform/companies')
def list_companies(db:Session=Depends(get_db),ctx=Depends(platform_only)):
    q=db.query(Company)
    if not ctx['account'].is_owner:
        ids=[a.company_id for a in db.query(PlatformAccess).filter_by(account_id=ctx['account'].id).all()]
        q=q.filter(Company.id.in_(ids))
    return [{'id':c.id,'name':c.name,'code':c.code,'active':c.active} for c in q.order_by(Company.name).all()]

@app.post('/api/platform/companies')
def create_company(data:CompanyIn,db:Session=Depends(get_db),_:dict=Depends(owner_only)):
    code=re.sub(r'[^a-zA-Z0-9_-]','',data.code.strip()).lower()
    if not code:raise HTTPException(400,'Código inválido')
    if db.query(Company).filter(func.lower(Company.code)==code).first():raise HTTPException(409,'Código de empresa já existe')
    company=Company(name=data.name.strip(),code=code)
    db.add(company);db.flush()
    temp=secrets.token_urlsafe(15)
    db.add(Employee(company_id=company.id,username='admin',full_name='Administrador',password_hash=hash_password(temp),role='admin',must_change_password=True))
    db.commit();db.refresh(company)
    return {'id':company.id,'name':company.name,'code':company.code,'initial_admin':'admin','initial_admin_password':temp,'message':'Senha provisória exibida uma única vez.'}

@app.get('/api/owner/accounts')
def owner_accounts(db:Session=Depends(get_db),_=Depends(owner_only)):
    return [{'id':a.id,'email':a.email,'is_owner':a.is_owner,'active':a.active,'must_change_password':a.must_change_password,'company_ids':[x.company_id for x in db.query(PlatformAccess).filter_by(account_id=a.id).all()]} for a in db.query(PlatformAccount).order_by(PlatformAccount.id).all()]

@app.post('/api/owner/accounts')
def owner_create_account(data:PlatformAccountIn,db:Session=Depends(get_db),_=Depends(owner_only)):
    email=data.email.strip().lower()
    if not re.fullmatch(r'[A-Za-z0-9._%+\-]+@gmail\.com',email):raise HTTPException(400,'Informe um Gmail válido')
    if db.query(PlatformAccount).filter_by(email=email).first():raise HTTPException(409,'Gmail já cadastrado')
    ids=set(data.company_ids)
    if ids and db.query(Company).filter(Company.id.in_(ids)).count()!=len(ids):raise HTTPException(400,'Empresa inválida')
    temporary=secrets.token_urlsafe(18)
    a=PlatformAccount(email=email,password_hash=hash_password(temporary),must_change_password=True)
    db.add(a);db.flush()
    for cid in ids:db.add(PlatformAccess(account_id=a.id,company_id=cid))
    db.commit()
    return {'id':a.id,'email':email,'temporary_password':temporary,'message':'Copie a senha agora: não será possível visualizá-la novamente.'}

@app.post('/api/owner/accounts/{account_id}/reset')
def owner_reset_account(account_id:int,db:Session=Depends(get_db),ctx=Depends(owner_only)):
    a=db.get(PlatformAccount,account_id)
    if not a:raise HTTPException(404,'Conta não encontrada')
    if a.id==ctx['account'].id:raise HTTPException(400,'Use a troca de senha para sua própria conta')
    temporary=secrets.token_urlsafe(18)
    revoke(db, 'platform', a.id)
    a.password_hash=hash_password(temporary);a.must_change_password=True
    db.commit()
    return {'temporary_password':temporary,'message':'Nova senha provisória. Copie agora.'}

@app.patch('/api/owner/accounts/{account_id}/toggle')
def owner_toggle_account(account_id:int,db:Session=Depends(get_db),ctx=Depends(owner_only)):
    a=db.get(PlatformAccount,account_id)
    if not a:raise HTTPException(404,'Conta não encontrada')
    if a.id==ctx['account'].id:raise HTTPException(400,'Não pode desativar sua própria conta')
    revoke(db, 'platform', a.id)
    a.active=not a.active;db.commit();return {'active':a.active}

@app.put('/api/owner/accounts/{account_id}/companies')
def owner_set_companies(account_id:int,data:PlatformAccountIn,db:Session=Depends(get_db),_=Depends(owner_only)):
    a=db.get(PlatformAccount,account_id)
    if not a or a.is_owner:raise HTTPException(404,'Conta não encontrada ou é proprietário')
    ids=set(data.company_ids)
    if ids and db.query(Company).filter(Company.id.in_(ids)).count()!=len(ids):raise HTTPException(400,'Empresa inválida')
    db.query(PlatformAccess).filter_by(account_id=account_id).delete()
    for cid in ids:db.add(PlatformAccess(account_id=account_id,company_id=cid))
    db.commit();return {'ok':True}

@app.post('/api/owner/employees/reset')
def owner_reset_employee(data:ResetEmployeeIn,db:Session=Depends(get_db),_=Depends(owner_only)):
    e=db.query(Employee).filter_by(id=data.employee_id,company_id=data.company_id).first()
    if not e:raise HTTPException(404,'Funcionário não encontrado')
    temporary=secrets.token_urlsafe(16)
    revoke(db, 'employee', e.id)
    e.password_hash=hash_password(temporary);e.must_change_password=True
    db.commit();return {'temporary_password':temporary}

@app.get('/api/owner/employees/{company_id}')
def owner_employees(company_id:int,db:Session=Depends(get_db),_=Depends(owner_only)):
    return [{'id':e.id,'username':e.username,'full_name':e.full_name,'role':e.role,'active':e.active} for e in db.query(Employee).filter_by(company_id=company_id).all()]

@app.get('/controle',response_class=HTMLResponse)
def owner_panel():
    return (WEB_DIR/'controle.html').read_text(encoding='utf-8')

@app.post('/api/auth/login')
def employee_login(data: EmployeeLogin, db: Session=Depends(get_db)):
    company=db.get(Company,data.company_id)
    if not company or not company.active: raise HTTPException(404,'Empresa não encontrada ou inativa')
    emp=db.query(Employee).filter(Employee.company_id==company.id,func.lower(Employee.username)==data.username.strip().lower()).first()
    if not emp or not emp.active or not verify_password(data.password,emp.password_hash): raise HTTPException(401,'Usuário ou senha incorretos')
    return {'must_change_password':emp.must_change_password,'access_token':create_token(str(emp.id),emp.role,company.id,'employee'),'token_type':'bearer','user':{'id':emp.id,'username':emp.username,'full_name':emp.full_name,'role':emp.role},'company':{'id':company.id,'name':company.name,'code':company.code}}

@app.get('/api/auth/me')
def me(ctx=Depends(employee_only)):
    e=ctx['employee']; return {'id':e.id,'username':e.username,'full_name':e.full_name,'role':e.role,'company_id':ctx['company_id']}

@app.post('/api/auth/change-password')
def change_password(data:PasswordChange,ctx=Depends(employee_only),db:Session=Depends(get_db)):
    employee=ctx['employee']
    if not verify_password(data.current_password,employee.password_hash): raise HTTPException(400,'A senha atual está incorreta')
    revoke(db, 'employee', employee.id)
    employee.password_hash=hash_password(data.new_password);employee.must_change_password=False;db.commit()
    return {'ok':True,'message':'Senha alterada com sucesso'}

@app.get('/api/users', response_model=list[EmployeeOut])
def users(db:Session=Depends(get_db),ctx=Depends(admin_only)):
    return company_query(db,Employee,ctx['company_id']).order_by(Employee.id).all()
@app.post('/api/users',response_model=EmployeeOut)
def create_user(data:EmployeeCreate,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    if data.role not in ('admin','user'): raise HTTPException(400,'Perfil inválido')
    if company_query(db,Employee,ctx['company_id']).filter(func.lower(Employee.username)==data.username.lower()).first(): raise HTTPException(409,'Usuário já existe nesta empresa')
    e=Employee(company_id=ctx['company_id'],username=data.username.strip(),full_name=data.full_name,password_hash=hash_password(data.password),role=data.role,must_change_password=True)
    db.add(e);db.flush();audit(db,ctx,'funcionario_criado','funcionario',e.id,{'usuario':e.username,'perfil':e.role});db.commit();db.refresh(e);return e
@app.patch('/api/users/{uid}/toggle')
def toggle_user(uid:int,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    e=company_query(db,Employee,ctx['company_id']).filter(Employee.id==uid).first()
    if not e: raise HTTPException(404,'Funcionário não encontrado')
    if e.id==ctx['employee'].id: raise HTTPException(400,'Não pode desativar sua própria conta')
    revoke(db, 'employee', e.id)
    e.active=not e.active;audit(db,ctx,'funcionario_status_alterado','funcionario',e.id,{'ativo':e.active});db.commit();return {'id':e.id,'active':e.active}

@app.post('/api/users/{uid}/reset')
def reset_user(uid:int,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    e=company_query(db,Employee,ctx['company_id']).filter(Employee.id==uid).first()
    if not e:raise HTTPException(404,'Funcionário não encontrado')
    temporary=secrets.token_urlsafe(16)
    revoke(db, 'employee', e.id)
    e.password_hash=hash_password(temporary);e.must_change_password=True
    audit(db,ctx,'senha_funcionario_redefinida','funcionario',e.id,{'usuario':e.username})
    db.commit()
    return {'temporary_password':temporary}

@app.get('/api/products')
def products(q:str='',db:Session=Depends(get_db),ctx=Depends(employee_only)):
    query=company_query(db,Product,ctx['company_id']).filter(Product.active==True)
    if q: query=query.filter(Product.name.ilike(f'%{q}%')|Product.sku.ilike(f'%{q}%')|Product.barcode.ilike(f'%{q}%'))
    return query.order_by(Product.name).all()
@app.post('/api/products')
def create_product(data:ProductIn,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    if company_query(db,Product,ctx['company_id']).filter(func.lower(Product.sku)==data.sku.lower()).first(): raise HTTPException(409,'Código/SKU já cadastrado nesta empresa')
    p=Product(company_id=ctx['company_id'],**data.model_dump());db.add(p);db.flush();audit(db,ctx,'produto_criado','produto',p.id,{'sku':p.sku,'nome':p.name});db.commit();db.refresh(p);return p
@app.put('/api/products/{pid}')
def update_product(pid:int,data:ProductIn,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    p=company_query(db,Product,ctx['company_id']).filter(Product.id==pid).first()
    if not p: raise HTTPException(404,'Produto não encontrado')
    if company_query(db,Product,ctx['company_id']).filter(Product.id!=pid,func.lower(Product.sku)==data.sku.lower()).first(): raise HTTPException(409,'SKU já cadastrado')
    before={'sku':p.sku,'nome':p.name,'preco':p.sale_price,'estoque':p.stock}
    for k,v in data.model_dump().items():setattr(p,k,v)
    audit(db,ctx,'produto_atualizado','produto',p.id,{'antes':before,'sku':p.sku,'nome':p.name,'preco':p.sale_price,'estoque':p.stock})
    db.commit();db.refresh(p);return p
@app.delete('/api/products/{pid}')
def delete_product(pid:int,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    p=company_query(db,Product,ctx['company_id']).filter(Product.id==pid).first()
    if not p:raise HTTPException(404,'Produto não encontrado')
    p.active=False;audit(db,ctx,'produto_desativado','produto',p.id,{'sku':p.sku,'nome':p.name});db.commit();return {'ok':True}

@app.get('/api/products/template.xlsx')
def product_template(ctx=Depends(admin_only)):
    wb=Workbook();ws=wb.active;ws.title='Produtos';ws.append(['sku','barcode','name','category','unit','cost_price','sale_price','stock','min_stock'])
    bio=io.BytesIO();wb.save(bio);bio.seek(0)
    return StreamingResponse(bio,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="modelo_produtos_nyc.xlsx"'})
@app.get('/api/products/export.xlsx')
def export_products(db:Session=Depends(get_db),ctx=Depends(admin_only)):
    wb=Workbook();ws=wb.active;ws.title='Produtos';ws.append(['sku','barcode','name','category','unit','cost_price','sale_price','stock','min_stock'])
    for p in company_query(db,Product,ctx['company_id']).filter(Product.active==True).order_by(Product.name): ws.append([p.sku,p.barcode,p.name,p.category,p.unit,p.cost_price,p.sale_price,p.stock,p.min_stock])
    bio=io.BytesIO();wb.save(bio);bio.seek(0)
    return StreamingResponse(bio,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="estoque_nyc.xlsx"'})
def _parse_import_xlsx(raw, db, company_id):
    """Validates the entire spreadsheet without writing to the database."""
    from decimal import Decimal, InvalidOperation
    import unicodedata
    from openpyxl.utils.exceptions import InvalidFileException
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(400, 'Não foi possível ler o arquivo. Use Excel .xlsx válido.')
    def norm(v):
        t=unicodedata.normalize('NFKD',str(v or '').strip().lower())
        return re.sub(r'_+','_',re.sub(r'[^a-z0-9]+','_', ''.join(c for c in t if not unicodedata.combining(c)))).strip('_')
    aliases={
      'codigo':'sku','cod':'sku','cod_produto':'sku','codigo_produto':'sku','codigo_do_produto':'sku','codigo_interno':'sku',
      'codigo_de_barras':'barcode','codigo_barras':'barcode','cod_barras':'barcode','gtin':'barcode','barcode':'barcode','ean':'barcode','ean13':'barcode','referencia':'sku','ref':'sku',
      'produto':'name','nome':'name','nome_produto':'name','nome_do_produto':'name','descricao':'name','descricao_do_produto':'name',
      'categoria':'category','grupo':'category','familia':'category','unidade':'unit','und':'unit','un':'unit','medida':'unit',
      'custo':'cost_price','preco_custo':'cost_price','preco_de_custo':'cost_price','preco_compra':'cost_price','valor_custo':'cost_price',
      'preco':'sale_price','preco_venda':'sale_price','preco_de_venda':'sale_price','valor':'sale_price','valor_venda':'sale_price','valor_unitario':'sale_price',
      'quantidade':'stock','qtd':'stock','qtde':'stock','estoque':'stock','estoque_atual':'stock','saldo':'stock','saldo_atual':'stock',
      'estoque_minimo':'min_stock','minimo':'min_stock','qtd_minima':'min_stock','quantidade_minima':'min_stock',
    }
    def num(v,field):
        if v is None or str(v).strip()=='':return 0.0
        if isinstance(v,str):
            t=v.strip().replace('R$','').replace('r$','').replace(' ','').replace('\u00a0','')
            if ',' in t and '.' in t:
                t=t.replace('.','').replace(',','.') if t.rfind(',')>t.rfind('.') else t.replace(',','')
            elif ',' in t:t=t.replace(',','.')
            elif t.count('.')>1:t=t.replace('.','')
        else:t=str(v)
        try:d=Decimal(t)
        except (InvalidOperation,ValueError):raise ValueError(f'{field}: número inválido ({v})')
        if not d.is_finite() or d<0:raise ValueError(f'{field}: não pode ser negativo')
        if d>Decimal('1000000000'):raise ValueError(f'{field}: valor muito alto')
        return float(d)
    errors=[];prepared=[];found=False;sheet_name='';max_errors=60
    try:
        for ws in wb.worksheets:
            rows=ws.iter_rows(values_only=True)
            for header_line,header in enumerate(rows,start=1):
                if not any(v is not None and str(v).strip() for v in header):continue
                headers=[aliases.get(norm(v),norm(v)) for v in header]
                if 'sku' not in headers and 'barcode' in headers: headers[headers.index('barcode')]='sku'
                if not {'sku','name','sale_price','stock'}.issubset(headers):
                    # First non-empty line may be a report title; inspect up to 12 lines.
                    if header_line<12:continue
                    break
                found=True;sheet_name=ws.title
                dup=[x for x in headers if x and headers.count(x)>1 and x in ('sku','name','sale_price','stock')]
                if dup:raise HTTPException(422,{'mensagem':'Cabeçalhos duplicados: '+', '.join(sorted(set(dup))), 'erros':[]})
                idx={k:headers.index(k) for k in ['sku','barcode','name','category','unit','cost_price','sale_price','stock','min_stock'] if k in headers}
                seen=set()
                for line,row in enumerate(rows,start=header_line+1):
                    if not any(v is not None and str(v).strip() for v in row):continue
                    try:
                        d={k:row[i] if i<len(row) else None for k,i in idx.items()}
                        raw_sku=d.get('sku')
                        if isinstance(raw_sku,float) and raw_sku.is_integer():sku=str(int(raw_sku))
                        elif isinstance(raw_sku,int):sku=str(raw_sku)
                        else:sku=str(raw_sku or '').strip()
                        name=str(d.get('name') or '').strip()
                        if not sku or not name:raise ValueError('Código e nome são obrigatórios')
                        if len(sku)>80 or len(name)>180:raise ValueError('Código ou nome excede o limite permitido')
                        if sku.casefold() in seen:raise ValueError('Código duplicado na planilha: '+sku)
                        seen.add(sku.casefold())
                        category=str(d.get('category') or 'Geral').strip()[:100]
                        unit=str(d.get('unit') or 'un').strip()[:20]
                        prepared.append({'sku':sku,'barcode':str(d.get('barcode') or (sku if 'barcode' not in idx else '')).strip()[:80],'name':name,'category':category,'unit':unit,
                            'cost_price':num(d.get('cost_price'),'Custo'),
                            'sale_price':num(d.get('sale_price'),'Preço de venda'),
                            'stock':num(d.get('stock'),'Estoque'),
                            'min_stock':num(d.get('min_stock'),'Estoque mínimo')})
                    except Exception as ex:
                        errors.append({'linha':line,'erro':str(ex)})
                        if len(errors)>=max_errors:break
                break
            if found:break
    finally:wb.close()
    if not found:raise HTTPException(422,{'mensagem':'Não encontrei as colunas Código, Produto, Preço e Estoque. Confira os títulos ou baixe o modelo.','erros':[]})
    if not prepared and not errors:raise HTTPException(422,{'mensagem':'A planilha não contém produtos.','erros':[]})
    if errors:raise HTTPException(422,{'mensagem':'Importação cancelada: nenhuma alteração realizada.','total_erros':len(errors),'erros':errors})
    existing={p.sku.casefold():p for p in company_query(db,Product,company_id).all()}
    created=sum(x['sku'].casefold() not in existing for x in prepared)
    return {'rows':prepared,'existing':existing,'criados':created,'atualizados':len(prepared)-created,'aba':sheet_name}

@app.post('/api/products/import.xlsx')
async def import_products(file:UploadFile=File(...),preview:bool=False,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    if not (file.filename or '').lower().endswith(('.xlsx','.xlsm')):
        raise HTTPException(400,'Envie uma planilha .xlsx ou .xlsm.')
    raw=await file.read(10*1024*1024+1)
    if len(raw)>10*1024*1024:raise HTTPException(413,'Planilha maior que 10 MB.')
    parsed=_parse_import_xlsx(raw,db,ctx['company_id'])
    result={'criados':parsed['criados'],'atualizados':parsed['atualizados'],'total_erros':0,'erros':[],
            'aba':parsed['aba'],'total':len(parsed['rows']),'amostra':parsed['rows'][:8]}
    if preview:
        result['mensagem']='Conferência concluída. Nenhum produto foi alterado.'
        return result
    try:
        for vals in parsed['rows']:
            item=parsed['existing'].get(vals['sku'].casefold())
            if item:
                for k,v in vals.items():setattr(item,k,v)
                item.active=True
            else:db.add(Product(company_id=ctx['company_id'],**vals))
        audit(db,ctx,'produtos_importados','planilha','',{'criados':parsed['criados'],'atualizados':parsed['atualizados']})
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500,'Não foi possível salvar a planilha. Nenhuma alteração foi confirmada.')
    result['mensagem']='Importação concluída com sucesso.'
    return result

# Clientes e fornecedores ficam separados por empresa
for route, model, label in [('customers',Customer,'Cliente'),('suppliers',Supplier,'Fornecedor')]:
    def make_list(m):
      def endpoint(db:Session=Depends(get_db),ctx=Depends(employee_only)): return company_query(db,m,ctx['company_id']).order_by(m.name).all()
      return endpoint
    def make_create(m):
      def endpoint(data:ContactIn,db:Session=Depends(get_db),ctx=Depends(employee_only)):
        obj=m(company_id=ctx['company_id'],**data.model_dump());db.add(obj);db.commit();db.refresh(obj);return obj
      return endpoint
    app.add_api_route(f'/api/{route}',make_list(model),methods=['GET'],name=f'list_{route}')
    app.add_api_route(f'/api/{route}',make_create(model),methods=['POST'],name=f'create_{route}')

def revoke(db, kind, subject):
    import time as clock
    key = f'{kind}:{subject}'
    row = db.get(AuthVersion, key)
    if row: row.revoked_at = clock.time()
    else: db.add(AuthVersion(subject=key, revoked_at=clock.time()))

def sale_response(sale, request, db=None):
    result={'id':sale.id,'total':sale.total,'discount_total':sale.discount_total,'payment_method':sale.payment_method,'status':sale.status,
            'pix_verification':sale.pix_verification,'created_at':sale.created_at,'received_amount':request.received_amount,'change_amount':request.change_amount}
    if db is not None:
        result['payments']=[{'method':x.method,'amount':x.amount} for x in company_query(db,SalePayment,sale.company_id).filter(SalePayment.sale_id==sale.id).all()]
    return result

class PaymentTerminalIn(BaseModel):
    provider: str = Field(min_length=2,max_length=100)
    terminal_name: str = Field(default='',max_length=100)

@app.get('/api/payment-terminal/settings')
def payment_terminal_settings(db:Session=Depends(get_db),ctx=Depends(admin_only)):
    row=db.get(PaymentTerminalSettings,ctx['company_id'])
    return {'provider':row.provider,'terminal_name':row.terminal_name,'integration_mode':'manual','connected':False} if row else {'provider':'','terminal_name':'','integration_mode':'manual','connected':False}

@app.put('/api/payment-terminal/settings')
def save_payment_terminal(data:PaymentTerminalIn,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    provider=data.provider.strip();terminal_name=data.terminal_name.strip()
    if not provider: raise HTTPException(400,'Informe a marca/provedor da maquininha')
    row=db.get(PaymentTerminalSettings,ctx['company_id'])
    if not row: row=PaymentTerminalSettings(company_id=ctx['company_id'],provider=provider,terminal_name=terminal_name,integration_mode='manual');db.add(row)
    else: row.provider=provider;row.terminal_name=terminal_name;row.integration_mode='manual'
    audit(db,ctx,'maquininha_configurada','integracao',provider,{'maquininha':terminal_name,'modo':'manual'})
    db.commit();return {'ok':True,'integration_mode':'manual','connected':False}

@app.get('/api/audit')
def list_admin_audit(limit:int=100,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    rows=company_query(db,AdminAudit,ctx['company_id']).order_by(desc(AdminAudit.created_at)).limit(max(1,min(limit,500))).all()
    return [{'id':x.id,'employee_id':x.employee_id,'action':x.action,'entity':x.entity,'entity_id':x.entity_id,'details':json.loads(x.details or '{}'),'created_at':x.created_at} for x in rows]

@app.post('/api/sales')
def create_sale(data:SaleIn,db:Session=Depends(get_db),ctx=Depends(employee_only)):
    return _register_sale(data,db,ctx)

def _register_sale(data:SaleIn,db:Session,ctx,verified_pix_order:PixOrder|None=None):
    company_id=ctx['company_id']
    fingerprint=hashlib.sha256(data.model_dump_json(exclude={'request_id'}).encode()).hexdigest()
    existing=db.query(SaleRequest).filter_by(company_id=company_id,request_id=data.request_id).first()
    if existing:
        if existing.employee_id!=ctx['employee'].id or existing.fingerprint!=fingerprint:
            raise HTTPException(409,'Identificador já usado em outra venda')
        return sale_response(db.get(Sale,existing.sale_id),existing,db)
    if data.discount_percent>0 and ctx['role']!='admin': raise HTTPException(403,'Desconto permitido somente com autorização de administrador')
    request=SaleRequest(company_id=company_id,employee_id=ctx['employee'].id,request_id=data.request_id,fingerprint=fingerprint)
    try:
        db.add(request);db.flush()
        lines,gross,discount,total=calculate_sale(db,company_id,data.items,data.discount_percent)
        if money(data.expected_total)!=total: raise HTTPException(409,'Preços ou desconto alterados. Revise a venda.')
        payment_inputs=data.payments or [SalePaymentIn(method=data.payment_method,amount=float(total))]
        if len({p.method for p in payment_inputs})!=len(payment_inputs): raise HTTPException(400,'Cada forma de pagamento pode aparecer somente uma vez')
        if money(sum((money(p.amount) for p in payment_inputs),money(0)))!=total: raise HTTPException(400,'A soma das formas de pagamento deve ser igual ao total')
        cash_total=sum((money(p.amount) for p in payment_inputs if p.method=='Dinheiro'),money(0))
        uses_pix=any(p.method=='PIX' for p in payment_inputs)
        pix_total=sum((money(p.amount) for p in payment_inputs if p.method=='PIX'),money(0))
        if uses_pix:
            pix_config=db.get(PixSettings,company_id)
            if not pix_config: raise HTTPException(400,'Cadastre o PIX antes de vender')
            if verified_pix_order is not None:
                expected_cents=int(money(pix_total)*100)
                if (pix_config.provider!='mercado_pago' or verified_pix_order.company_id!=company_id or
                    verified_pix_order.employee_id!=ctx['employee'].id or verified_pix_order.request_id!=data.request_id or
                    not verified_pix_order.provider_order_id or verified_pix_order.status!='credito_confirmado' or
                    verified_pix_order.amount_cents!=expected_cents):
                    raise HTTPException(400,'A confirmação automática do PIX não corresponde ao valor desta venda')
            else:
                if pix_config.provider=='mercado_pago' and pix_config.mp_access_token_cipher: raise HTTPException(400,'No Mercado Pago com API configurada, finalize somente após a confirmação automática do pedido')
                if not data.pix_confirmed: raise HTTPException(400,'Confira o recebimento no banco e confirme manualmente o PIX')
        received=money(data.received_amount or 0) if cash_total else money(0)
        if cash_total and received<cash_total: raise HTTPException(400,'Valor recebido em dinheiro menor que a parte em dinheiro')
        if not cash_total and data.received_amount not in (None,0): raise HTTPException(400,'Valor recebido só pode ser informado quando houver pagamento em dinheiro')
        for line in lines:
            p=line['product'];qty=line['quantity']
            result=db.execute(update(Product).where(Product.id==p.id,Product.company_id==company_id,Product.stock>=qty).values(stock=Product.stock-qty),execution_options={'synchronize_session':False})
            if result.rowcount!=1: raise HTTPException(409,f'Estoque insuficiente para {p.name}')
        methods=' + '.join(p.method for p in payment_inputs)
        sale=Sale(company_id=company_id,employee_id=ctx['employee'].id,total=float(total),discount_total=float(discount),payment_method=methods,
                  pix_verification=('verificado_mercado_pago' if verified_pix_order is not None else 'conferencia_manual') if uses_pix else 'nao_aplicavel')
        db.add(sale);db.flush()
        for line in lines:
            p=line['product'];db.add(SaleItem(company_id=company_id,sale_id=sale.id,product_id=p.id,product_name=p.name,quantity=line['quantity'],unit_price=p.sale_price,subtotal=float(line['subtotal']),discount_amount=float(line['discount'])))
        for payment in payment_inputs: db.add(SalePayment(company_id=company_id,sale_id=sale.id,method=payment.method,amount=float(money(payment.amount))))
        request.sale_id=sale.id;request.received_amount=float(received);request.change_amount=float(received-cash_total)
        if uses_pix:
            pix_config=db.get(PixSettings,company_id)
            if verified_pix_order is not None:
                audit(db,ctx,'pix_confirmado_mercado_pago','venda',sale.id,{'valor_pix':float(pix_total),'instituicao':'mercado_pago','verificacao':'api_orders','pedido':verified_pix_order.provider_order_id})
            else:
                audit(db,ctx,'pix_confirmado_manual','venda',sale.id,{'valor_pix':float(pix_total),'instituicao':pix_config.provider if pix_config else 'manual','verificacao':'conferencia_manual'})
        if discount>0: audit(db,ctx,'desconto_aplicado','venda',sale.id,{'percentual':data.discount_percent,'valor':float(discount),'total_bruto':float(gross),'total_liquido':float(total)})
        db.commit();return sale_response(sale,request,db)
    except IntegrityError:
        db.rollback();raise HTTPException(409,'Venda em processamento. Tente novamente com o mesmo identificador.')
    except Exception:
        db.rollback();raise

def money_quantity(qty):
    from decimal import Decimal
    return Decimal(str(qty))

class PixIn(BaseModel):
    key: str = Field(default='',max_length=100)
    merchant_name: str = Field(default='',max_length=25)
    merchant_city: str = Field(default='',max_length=15)
    provider: str = Field(default='manual',max_length=40)
    access_token: str = Field(default='',max_length=500)
    clear_access_token: bool = False

class PixOrderIn(BaseModel):
    sale: SaleIn
    payer_email: str = Field(min_length=6,max_length=254,pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

PIX_PROVIDER_LABELS={'manual':'Chave PIX (sem integração)','mercado_pago':'Mercado Pago','nubank':'Nubank','inter':'Banco Inter','c6':'Banco C6','itau':'Itaú','bradesco':'Bradesco','outro':'Outro'}
PIX_OPEN_ORDER_STATES=('criando','aguardando_pagamento','processando','credito_confirmado','pago_sem_venda','inconsistente')

def pix_settings_view(row):
    provider=(row.provider or 'manual') if row else 'manual'
    has_token=bool(row and row.mp_access_token_cipher)
    return {'key':row.key if row else '',
            'merchant_name':row.merchant_name if row else '',
            'merchant_city':row.merchant_city if row else '',
            'provider':provider,'provider_label':PIX_PROVIDER_LABELS.get(provider,'Outro'),
            'has_access_token':has_token,'automatic_ready':provider=='mercado_pago' and has_token,
            'verification_mode':'mercado_pago' if provider=='mercado_pago' and has_token else 'manual',
            'connected':False}

@app.get('/api/pix/settings')
def pix_settings(db:Session=Depends(get_db),ctx=Depends(admin_only)):
    row=db.get(PixSettings,ctx['company_id'])
    return pix_settings_view(row)

@app.put('/api/pix/settings')
def save_pix(data:PixIn,db:Session=Depends(get_db),ctx=Depends(admin_only)):
    provider=data.provider.strip().lower() or 'manual'
    if provider not in PIX_PROVIDER_LABELS: raise HTTPException(400,'Escolha um banco/provedor da lista')
    row=db.get(PixSettings,ctx['company_id'])
    try:
        key=data.key.strip() or (row.key if row else '')
        if key: key=validate_key(key)
        elif provider!='mercado_pago': raise ValueError('Informe a chave PIX da empresa')
        name=merchant(data.merchant_name.strip() or (row.merchant_name if row else '') or 'NYC GESTAO',25)
        city=merchant(data.merchant_city.strip() or (row.merchant_city if row else '') or 'SAO PAULO',15)
    except ValueError as e: raise HTTPException(400,str(e))
    old_cipher=row.mp_access_token_cipher if row else None
    new_cipher=old_cipher
    if data.clear_access_token:
        new_cipher=None
    elif data.access_token.strip():
        candidate=data.access_token.strip()
        try: old_token=decrypt_private(old_cipher) if old_cipher else None
        except RuntimeError: old_token=None
        new_cipher=old_cipher if candidate==old_token else encrypt_private(candidate)
    if provider!='mercado_pago': new_cipher=None
    open_orders=company_query(db,PixOrder,ctx['company_id']).filter(PixOrder.status.in_(PIX_OPEN_ORDER_STATES))
    pending=open_orders.count()
    known_provider_orders=open_orders.filter(PixOrder.provider_order_id.is_not(None)).count()
    old_provider=row.provider if row else 'manual'
    if pending and (provider!='mercado_pago' or old_provider!='mercado_pago' or (known_provider_orders and new_cipher!=old_cipher)):
        raise HTTPException(409,'Há PIXs pendentes ou em conferência nesta empresa. Aguarde resolver esses pedidos antes de trocar ou apagar o Access Token.')
    values={'key':key,'merchant_name':name,'merchant_city':city,'provider':provider,'mp_access_token_cipher':new_cipher}
    if not row: row=PixSettings(company_id=ctx['company_id'],**values);db.add(row)
    else:
        for key,value in values.items(): setattr(row,key,value)
    audit(db,ctx,'pix_configurado','integracao','pix',{'chave_atualizada':bool(data.key.strip()),'instituicao':provider,'access_token_atualizado':bool(data.access_token.strip()) or data.clear_access_token,'modo_verificacao':'mercado_pago' if provider=='mercado_pago' and new_cipher else 'manual'})
    db.commit();return {'ok':True,**pix_settings_view(row)}

@app.post('/api/pix/preview')
def pix_preview(data:SaleIn,db:Session=Depends(get_db),ctx=Depends(employee_only)):
    config=db.get(PixSettings,ctx['company_id'])
    if not config: raise HTTPException(400,'Administrador: cadastre o PIX nas Configurações')
    if data.discount_percent>0 and ctx['role']!='admin': raise HTTPException(403,'Desconto permitido somente com autorização de administrador')
    lines,gross,discount,total=calculate_sale(db,ctx['company_id'],data.items,data.discount_percent)
    if total!=money(data.expected_total): raise HTTPException(409,'Preços ou desconto alterados. Revise a venda.')
    pix_amount=total
    if data.payments:
        if money(sum((money(x.amount) for x in data.payments),money(0)))!=total: raise HTTPException(400,'A soma das formas de pagamento deve ser igual ao total')
        pix_amount=sum((money(x.amount) for x in data.payments if x.method=='PIX'),money(0))
        if pix_amount<=0: raise HTTPException(400,'A venda não possui uma parcela PIX')
    elif data.payment_method!='PIX': raise HTTPException(400,'A venda não possui uma forma de pagamento PIX')
    if config.provider=='mercado_pago' and config.mp_access_token_cipher:
        return {'automatic':True,'payload':'','qr_svg':'','total':float(pix_amount),'sale_total':float(total),
                'payment_status':'aguardando_criacao','verification_mode':'mercado_pago','provider':'mercado_pago',
                'provider_label':PIX_PROVIDER_LABELS['mercado_pago'],'connected':False}
    try: payload=brcode(config.key,config.merchant_name,config.merchant_city,pix_amount)
    except ValueError as e: raise HTTPException(400,str(e))
    import qrcode, qrcode.image.svg
    image=qrcode.make(payload,image_factory=qrcode.image.svg.SvgPathImage)
    buffer=io.BytesIO();image.save(buffer)
    return {'payload':payload,'qr_svg':buffer.getvalue().decode(),'total':float(pix_amount),'sale_total':float(total),
            'payment_status':'aguardando_conferencia_manual','verification_mode':'manual','provider':config.provider or 'manual',
            'provider_label':PIX_PROVIDER_LABELS.get(config.provider or 'manual','Outro'),'connected':False,'automatic':False}

def _mp_order_view(row, sale=None, message=''):
    qr_data=row.qr_code_base64 or ''
    mime='image/png' if qr_data.startswith('iVBOR') else 'image/jpeg'
    labels={'criando':'Preparando cobrança PIX','aguardando_pagamento':'Aguardando pagamento PIX',
            'processando':'Mercado Pago está processando','credito_confirmado':'Pagamento confirmado; registrando venda',
            'pago_confirmado':'Pagamento confirmado pelo Mercado Pago','cancelado':'Cobrança cancelada ou expirada',
            'falha':'Cobrança não aprovada','inconsistente':'Não foi possível validar valor/status; venda bloqueada',
            'pago_sem_venda':'PIX creditado, mas a venda requer ajuste no sistema'}
    result={'id':row.id,'status':row.status,'status_label':labels.get(row.status,'Em processamento'),
            'provider_status':row.provider_status,'provider_status_detail':row.provider_status_detail,
            'amount':row.amount_cents/100,'qr_code':row.qr_code,'qr_image_data_url':('data:'+mime+';base64,'+qr_data) if qr_data else '',
            'ticket_url':row.ticket_url,'sale_id':row.sale_id,'message':message}
    if sale is not None: result['sale']=sale
    return result

def _mp_access_token(db,company_id):
    config=db.get(PixSettings,company_id)
    if not config or config.provider!='mercado_pago' or not config.mp_access_token_cipher:
        raise HTTPException(400,'O Mercado Pago não está configurado para esta empresa')
    try: return decrypt_private(config.mp_access_token_cipher)
    except RuntimeError as e: raise HTTPException(500,'Não foi possível abrir o Access Token salvo. Cadastre novamente a credencial em Configurações do PIX.') from e

def _mp_http_error(exc):
    if exc.status_code==401: detail='Mercado Pago recusou o Access Token. Revise a credencial salva nas configurações do PIX.'
    elif exc.status_code in (400,422): detail='Mercado Pago recusou os dados da cobrança. Confira o e-mail do pagador e as credenciais da aplicação.'
    elif exc.status_code==429: detail='Limite temporário de consultas do Mercado Pago. Aguarde alguns segundos e tente novamente.'
    else: detail='Não foi possível comunicar com o Mercado Pago. O pedido ficou salvo para uma nova consulta segura.'
    return HTTPException(502,detail)

def _call_mp_create(row,db):
    try:
        saved=json.loads(decrypt_private(row.request_cipher))
        token=_mp_access_token(db,row.company_id)
        response=mp_create_order(token,row.idempotency_key,saved['provider_payload'])
    except MercadoPagoError as e: raise _mp_http_error(e)
    except (RuntimeError,KeyError,TypeError,json.JSONDecodeError) as e:
        raise HTTPException(500,'Não foi possível recuperar os dados protegidos deste pedido PIX') from e
    order_id=str(response.get('id') or '')
    if not order_id:
        row.status='criando';db.commit()
        raise HTTPException(502,'Mercado Pago não devolveu o identificador do pedido. Tente consultar novamente; o mesmo código de idempotência será usado.')
    row.provider_order_id=order_id
    row.provider_status=str(response.get('status') or '')[:40]
    row.provider_status_detail=str(response.get('status_detail') or '')[:60]
    qr=mp_pix_qr(response)
    row.qr_code=qr['qr_code'][:10000]
    row.qr_code_base64=qr['qr_code_base64'][:1_500_000]
    row.ticket_url=qr['ticket_url'][:2000]
    row.status=normalized_order_status(response)
    if row.status=='encerrado': row.status='falha'
    row.last_checked_at=datetime.utcnow()
    db.commit();db.refresh(row)
    return row

@app.post('/api/pix/orders')
def create_mp_pix_order(data:PixOrderIn,db:Session=Depends(get_db),ctx=Depends(employee_only)):
    sale_data=data.sale
    company_id=ctx['company_id']
    if sale_data.discount_percent>0 and ctx['role']!='admin': raise HTTPException(403,'Desconto permitido somente com autorização de administrador')
    lines,gross,discount,total=calculate_sale(db,company_id,sale_data.items,sale_data.discount_percent)
    if money(sale_data.expected_total)!=total: raise HTTPException(409,'Preços ou desconto alterados. Revise a venda.')
    payment_inputs=sale_data.payments or [SalePaymentIn(method=sale_data.payment_method,amount=float(total))]
    if len({p.method for p in payment_inputs})!=len(payment_inputs): raise HTTPException(400,'Cada forma de pagamento pode aparecer somente uma vez')
    if money(sum((money(p.amount) for p in payment_inputs),money(0)))!=total: raise HTTPException(400,'A soma das formas de pagamento deve ser igual ao total')
    cash_total=sum((money(p.amount) for p in payment_inputs if p.method=='Dinheiro'),money(0))
    pix_total=sum((money(p.amount) for p in payment_inputs if p.method=='PIX'),money(0))
    if pix_total<=0: raise HTTPException(400,'A venda não possui uma parcela PIX')
    received=money(sale_data.received_amount or 0) if cash_total else money(0)
    if cash_total and received<cash_total: raise HTTPException(400,'Valor recebido em dinheiro menor que a parte em dinheiro')
    if not cash_total and sale_data.received_amount not in (None,0): raise HTTPException(400,'Valor recebido só pode ser informado quando houver pagamento em dinheiro')
    config=db.get(PixSettings,company_id)
    if not config or config.provider!='mercado_pago' or not config.mp_access_token_cipher:
        raise HTTPException(400,'O administrador precisa configurar o Mercado Pago nesta empresa antes de gerar cobranças PIX')
    amount_cents=int(money(pix_total)*100)
    amount_text=f'{amount_cents//100}.{amount_cents%100:02d}'
    external_reference='nyc-'+str(company_id)+'-'+hashlib.sha256(sale_data.request_id.encode()).hexdigest()[:32]
    provider_payload={'type':'online','external_reference':external_reference,'transactions':{'payments':[{'amount':amount_text,'payment_method':{'id':'pix','type':'bank_transfer'},'expiration_time':'P1D'}]},'payer':{'email':data.payer_email.strip().lower()},'total_amount':amount_text,'processing_mode':'automatic'}
    saved_request={'sale':sale_data.model_dump(mode='json'),'payer_email':data.payer_email.strip().lower(),'provider_payload':provider_payload}
    fingerprint=hashlib.sha256(json.dumps(saved_request,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    existing=company_query(db,PixOrder,company_id).filter_by(request_id=sale_data.request_id).first()
    if existing:
        if existing.employee_id!=ctx['employee'].id or existing.draft_fingerprint!=fingerprint: raise HTTPException(409,'Este identificador já está ligado a outra cobrança PIX')
        row=existing
    else:
        if company_query(db,SaleRequest,company_id).filter_by(request_id=sale_data.request_id).first(): raise HTTPException(409,'Esta venda já foi registrada com esse identificador')
        row=PixOrder(company_id=company_id,employee_id=ctx['employee'].id,employee_role=ctx['role'],request_id=sale_data.request_id,
                     draft_fingerprint=fingerprint,idempotency_key=str(uuid.uuid4()),external_reference=external_reference,
                     amount_cents=amount_cents,request_cipher=encrypt_private(json.dumps(saved_request,ensure_ascii=False,separators=(',',':'))),status='criando')
        db.add(row)
        try: db.commit();db.refresh(row)
        except IntegrityError:
            db.rollback()
            existing=company_query(db,PixOrder,company_id).filter_by(request_id=sale_data.request_id).first()
            if not existing or existing.employee_id!=ctx['employee'].id or existing.draft_fingerprint!=fingerprint: raise HTTPException(409,'Cobrança PIX em processamento. Repita a mesma tentativa.')
            row=existing
    if row.provider_order_id: return _mp_order_view(row)
    row=_call_mp_create(row,db)
    audit(db,ctx,'cobranca_pix_mercado_pago_criada','pix_order',row.id,{'valor':amount_cents/100,'pedido':row.provider_order_id,'status':row.provider_status})
    db.commit();return _mp_order_view(row)

@app.get('/api/pix/orders/{order_id}')
def check_mp_pix_order(order_id:int,db:Session=Depends(get_db),ctx=Depends(employee_only)):
    row=company_query(db,PixOrder,ctx['company_id']).filter(PixOrder.id==order_id).first()
    if not row: raise HTTPException(404,'Cobrança PIX não encontrada nesta empresa')
    if row.employee_id!=ctx['employee'].id and ctx['role']!='admin': raise HTTPException(403,'Somente o operador que iniciou ou o administrador pode consultar esta cobrança')
    if row.status in ('pago_confirmado','cancelado','falha','inconsistente'):
        sale=None
        if row.sale_id:
            sale_row=db.get(Sale,row.sale_id);request=company_query(db,SaleRequest,row.company_id).filter_by(request_id=row.request_id).first()
            if sale_row and request: sale=sale_response(sale_row,request,db)
        message='O valor ou status devolvido não passou na validação. Nenhuma venda foi registrada; peça a revisão do administrador.' if row.status=='inconsistente' else ''
        return _mp_order_view(row,sale,message)
    try:
        if not row.provider_order_id:
            row=_call_mp_create(row,db)
            if not row.provider_order_id: return _mp_order_view(row)
        token=_mp_access_token(db,row.company_id)
        response=mp_get_order(token,row.provider_order_id)
    except MercadoPagoError as e: raise _mp_http_error(e)
    row=db.get(PixOrder,order_id)
    if str(response.get('id') or '')!=row.provider_order_id or str(response.get('external_reference') or '')!=row.external_reference:
        row.status='inconsistente';row.provider_status=str(response.get('status') or '')[:40];row.provider_status_detail=str(response.get('status_detail') or '')[:60]
        audit(db,ctx,'pix_mercado_pago_inconsistente','pix_order',row.id,{'motivo':'identificador_ou_referencia_divergente','pedido':row.provider_order_id})
        db.commit();return _mp_order_view(row,None,'O identificador retornado pelo provedor não corresponde ao pedido salvo. Nenhuma venda foi registrada.')
    row.provider_status=str(response.get('status') or '')[:40]
    row.provider_status_detail=str(response.get('status_detail') or '')[:60]
    qr=mp_pix_qr(response)
    if qr['qr_code']: row.qr_code=qr['qr_code'][:10000]
    if qr['qr_code_base64']: row.qr_code_base64=qr['qr_code_base64'][:1_500_000]
    if qr['ticket_url']: row.ticket_url=qr['ticket_url'][:2000]
    row.last_checked_at=datetime.utcnow()
    if credited_pix_order(response,row.amount_cents):
        row.status='credito_confirmado';db.commit()
        try:
            saved=json.loads(decrypt_private(row.request_cipher))
            sale_data=SaleIn.model_validate(saved['sale']).model_copy(update={'pix_confirmed':True})
            employee=db.get(Employee,row.employee_id)
            if not employee or not employee.active or employee.company_id!=row.company_id:
                raise HTTPException(409,'O funcionário que iniciou o pedido está inativo. Um administrador deve revisar a venda.')
            sale_result=_register_sale(sale_data,db,{'employee':employee,'company_id':row.company_id,'role':row.employee_role},verified_pix_order=row)
            row=db.get(PixOrder,order_id);row.status='pago_confirmado';row.sale_id=sale_result['id'];db.commit()
            return _mp_order_view(row,sale_result,'Pagamento confirmado pela API do Mercado Pago. Venda registrada.')
        except (HTTPException,RuntimeError,KeyError,TypeError,json.JSONDecodeError) as e:
            row=db.get(PixOrder,order_id);row.status='pago_sem_venda';db.commit()
            reason=e.detail if isinstance(e,HTTPException) else 'Falha ao recuperar os dados protegidos da venda.'
            return _mp_order_view(row,None,'O Mercado Pago confirmou o crédito, mas o sistema não concluiu a venda: '+str(reason))
    normalized=normalized_order_status(response)
    if normalized=='encerrado': row.status='cancelado' if row.provider_status=='canceled' else 'falha'
    elif normalized=='inconsistente': row.status='inconsistente'
    elif normalized=='credito_confirmado': row.status='inconsistente'
    else: row.status=normalized
    if row.status=='inconsistente': audit(db,ctx,'pix_mercado_pago_inconsistente','pix_order',row.id,{'motivo':'valor_ou_status_divergente','status':row.provider_status,'detalhe':row.provider_status_detail,'valor_esperado_centavos':row.amount_cents})
    db.commit()
    if row.status=='inconsistente': return _mp_order_view(row,None,'O Mercado Pago não confirmou exatamente o meio e o valor esperado. Nenhuma venda foi registrada; peça a revisão do administrador.')
    return _mp_order_view(row)

@app.get('/api/sales')
def list_sales(limit:int=100,db:Session=Depends(get_db),ctx=Depends(employee_only)):
    sales=company_query(db,Sale,ctx['company_id']).order_by(desc(Sale.created_at)).limit(max(1,min(limit,500))).all()
    return [{'id':s.id,'total':s.total,'discount_total':s.discount_total,'payment_method':s.payment_method,'pix_verification':s.pix_verification,'payments':[{'method':p.method,'amount':p.amount} for p in company_query(db,SalePayment,ctx['company_id']).filter(SalePayment.sale_id==s.id).all()],'status':s.status,'created_at':s.created_at,'items':[{'product_id':i.product_id,'product_name':i.product_name,'quantity':i.quantity,'unit_price':i.unit_price,'subtotal':i.subtotal,'discount_amount':i.discount_amount} for i in company_query(db,SaleItem,ctx['company_id']).filter(SaleItem.sale_id==s.id).all()]} for s in sales]
@app.get('/api/cash/current')
def current_cash(db:Session=Depends(get_db),ctx=Depends(employee_only)):
    return company_query(db,CashSession,ctx['company_id']).filter(CashSession.status=='aberto').order_by(desc(CashSession.opened_at)).first()
@app.post('/api/cash/open')
def open_cash(data:CashAmount,db:Session=Depends(get_db),ctx=Depends(employee_only)):
    if company_query(db,CashSession,ctx['company_id']).filter(CashSession.status=='aberto').first():raise HTTPException(409,'Já existe um caixa aberto nesta empresa')
    row=CashSession(company_id=ctx['company_id'],employee_id=ctx['employee'].id,opening_amount=data.amount);db.add(row);db.commit();db.refresh(row);return row
@app.post('/api/cash/close')
def close_cash(data:CashAmount,db:Session=Depends(get_db),ctx=Depends(employee_only)):
    row=company_query(db,CashSession,ctx['company_id']).filter(CashSession.status=='aberto').first()
    if not row:raise HTTPException(404,'Não existe caixa aberto')
    row.closing_amount=data.amount;row.status='fechado';row.closed_at=datetime.utcnow();db.commit();db.refresh(row);return row
@app.get('/api/cash/history')
def cash_history(db:Session=Depends(get_db),ctx=Depends(employee_only)):
    return company_query(db,CashSession,ctx['company_id']).order_by(desc(CashSession.opened_at)).limit(50).all()

@app.get('/api/reports/summary')
def summary(db:Session=Depends(get_db),ctx=Depends(employee_only)):
    today=date.today();start=datetime.combine(today,time.min);end=datetime.combine(today,time.max)
    q=company_query(db,Sale,ctx['company_id']).filter(Sale.created_at>=start,Sale.created_at<=end,Sale.status=='concluida')
    count,total=q.with_entities(func.count(Sale.id),func.coalesce(func.sum(Sale.total),0)).first()
    products=company_query(db,Product,ctx['company_id']).filter(Product.active==True)
    low=products.filter(Product.stock<=Product.min_stock).count();inventory=products.with_entities(func.coalesce(func.sum(Product.stock*Product.cost_price),0)).scalar()
    return {'sales_today_count':count or 0,'sales_today_total':float(total or 0),'low_stock_count':low,'inventory_cost_value':float(inventory or 0),'products_count':products.count(),'recent_sales':[{'id':s.id,'total':s.total,'created_at':s.created_at} for s in q.order_by(desc(Sale.created_at)).limit(5).all()]}

@app.get('/api/reports/low-stock')
def low_stock(db:Session=Depends(get_db),ctx=Depends(employee_only)):
    return company_query(db,Product,ctx['company_id']).filter(Product.active==True,Product.stock<=Product.min_stock).order_by(Product.stock).all()

WEB_DIR=Path(__file__).resolve().parents[2]/'web'
app.mount('/',StaticFiles(directory=WEB_DIR,html=True),name='web-assets')
