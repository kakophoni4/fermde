"""Local administrative recovery. Run as the fermde service user."""
import argparse
import getpass
import json
import os
import sqlite3
import time
from pathlib import Path
from fermde import store as db

def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['bootstrap','reset-password','backup'])
    p.add_argument('--username',default='admin')
    a=p.parse_args(); db.init()
    if a.action=='backup':
        folder=db.DATA/'backups'; folder.mkdir(mode=0o700,exist_ok=True)
        target=folder/('panel-'+time.strftime('%Y%m%d-%H%M%S')+'.db')
        with db.connect() as src, sqlite3.connect(target) as dst: src.backup(dst)
        target.chmod(0o600); print(target); return
    u=db.one('SELECT * FROM users WHERE username=?',(a.username,))
    if a.action=='bootstrap' and u:
        print('Account already exists; use reset-password if needed.'); return
    if a.action=='reset-password' and not u: raise SystemExit('Account not found')
    password=getpass.getpass('New password (12+ characters): ')
    if not 12<=len(password)<=200 or password!=getpass.getpass('Repeat password: '):
        raise SystemExit('Password mismatch or invalid length')
    if u:
        db.execute('UPDATE users SET password=?,active=1 WHERE id=?',(db.password_hash(password),u['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
    else:
        db.execute('INSERT INTO users(username,password,admin,quota,running_quota) VALUES (?,?,1,50,4)',
                   (a.username,db.password_hash(password)))
    print('Account ready.')

if __name__=='__main__': main()
