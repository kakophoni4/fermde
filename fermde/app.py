import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import FastAPI, Request, HTTPException, WebSocket, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fermde import store as db
from fermde.profiles import PROFILES

ORIGIN = os.environ.get('FERMDE_ORIGIN','https://localhost').rstrip('/')
STATIC = Path(__file__).resolve().parent.parent/'static'
ADB = '/opt/fermde-sdk/platform-tools/adb'
os.environ['ADB_SERVER_SOCKET']='tcp:127.0.0.1:5039'
operation_lock = asyncio.Lock()
jobs = set()

def fail(message, code=400): raise HTTPException(code,message)

async def agent(action, **kwargs):
    proc = await asyncio.create_subprocess_exec('/usr/bin/sudo','-n','/opt/fermde/venv/bin/python',
        '/opt/fermde/agent.py',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps(dict(action=action,**kwargs)).encode()),240)
    except asyncio.TimeoutError:
        # Do not blindly retry a timed-out mutation: the root child may be completing it.
        raise RuntimeError('Операция выполняется дольше ожидаемого. Проверьте журнал перед повтором.')
    try: result=json.loads(out)
    except ValueError: raise RuntimeError('Нет ответа системного агента')
    if not result.get('ok'): raise RuntimeError(result.get('error','Ошибка системного агента'))
    return result['result']

