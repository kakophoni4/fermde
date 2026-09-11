"""Run ONLY on the target server with deploy/check-server.sh."""
import hashlib
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

    def test_reserve_cannot_be_lowered(self):
        self.login('admin','test-password-123')
        r=self.client.post('/api/admin/settings',headers={'origin':ORIGIN},json={'reserve_mib':0})
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

if __name__=='__main__': unittest.main()
