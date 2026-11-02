import os
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from pathlib import Path
import secrets
import hashlib
import time
# Persist a private key locally when the distributed example key is still configured.
_key_file = Path(__file__).resolve().parents[1] / '.session-key'
SECRET_KEY = os.getenv('SECRET_KEY', '')
_example = Path(__file__).resolve().parents[1] / '.env.example'
_example_key = next((line.split('=',1)[1] for line in _example.read_text().splitlines() if line.startswith('SECRET_KEY=')), '') if _example.exists() else ''
if hashlib.sha256(SECRET_KEY.encode()).hexdigest() == 'ff43cf6b3588dd066a4250f6e935aa1eac800a209fd90ca49925ee33101add12' or not SECRET_KEY or SECRET_KEY == _example_key or len(SECRET_KEY) < 32 or SECRET_KEY.startswith(('CHANGE-', 'TROQUE', 'troque')):
    try:
        with _key_file.open('x', encoding='ascii') as f:
            f.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    SECRET_KEY = _key_file.read_text(encoding='ascii').strip()

ALGORITHM = 'HS256'
TOKEN_MINUTES = int(os.getenv('ACCESS_TOKEN_MINUTES', '720'))
pwd_context = CryptContext(schemes=['pbkdf2_sha256', 'bcrypt'], deprecated='auto')
def hash_password(password: str) -> str: return pwd_context.hash(password)
def verify_password(password: str, hashed: str) -> bool:
    try: return pwd_context.verify(password, hashed)
    except Exception: return False
def create_token(subject: str, role: str, company_id: int | None = None, kind: str = 'employee') -> str:
    payload = {'sub': str(subject), 'role': role, 'kind': kind, 'iat': time.time(), 'exp': datetime.now(timezone.utc)+timedelta(minutes=TOKEN_MINUTES)}
    if company_id is not None: payload['company_id'] = company_id
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
def decode_token(token: str):
    try: return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError: return None