async def adb(serial,*args, timeout=30):
    p=await asyncio.create_subprocess_exec(ADB,'-s',serial,*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try: out,err=await asyncio.wait_for(p.communicate(),timeout)
    except asyncio.TimeoutError:
        p.kill(); await p.wait(); raise RuntimeError('ADB: истекло время ожидания')
    if p.returncode: raise RuntimeError(err.decode(errors='replace')[-800:])
    return out.decode(errors='replace').strip()

async def connect_device(serial):
    p=await asyncio.create_subprocess_exec(ADB,'connect',serial,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    await asyncio.wait_for(p.wait(),10)

def current(token):
    if not token: return None
    return db.one('''SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token=? AND s.expires>? AND u.active=1''',(hashlib.sha256(token.encode()).hexdigest(),time.time()))

def user(request):
    u=current(request.cookies.get('fermde_session'))
    if not u: fail('Войдите в аккаунт',401)
    return u

def admin(request):
    u=user(request)
    if not u['admin']: fail('Доступ только администратору',403)
    return u

def device_for(u,i):
    d=db.one('SELECT * FROM devices WHERE id=?',(i,))
    if not d or (not u['admin'] and d['owner']!=u['id']): fail('Устройство не найдено',404)
    return d

def safe_user(u): return {k:u[k] for k in ('id','username','admin','active','quota','running_quota')}

async def floppy(path,payload=None):
    encrypted=db.setting('floppy_key')
    if not encrypted: fail('Администратор должен настроить FloppyData')
    async with httpx.AsyncClient(timeout=30) as c:
        response=await c.request('POST' if payload is not None else 'GET',
            'https://api.floppydata.net/v2/'+path,headers={'X-Api-Key':db.decrypt(encrypted)},json=payload)
    if response.status_code>=400: raise RuntimeError(f'FloppyData: HTTP {response.status_code}. Проверьте ключ, баланс и регион.')
    return response.json()

def launch(coro):
    task=asyncio.create_task(coro); jobs.add(task); task.add_done_callback(jobs.discard)

async def perform(i, actor, action):
    async with operation_lock:
        d=db.one('SELECT * FROM devices WHERE id=?',(i,))
        if not d: return
        try:
            if action=='create':
                result=await floppy('proxy/rotating/connections',dict(type='residential',country=db.setting('country','RU'),
                    protocol='socks5',rotation=0,session=d['session'],udp=True))
                conn=result['connection']
                if conn.get('protocol')!='socks5': raise RuntimeError('FloppyData вернул неподдерживаемый протокол')
                db.execute('UPDATE devices SET proxy=? WHERE id=?',(db.encrypt(json.dumps(conn)),i))
                await agent('create',id=i,profile=d['profile'])
                db.execute("UPDATE devices SET status='stopped',error='' WHERE id=?",(i,))
            elif action=='start':
                inventory=await agent('inventory')
                running={x['id'] for x in inventory if x['active']}
                owner=db.one('SELECT * FROM users WHERE id=?',(d['owner'],))
                if not owner or not owner['active']: raise RuntimeError('Владелец отключён')
                owned=sum(x['id'] in running for x in db.rows('SELECT id FROM devices WHERE owner=?',(d['owner'],)))
                if len(running)>=int(db.setting('max_running')) or owned>=owner['running_quota']:
                    raise RuntimeError('Достигнут лимит одновременно работающих устройств')
                g=await agent('stats')
                needed=sum(int(db.setting(x)) for x in ('reserve_mib','headroom_mib','device_budget_mib'))
                if g['free']<needed: raise RuntimeError('Недостаточно свободной VRAM. Работающие телефоны сохранены.')
                result=await agent('start',id=i,connection=json.loads(db.decrypt(d['proxy'])))
                serial=result['serial']
                db.execute("UPDATE devices SET status='booting',wanted=1,error='' WHERE id=?",(i,))
                for _ in range(90):
                    try:
                        await connect_device(serial)
                        if await adb(serial,'shell','getprop','sys.boot_completed',timeout=5)=='1': break
                    except Exception: pass
                    await asyncio.sleep(2)
                else: raise RuntimeError('Android ещё не загрузился. Процесс оставлен работающим; проверьте журнал.')
                await adb(serial,'shell','settings','put','global','device_name',f'Phone-{i}')
                db.execute("UPDATE devices SET status='running',error='' WHERE id=?",(i,))
                try:
                    result=await agent('check_proxy',id=i)
                    db.execute('UPDATE devices SET ip=? WHERE id=?',(result['ip'],i))
                except Exception as e:
                    db.execute('UPDATE devices SET ip=?,error=? WHERE id=?',('',str(e)[:500],i))
            elif action=='stop':
                await agent('stop',id=i)
                db.execute("UPDATE devices SET status='stopped',wanted=0,error='' WHERE id=?",(i,))
            elif action=='delete':
                await agent('delete',id=i)
                db.execute('DELETE FROM devices WHERE id=?',(i,))
            db.audit(actor,action,i,'completed')
        except Exception as e:
            # Credentials never appear in generic httpx messages displayed here.
            message=str(e)[:1000] if not isinstance(e,httpx.HTTPError) else 'Ошибка соединения с FloppyData'
            db.execute("UPDATE devices SET status='error',error=? WHERE id=?",(message,i))
            db.audit(actor,action,i,'failed')

async def reconcile():
    while True:
        await asyncio.sleep(15)
        if operation_lock.locked(): continue
        try:
            inv={x['id']:x for x in await agent('inventory')}
            for d in db.rows('SELECT * FROM devices'):
                item=inv.get(d['id'])
                if item and item['active']:
                    try:
                        await connect_device(item['serial'])
                        boot=await adb(item['serial'],'shell','getprop','sys.boot_completed',timeout=5)
                        if boot=='1' and d['status']!='running':
                            db.execute("UPDATE devices SET status='running',wanted=1 WHERE id=?",(d['id'],))
                    except Exception: pass
                elif d['status'] in ('running','booting','starting','stopping','creating','deleting'):
                    db.execute("UPDATE devices SET status='error',error=? WHERE id=?",
                        ('Процесс не работает. Данные сохранены; проверьте журнал.',d['id']))
        except Exception: pass

@asynccontextmanager
async def lifespan(app):
    db.init()
    monitor=asyncio.create_task(reconcile())
    yield
    monitor.cancel()
    # Emulator units outlive the web process. Never stop them on app shutdown.

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)

@app.middleware('http')
async def security(request,call_next):
    if request.method not in ('GET','HEAD','OPTIONS'):
        if request.headers.get('origin')!=ORIGIN:
            return JSONResponse({'detail':'Недопустимый источник запроса'},status_code=403)
    r=await call_next(request)
    r.headers['X-Content-Type-Options']='nosniff'
    r.headers['X-Frame-Options']='DENY'
    r.headers['Referrer-Policy']='same-origin'
    r.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'"
    if request.url.path.startswith('/api'): r.headers['Cache-Control']='no-store'
    return r

@app.exception_handler(RuntimeError)
async def runtime_error(request,exc): return JSONResponse({'detail':str(exc)},status_code=400)

@app.get('/')
async def index(): return FileResponse(STATIC/'index.html')

@app.get('/healthz')
async def health(): return {'ok':True}

@app.post('/api/login')
async def login(request:Request):
    b=await request.json()
    address=request.client.host
    attempt=db.one('SELECT * FROM attempts WHERE address=?',(address,))
    if attempt and attempt['count']>=10 and time.time()-attempt['since']<300: fail('Слишком много попыток. Подождите 5 минут.',429)
    u=db.one('SELECT * FROM users WHERE username=?',(str(b.get('username',''))[:80],))
    password=str(b.get('password',''))[:1024]
    if not u or not u['active'] or not db.password_ok(password,u['password']):
        with db.transaction() as conn:
            conn.execute('''INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(address) DO UPDATE SET
               count=CASE WHEN since<? THEN 1 ELSE count+1 END,
               since=CASE WHEN since<? THEN excluded.since ELSE since END''',
               (address,time.time(),time.time()-300,time.time()-300))
        fail('Неверный логин или пароль',401)
    db.execute('DELETE FROM attempts WHERE address=?',(address,))
    token=secrets.token_urlsafe(32)
    db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
    db.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),u['id'],time.time()+43200))
    r=JSONResponse(safe_user(u))
    r.set_cookie('fermde_session',token,httponly=True,secure=ORIGIN.startswith('https:'),samesite='strict',max_age=43200)
    return r

