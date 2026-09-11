"""Small SQLite store. No credentials are returned in device/user lists."""
import contextlib
import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path
from cryptography.fernet import Fernet

DATA = Path(os.getenv('FERMDE_DATA', '/var/lib/fermde'))

def connect():
    db = sqlite3.connect(DATA / 'panel.db', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db

@contextlib.contextmanager
def transaction():
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()

def rows(sql, args=()):
    with contextlib.closing(connect()) as db:
        return [dict(r) for r in db.execute(sql, args)]

def one(sql, args=()):
    result = rows(sql, args)
    return result[0] if result else None

def execute(sql, args=()):
    with transaction() as db:
        return db.execute(sql, args).lastrowid

def init():
    DATA.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(connect()) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
          password TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
          quota INTEGER NOT NULL DEFAULT 4, running_quota INTEGER NOT NULL DEFAULT 2);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires REAL);
        CREATE TABLE IF NOT EXISTS devices(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          owner INTEGER NOT NULL REFERENCES users(id), profile TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'creating',
          error TEXT NOT NULL DEFAULT '', proxy TEXT NOT NULL DEFAULT '', session TEXT NOT NULL,
          ip TEXT NOT NULL DEFAULT '', created REAL NOT NULL, wanted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at REAL, actor INTEGER, action TEXT, device INTEGER, detail TEXT);
        CREATE TABLE IF NOT EXISTS attempts(address TEXT PRIMARY KEY, count INTEGER, since REAL);
        ''')
        db.commit()
    for key, value in {'max_running':'4', 'reserve_mib':'8192', 'device_budget_mib':'1536',
                       'headroom_mib':'1024', 'country':'RU', 'restore_after_reboot':'false'}.items():
        execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, value))

def setting(key, default=''):
    r = one('SELECT value FROM settings WHERE key=?', (key,))
    return r['value'] if r else default

def set_setting(key, value):
    execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

def cipher():
    return Fernet((Path('/etc/fermde/secret.key')).read_bytes().strip())

def encrypt(value):
    return cipher().encrypt(value.encode()).decode()

def decrypt(value):
    return cipher().decrypt(value.encode()).decode()

def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + digest

def password_ok(password, encoded):
    try:
        salt, digest = encoded.split(':')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return secrets.compare_digest(digest, actual)
    except (ValueError, TypeError):
        return False

def audit(actor, action, device=None, detail=''):
    execute('INSERT INTO audit(at,actor,action,device,detail) VALUES (?,?,?,?,?)',
            (time.time(), actor, action, device, detail[:500]))
