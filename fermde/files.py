"""Private file exchange storage; user-visible names never become disk paths."""
import json
import re
import secrets
import shutil
from pathlib import Path
from fermde import store as db

MAX_FILE = 512 * 1024 * 1024
MAX_STORAGE = 5 * 1024 * 1024 * 1024


def folder(uid):
    path=db.DATA/'files'/str(int(uid))
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    return path


def entries(uid):
    result=[]
    for path in folder(uid).glob('*/meta.json'):
        if re.fullmatch('[a-f0-9]{32}',path.parent.name):
            result.append(json.loads(path.read_text(encoding='utf-8')))
    return sorted(result,key=lambda item:item['created'],reverse=True)


def entry(uid,key):
    if not re.fullmatch('[a-f0-9]{32}',key): raise FileNotFoundError
    path=folder(uid)/key
    return path,json.loads((path/'meta.json').read_text(encoding='utf-8'))


def filename(name):
    value=(str(name or 'file').replace('\\','/').split('/')[-1]
           .replace('\r','').replace('\n','').replace('\x00',''))[:180]
    return value if value not in ('','.', '..') else 'file'


def publish(uid,temp,name):
    import time
    size=temp.stat().st_size
    if size>MAX_FILE: raise ValueError('Максимальный размер файла — 512 МБ')
    if sum(x['size'] for x in entries(uid))+size>MAX_STORAGE:
        raise ValueError('Папка заполнена: лимит 5 ГБ')
    key=secrets.token_hex(16)
    staging=folder(uid)/('.'+key)
    staging.mkdir(mode=0o700)
    try:
        meta=dict(id=key,name=filename(name),size=size,created=time.time())
        shutil.move(str(temp),staging/'data')
        (staging/'meta.json').write_text(json.dumps(meta,ensure_ascii=False),encoding='utf-8')
        staging.rename(folder(uid)/key)
        return meta
    except BaseException:
        shutil.rmtree(staging,ignore_errors=True)
        raise