@app.post('/api/logout')
async def logout(request:Request):
    token=request.cookies.get('fermde_session','')
    db.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))
    r=JSONResponse({'ok':True}); r.delete_cookie('fermde_session'); return r

@app.get('/api/me')
async def me(request:Request): return safe_user(user(request))

@app.post('/api/password')
async def change_password(request:Request):
    u=user(request); b=await request.json()
    if not db.password_ok(str(b.get('old','')),u['password']): fail('Неверный текущий пароль')
    p=str(b.get('password',''))
    if not 12<=len(p)<=200: fail('Пароль: от 12 до 200 символов')
    db.execute('UPDATE users SET password=? WHERE id=?',(db.password_hash(p),u['id']))
    db.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],)); return {'ok':True}

@app.get('/api/profiles')
async def profiles(request:Request): user(request); return PROFILES

@app.get('/api/devices')
async def devices(request:Request):
    u=user(request)
    fields='d.id,d.name,d.owner,d.profile,d.status,d.error,d.ip,d.created,d.wanted,u.username AS owner_name'
    sql=f'SELECT {fields} FROM devices d JOIN users u ON d.owner=u.id'
    return db.rows(sql+(' ORDER BY d.id DESC' if u['admin'] else ' WHERE d.owner=? ORDER BY d.id DESC'),() if u['admin'] else (u['id'],))

@app.post('/api/devices')
async def create_device(request:Request):
    u=user(request); b=await request.json()
    name=str(b.get('name','')).strip(); profile=b.get('profile','pixel_7')
    if not 1<=len(name)<=60 or profile not in PROFILES: fail('Проверьте название и профиль')
    if not db.setting('floppy_key'): fail('Сначала настройте FloppyData в админке')
    with db.transaction() as conn:
        count=conn.execute('SELECT count(*) FROM devices WHERE owner=?',(u['id'],)).fetchone()[0]
        if count>=u['quota']: fail('Достигнута квота созданных устройств')
        seq=conn.execute("SELECT seq FROM sqlite_sequence WHERE name='devices'").fetchone()
        if seq and seq[0]>=200: fail('Достигнут предел ID этой установки; требуется расширение адресного пула')
        i=conn.execute('INSERT INTO devices(name,owner,profile,session,created) VALUES (?,?,?,?,?)',
                       (name,u['id'],profile,'phone_'+secrets.token_hex(12),time.time())).lastrowid
    db.audit(u['id'],'create',i,'queued'); launch(perform(i,u['id'],'create')); return {'id':i}

