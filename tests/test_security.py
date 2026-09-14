"""Run ONLY on the target server with deploy/check-server.sh."""
import hashlib
import asyncio
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from fermde import store as db
from fermde.app import app, ORIGIN
from fermde.stream import control_packet

class AccessTests(unittest.TestCase):
    def setUp(self):
        import fermde.app as lifecycle
        lifecycle.operation_lock=asyncio.Lock()
        lifecycle.device_locks.clear()
        self.temp=tempfile.TemporaryDirectory()
        self.old=db.DATA; db.DATA=Path(self.temp.name);db.init()
        self.a=db.execute('INSERT INTO users(username,password,admin) VALUES (?,?,1)',('admin',db.password_hash('test-password-123')))
        self.u=db.execute('INSERT INTO users(username,password) VALUES (?,?)',('user',db.password_hash('test-password-456')))
        self.d=db.execute('INSERT INTO devices(name,owner,profile,status,session,created) VALUES (?,?,?,?,?,?)',
            ('private',self.a,'pixel_7','stopped','test-session',time.time()))
        self.client=TestClient(app,base_url='https://testserver')

    def tearDown(self): self.client.close(); db.DATA=self.old;self.temp.cleanup()
    def login(self,name='user',password='test-password-456'):
        r=self.client.post('/api/login',headers={'origin':ORIGIN},json={'username':name,'password':password})
        self.assertEqual(r.status_code,200)

    def test_password_hash(self):
        h=db.password_hash('hello-very-long-password')
        self.assertTrue(db.password_ok('hello-very-long-password',h))
        self.assertFalse(db.password_ok('wrong',h))

    def test_csrf_rejected(self):
        r=self.client.post('/api/login',json={'username':'admin','password':'test-password-123'})
        self.assertEqual(r.status_code,403)

    def test_anonymous_denied(self):
        self.assertEqual(self.client.get('/api/devices').status_code,401)

    def test_owner_filter(self):
        self.login()
        self.assertEqual(self.client.get('/api/devices').json(),[])
        for action in ['start','stop','delete','check-proxy','restart']:
            r=self.client.post(f'/api/devices/{self.d}/actions/{action}',headers={'origin':ORIGIN})
            self.assertEqual(r.status_code,404)
        self.assertEqual(self.client.get('/api/admin/settings').status_code,403)

    def test_admin_listing_has_no_credentials(self):
        self.login('admin','test-password-123')
        r=self.client.get('/api/devices').json()[0]
        self.assertNotIn('proxy',r);self.assertNotIn('session',r)
        users=self.client.get('/api/admin/users').json()
        self.assertTrue(all('password' not in u for u in users))

    def test_disabled_user_session_revoked(self):
        self.login();db.execute('UPDATE users SET active=0 WHERE id=?',(self.u,))
        self.assertEqual(self.client.get('/api/devices').status_code,401)

    def test_quota_before_agent(self):
        self.login()
        db.execute('UPDATE users SET quota=0 WHERE id=?',(self.u,))
        db.set_setting('floppy_key','configured-placeholder')
        r=self.client.post('/api/devices',headers={'origin':ORIGIN},json={'name':'test','profile':'pixel_7'})
        self.assertEqual(r.status_code,400)

    def test_control_bounds_and_unicode(self):
        packet=control_packet({'type':'touch','action':0,'x':2,'y':-1},720,1600)
        fields=struct.unpack('>BBQiiHHHII',packet)
        self.assertEqual(fields[3:5],(719,0))
        packet=control_packet({'type':'text','text':'Привет'},720,1600)
        self.assertEqual(packet[14:].decode(),'Привет')
        with self.assertRaises(ValueError): control_packet({'type':'shell','text':'id'},720,1600)
        with self.assertRaises(ValueError): control_packet({'type':'key','key':999999},720,1600)

    def test_reserve_cannot_be_negative(self):
        self.login('admin','test-password-123')
        r=self.client.post('/api/admin/settings',headers={'origin':ORIGIN},json={'reserve_mib':-1})
        self.assertEqual(r.status_code,400)

    def test_proxy_failure_is_json_and_clears_stale_ip(self):
        self.login('admin','test-password-123')
        db.execute("UPDATE devices SET status='running',ip='8.8.8.8' WHERE id=?",(self.d,))
        with patch('fermde.app.agent',new=AsyncMock(side_effect=RuntimeError('DNS timeout'))):
            r=self.client.post(f'/api/devices/{self.d}/actions/check-proxy',headers={'origin':ORIGIN})
        self.assertEqual(r.status_code,502)
        self.assertEqual(r.json()['detail'],'DNS timeout')
        self.assertEqual(db.one('SELECT ip FROM devices WHERE id=?',(self.d,))['ip'],'')

    def test_proxy_recovery_clears_error(self):
        self.login('admin','test-password-123')
        db.execute("UPDATE devices SET status='running',error='DNS timeout' WHERE id=?",(self.d,))
        with patch('fermde.app.agent',new=AsyncMock(return_value={'ip':'8.8.8.8'})):
            r=self.client.post(f'/api/devices/{self.d}/actions/check-proxy',headers={'origin':ORIGIN})
        self.assertEqual(r.status_code,200)
        self.assertEqual(db.one('SELECT error FROM devices WHERE id=?',(self.d,))['error'],'')

    def test_optional_setup_cannot_prevent_ready_state(self):
        from fermde.app import perform
        db.execute("UPDATE devices SET status='starting',proxy='test' WHERE id=?",(self.d,))
        host=AsyncMock(side_effect=[[],{'free':100000},{'serial':'10.231.1.2:15555'}])
        android=AsyncMock(side_effect=['1',RuntimeError('Optional setting timed out')])
        # Close the diagnostic coroutine without executing external operations.
        with patch('fermde.app.agent',host), patch('fermde.app.adb',android), \
             patch('fermde.app.connect_device',AsyncMock()), \
             patch('fermde.app.db.decrypt',return_value='{}'), \
             patch('fermde.app.launch',side_effect=lambda coro:coro.close()) as background:
            asyncio.run(perform(self.d,self.a,'start'))
        self.assertEqual(db.one('SELECT status FROM devices WHERE id=?',(self.d,))['status'],'running')
        background.assert_called_once()

    def test_stop_does_not_wait_for_another_phone_boot_lock(self):
        from fermde.app import perform, operation_lock
        db.execute("UPDATE devices SET status='stopping' WHERE id=?",(self.d,))
        async def scenario():
            async with operation_lock:
                await asyncio.wait_for(perform(self.d,self.a,'stop'),1)
        with patch('fermde.app.agent',AsyncMock(return_value={})):
            asyncio.run(scenario())
        self.assertEqual(db.one('SELECT status FROM devices WHERE id=?',(self.d,))['status'],'stopped')

    def test_two_phones_boot_concurrently(self):
        import fermde.app as lifecycle
        db.execute("UPDATE devices SET status='starting',proxy='test' WHERE id=?",(self.d,))
        second=db.execute('INSERT INTO devices(name,owner,profile,status,session,proxy,created) VALUES (?,?,?,?,?,?,?)',
                          ('second',self.a,'pixel_7','starting','second-session','test',time.time()))
        db.execute('UPDATE users SET running_quota=2 WHERE id=?',(self.a,))
        async def scenario():
            started=set(); both=asyncio.Event()
            async def host(action,**kwargs):
                if action=='inventory': return [{'id':i,'active':True} for i in started]
                if action=='stats': return {'free':100000}
                if action=='start':
                    started.add(kwargs['id'])
                    if len(started)==2: both.set()
                    return {'serial':str(kwargs['id'])}
                raise AssertionError(action)
            async def android(*args,**kwargs):
                await both.wait()
                return '1'
            with patch('fermde.app.agent',side_effect=host), patch('fermde.app.adb',side_effect=android), \
                 patch('fermde.app.db.decrypt',return_value='{}'), \
                 patch('fermde.app.launch',side_effect=lambda coro:coro.close()):
                await asyncio.wait_for(asyncio.gather(lifecycle.perform(self.d,self.a,'start'),
                                                    lifecycle.perform(second,self.a,'start')),2)
        asyncio.run(scenario())
        self.assertEqual([r['status'] for r in db.rows('SELECT status FROM devices ORDER BY id')],['running','running'])

    def test_booting_phone_reserves_memory_for_next_admission(self):
        import fermde.app as lifecycle
        db.execute("UPDATE devices SET status='booting' WHERE id=?",(self.d,))
        second=db.execute('INSERT INTO devices(name,owner,profile,status,session,proxy,created) VALUES (?,?,?,?,?,?,?)',
                          ('second',self.a,'pixel_7','starting','second-session','test',time.time()))
        db.execute('UPDATE users SET running_quota=2 WHERE id=?',(self.a,))
        db.set_setting('max_running',4)
        db.set_setting('reserve_mib',8192)
        db.set_setting('headroom_mib',1024)
        db.set_setting('device_budget_mib',1536)
        host=AsyncMock(side_effect=[[{'id':self.d,'active':True}],{'free':12000}])
        with patch('fermde.app.agent',host):
            asyncio.run(lifecycle.perform(second,self.a,'start'))
        self.assertEqual(host.await_count,2)
        self.assertIn('VRAM',db.one('SELECT error FROM devices WHERE id=?',(second,))['error'])

    def test_recover_interrupted_stop_cleans_bridge_and_status(self):
        from fermde.app import reconcile_device
        db.execute("UPDATE devices SET status='stopping' WHERE id=?",(self.d,))
        with patch('fermde.app.agent',AsyncMock(return_value={})) as host:
            asyncio.run(reconcile_device(self.d,{'active':False,'busy':False}))
        host.assert_awaited_once_with('stop',id=self.d)
        self.assertEqual(db.one('SELECT status FROM devices WHERE id=?',(self.d,))['status'],'stopped')

    def test_recovery_does_not_race_root_operation(self):
        from fermde.app import reconcile_device
        db.execute("UPDATE devices SET status='stopping' WHERE id=?",(self.d,))
        with patch('fermde.app.agent',AsyncMock()) as host:
            asyncio.run(reconcile_device(self.d,{'active':False,'busy':True}))
        host.assert_not_awaited()
        self.assertEqual(db.one('SELECT status FROM devices WHERE id=?',(self.d,))['status'],'stopping')

    def test_file_roundtrip_and_cross_user_isolation(self):
        self.login('admin','test-password-123')
        r=self.client.post('/api/files',headers={'origin':ORIGIN},files={'file':('отчёт.txt',b'example bytes')})
        self.assertEqual(r.status_code,200)
        key=r.json()['id']
        self.assertEqual(self.client.get('/api/files/'+key+'/download').content,b'example bytes')
        self.assertEqual(self.client.get('/api/files').json()['files'][0]['name'],'отчёт.txt')
        self.login()
        self.assertEqual(self.client.get('/api/files').json()['files'],[])
        self.assertEqual(self.client.get('/api/files/'+key+'/download').status_code,404)
        self.assertEqual(self.client.delete('/api/files/'+key,headers={'origin':ORIGIN}).status_code,404)

    def test_file_name_cannot_escape_storage(self):
        self.login()
        r=self.client.post('/api/files',headers={'origin':ORIGIN},files={'file':('../../escape.txt',b'data')})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['name'],'escape.txt')
        self.assertFalse((db.DATA/'escape.txt').exists())

    def test_phone_file_access_requires_ownership(self):
        self.login()
        self.assertEqual(self.client.get(f'/api/devices/{self.d}/files').status_code,404)
        self.assertEqual(self.client.post(f'/api/devices/{self.d}/files/import',headers={'origin':ORIGIN},json={'name':'a.txt'}).status_code,404)

    def test_phone_import_rejects_path_traversal(self):
        self.login('admin','test-password-123')
        db.execute("UPDATE devices SET status='running' WHERE id=?",(self.d,))
        with patch('fermde.app.adb',AsyncMock()) as command:
            r=self.client.post(f'/api/devices/{self.d}/files/import',headers={'origin':ORIGIN},json={'name':'../secret'})
        self.assertEqual(r.status_code,400)
        command.assert_not_awaited()

if __name__=='__main__': unittest.main()
