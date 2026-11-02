"""Static PIX BR Code. Generating this payload does not confirm payment."""
import re, unicodedata
from decimal import Decimal, ROUND_HALF_UP

def money(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

def merchant(value, limit):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().upper()
    value = re.sub(r'[^A-Z0-9 .-]', '', value).strip()
    if not value or len(value) > limit: raise ValueError('Nome/cidade PIX inválidos')
    return value

def validate_key(key):
    key = key.strip()
    valid = (re.fullmatch(r'\d{11}|\d{14}', key) or
             re.fullmatch(r'\+[1-9]\d{7,14}', key) or
             re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', key) or
             re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', key))
    if not valid or len(key.encode()) > 77: raise ValueError('Chave PIX inválida. Use CPF/CNPJ sem pontuação, telefone +55..., e-mail ou chave aleatória.')
    return key

def tlv(tag, value):
    size = len(value.encode('utf-8'))
    if size > 99: raise ValueError('Campo PIX muito longo')
    return tag + f'{size:02d}' + value

def brcode(key, name, city, amount):
    amount = money(amount)
    if not amount.is_finite() or amount <= 0 or amount > Decimal('9999999999.99'): raise ValueError('Valor PIX inválido')
    value = (tlv('00','01') + tlv('26',tlv('00','br.gov.bcb.pix')+tlv('01',validate_key(key))) +
             tlv('52','0000') + tlv('53','986') + tlv('54',format(amount,'.2f')) + tlv('58','BR') +
             tlv('59',merchant(name,25)) + tlv('60',merchant(city,15)) + tlv('62',tlv('05','***')) + '6304')
    crc = 0xFFFF
    for byte in value.encode('utf-8'):
        crc ^= byte << 8
        for _ in range(8): crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return value + f'{crc:04X}'