@app.post('/api/devices/{i}/actions/{action}')
async def device_action(i:int,action:str,request:Request):
    u=user(request); d=device_for(u,i)
    if action not in ('start','stop','delete','check-proxy','restart','change-proxy'): fail('Неизвестное действие')
    if action=='change-proxy':
        if not u['admin']: fail('Доступ только администратору',403)
        if d['status']!='stopped': fail('Сначала остановите устройство')
        async with operation_lock:
            session='phone_'+secrets.token_hex(12)
            result=await floppy('proxy/rotating/connections',dict(type='residential',country=db.setting('country'),
                protocol='socks5',rotation=0,session=session,udp=True))
            if result['connection'].get('protocol')!='socks5': fail('Неподдерживаемое подключение')
            db.execute("UPDATE devices SET proxy=?,session=?,ip='' WHERE id=?",
                (db.encrypt(json.dumps(result['connection'])),session,i))
            db.audit(u['id'],'change-proxy',i)
        return {'ok':True}
    if action=='check-proxy':
        if d['status']!='running': fail('Сначала запустите телефон')
        try:
            result=await agent('check_proxy',id=i)
        except RuntimeError as e:
            db.execute('UPDATE devices SET ip=?,error=? WHERE id=?',('',str(e)[:500],i))
            fail(str(e),502)
        db.execute("UPDATE devices SET ip=?,error='' WHERE id=?",(result['ip'],i)); return result
    if d['status'] in ('creating','starting','booting','stopping','deleting'): fail('Дождитесь завершения текущей операции')
    if action=='restart':
        if d['status']!='running': fail('Телефон не запущен')
        await adb(f'10.231.{i}.2:15555','reboot')
        db.execute("UPDATE devices SET status='booting' WHERE id=?",(i,))
        db.audit(u['id'],'restart',i); return {'ok':True}
    if action=='start' and d['status']=='running': return {'ok':True}
    if action=='start' and not d['proxy']: fail('Подключение прокси не создано. Удалите устройство и создайте заново.')
    state={'start':'starting','stop':'stopping','delete':'deleting'}[action]
    with db.transaction() as conn:
        changed=conn.execute('UPDATE devices SET status=? WHERE id=? AND status=?',(state,i,d['status'])).rowcount
        if not changed: fail('Операция уже началась')
    db.audit(u['id'],action,i,'queued'); launch(perform(i,u['id'],action)); return {'ok':True}

@app.patch('/api/devices/{i}')
async def edit_device(i:int,request:Request):
    u=user(request); d=device_for(u,i); b=await request.json()
    name=str(b.get('name',d['name'])).strip()
    if not 1<=len(name)<=60: fail('Название: 1–60 символов')
    owner=d['owner']
    if u['admin'] and 'owner' in b:
        owner=int(b['owner'])
        target=db.one('SELECT * FROM users WHERE id=? AND active=1',(owner,))
        if not target: fail('Владелец не найден')
        if owner!=d['owner'] and db.one('SELECT count(*) n FROM devices WHERE owner=?',(owner,))['n']>=target['quota']: fail('Квота владельца исчерпана')
    db.execute('UPDATE devices SET name=?,owner=? WHERE id=?',(name,owner,i))
    db.audit(u['id'],'edit',i); return {'ok':True}

@app.get('/api/devices/{i}/logs')
async def logs(i:int,request:Request):
    admin(request); return await agent('logs',id=i)

@app.post('/api/devices/{i}/upload')
async def upload(i:int,request:Request,file:UploadFile=File(...)):
    u=user(request); d=device_for(u,i)
    if d['status']!='running': fail('Сначала запустите телефон')
    suffix='.apk' if (file.filename or '').lower().endswith('.apk') else '.bin'
    path=db.DATA/'uploads'/ (secrets.token_hex(16)+suffix)
    path.parent.mkdir(exist_ok=True)
    try:
        size=0
        with path.open('wb') as dest:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>512*1024*1024: fail('Максимальный размер файла — 512 МБ')
                dest.write(chunk)
        serial=f'10.231.{i}.2:15555'
        if suffix=='.apk':
            result=await adb(serial,'install','-r',str(path),timeout=180)
            if 'Success' not in result: fail('Android не установил APK: '+result[:300])
        else:
            name=re.sub(r'[^a-zA-Z0-9_.-]','_',file.filename or 'upload.bin')[:100].lstrip('.') or 'upload.bin'
            await adb(serial,'push',str(path),'/sdcard/Download/'+name,timeout=180)
        db.audit(u['id'],'upload',i); return {'ok':True}
    finally: path.unlink(missing_ok=True)

@app.get('/api/admin/users')
async def users(request:Request): admin(request); return [safe_user(u) for u in db.rows('SELECT * FROM users')]

@app.post('/api/admin/users')
async def add_user(request:Request):
    u=admin(request); b=await request.json()
    name=str(b.get('username','')); password=str(b.get('password',''))
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{3,40}',name) or not 12<=len(password)<=200: fail('Логин: 3–40 латинских символов. Пароль: 12–200 символов.')
    if db.one('SELECT id FROM users WHERE username=?',(name,)): fail('Логин занят')
    i=db.execute('INSERT INTO users(username,password,quota,running_quota) VALUES (?,?,?,?)',
                 (name,db.password_hash(password),max(1,min(50,int(b.get('quota',4)))),max(1,min(4,int(b.get('running_quota',2))))))
    db.audit(u['id'],'user-create',detail=str(i)); return {'id':i}

@app.patch('/api/admin/users/{i}')
async def edit_user(i:int,request:Request):
    actor=admin(request); b=await request.json(); u=db.one('SELECT * FROM users WHERE id=?',(i,))
    if not u: fail('Пользователь не найден',404)
    active=int(bool(b.get('active',u['active'])))
    if i==actor['id'] and not active: fail('Нельзя отключить себя')
    password=u['password']
    if b.get('password'):
        if not 12<=len(b['password'])<=200: fail('Пароль: 12–200 символов')
        password=db.password_hash(b['password'])
    db.execute('UPDATE users SET active=?,quota=?,running_quota=?,password=? WHERE id=?',
               (active,max(1,min(50,int(b.get('quota',u['quota'])))),max(1,min(4,int(b.get('running_quota',u['running_quota'])))),password,i))
    db.execute('DELETE FROM sessions WHERE user_id=?',(i,))
    db.audit(actor['id'],'user-edit',detail=str(i)); return {'ok':True}

@app.get('/api/admin/settings')
async def settings(request:Request):
    admin(request)
    return {**{k:db.setting(k) for k in ('country','max_running','reserve_mib','headroom_mib','device_budget_mib')},'has_key':bool(db.setting('floppy_key'))}

@app.post('/api/admin/settings')
async def save_settings(request:Request):
    u=admin(request); b=await request.json()
    country=str(b.get('country','RU')).upper()
    if not re.fullmatch('[A-Z]{2}',country): fail('Страна: двухбуквенный код')
    if b.get('api_key'):
        key=str(b['api_key']).strip()
        if not 8<=len(key)<=500: fail('Некорректный API-ключ')
        db.set_setting('floppy_key',db.encrypt(key))
    db.set_setting('country',country)
    for key,low,high in [('max_running',1,4),('reserve_mib',8192,14000),('headroom_mib',1024,4096),('device_budget_mib',1536,4096)]:
        if key in b:
            value=int(b[key])
            if not low<=value<=high: fail(f'{key}: диапазон {low}–{high}')
            db.set_setting(key,value)
    db.audit(u['id'],'settings'); return {'ok':True}

@app.get('/api/admin/balance')
async def balance(request:Request): admin(request); return await floppy('account/balances')

@app.get('/api/admin/stats')
async def stats(request:Request):
    admin(request); return {**await agent('stats'),'reserve_target':int(db.setting('reserve_mib'))}

@app.get('/api/admin/audit')
async def audit(request:Request):
    admin(request); return db.rows('SELECT a.*,u.username FROM audit a LEFT JOIN users u ON a.actor=u.id ORDER BY a.id DESC LIMIT 150')

@app.websocket('/api/devices/{i}/screen')
async def screen(ws:WebSocket,i:int):
    if ws.headers.get('origin')!=ORIGIN: await ws.close(code=4403); return
    u=current(ws.cookies.get('fermde_session'))
    if not u: await ws.close(code=4401); return
    try: d=device_for(u,i)
    except HTTPException: await ws.close(code=4404); return
    if d['status']!='running': await ws.close(code=4409); return
    from fermde.stream import serve
    await serve(ws,i,u)

app.mount('/static',StaticFiles(directory=STATIC),name='static')
