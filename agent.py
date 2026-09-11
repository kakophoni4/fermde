#!/usr/bin/env python3
"""Root-only narrow host agent. Invoked via sudo, one JSON object on stdin.

No shell commands or arbitrary paths accepted from the web service. Host files,
units and namespace names are derived from bounded integer device IDs.
"""
import fcntl
import ipaddress
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
from fermde.profiles import PROFILES, IMAGE

ROOT = Path('/var/lib/fermde-agent')
SDK = '/opt/fermde-sdk'

def run(*args, check=True, input=None, timeout=120):
    p = subprocess.run(list(map(str,args)), input=input, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    if check and p.returncode:
        # Never expose arguments: tun2socks/proxy credentials may occur in them.
        raise RuntimeError(f'{Path(str(args[0])).name} failed ({p.returncode}): ' + p.stderr[-1200:])
    return p.stdout.strip()

def write(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    tmp.write_text(value)
    tmp.chmod(mode)
    tmp.replace(path)

def names(i):
    return f'fd{i}', ROOT / str(i), f'fermde-{i}'

def unit(i, component):
    return f'fermde-{component}-{i}.service'

def active(i):
    return run('systemctl','is-active',unit(i,'phone'),check=False) == 'active'

def gpu():
    data = run('nvidia-smi','--query-gpu=memory.total,memory.used,memory.free,utilization.gpu',
               '--format=csv,noheader,nounits').splitlines()[0]
    total, used, free, util = [int(x.strip()) for x in data.split(',')]
    mem = dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
    return dict(total=total,used=used,free=free,reserved=total-used-free,util=util,
                ram_available_mib=int(mem['MemAvailable'].strip().split()[0])//1024,
                disk_free_gib=shutil.disk_usage(ROOT).free//(1024**3), load=os.getloadavg()[0])

def manifest(i):
    return json.loads((ROOT / str(i) / 'manifest.json').read_text())

def public(i):
    m = manifest(i)
    return {'id':i,'active':active(i),'serial':m['ip']+':15555','profile':m['profile']}

def ipt(*args):
    run('iptables','-w','5',*args)

def ensure_ipt(table, chain, rule):
    p = subprocess.run(['iptables','-w','5','-t',table,'-C',chain,*rule],capture_output=True)
    if p.returncode:
        ipt('-t',table,'-I',chain,'1',*rule)

def network(i, conn):
    user, base, ns = names(i)
    peer = f'fdp{i}'
    hostif = f'fdh{i}'
    host = f'10.231.{i}.1'
    guest = f'10.231.{i}.2'
    port = int(conn['port'])
    if not 1 <= port <= 65535 or conn['protocol'] != 'socks5':
        raise ValueError('Only SOCKS5 proxy connections are supported')
    addresses = socket.getaddrinfo(conn['host'],port,socket.AF_INET,socket.SOCK_STREAM)
    addr = addresses[0][4][0]
    if not ipaddress.ip_address(addr).is_global:
        raise ValueError('Proxy endpoint must be a public IPv4 address')
    m=manifest(i)
    m['endpoint']=addr
    write(base/'manifest.json',json.dumps(m))
    run('ip','netns','add',ns)
    write(f'/etc/netns/{ns}/resolv.conf','nameserver 127.0.0.1\noptions timeout:3 attempts:2\n',0o644)
    run('ip','link','add',hostif,'type','veth','peer','name',peer)
    run('ip','link','set',peer,'netns',ns)
    run('ip','addr','add',host+'/30','dev',hostif)
    run('ip','link','set',hostif,'up')
    def nr(*args, **kw): return run('ip','netns','exec',ns,*args,**kw)
    nr('sysctl','-qw','net.ipv6.conf.all.disable_ipv6=1')
    nr('sysctl','-qw','net.ipv6.conf.default.disable_ipv6=1')
    nr('ip','link','set','lo','up')
    nr('ip','addr','add',guest+'/30','dev',peer)
    nr('ip','link','set',peer,'up')
    nr('ip','tuntap','add','dev','tun0','mode','tun')
    nr('ip','addr','add','198.18.0.1/15','dev','tun0')
    nr('ip','link','set','tun0','up')
    nr('ip','route','add',addr+'/32','via',host,'dev',peer)
    nr('ip','route','add','default','dev','tun0')
    # No direct route for guest payload. Only the proxy endpoint may leave veth.
    rules = f'''table inet fermde {{
      chain input {{ type filter hook input priority 0; policy drop;
        iifname "lo" accept
        iifname "tun0" accept
        ct state established,related accept
        ip saddr {host} tcp dport 15555 accept
      }}
      chain output {{ type filter hook output priority 0; policy drop;
        oifname "lo" accept
        oifname "tun0" accept
        ip daddr {host} tcp sport 15555 ct state established accept
        ip daddr {addr} tcp dport {port} accept
        ip daddr {addr} meta l4proto udp accept
      }}
    }}'''
    nr('nft','-f','-',input=rules)
    ensure_ipt('nat','POSTROUTING',['-s',guest+'/32','-j','MASQUERADE'])
    ensure_ipt('filter','FORWARD',['-i',hostif,'-d',addr,'-j','ACCEPT'])
    ensure_ipt('filter','FORWARD',['-o',hostif,'-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT'])
    proxy = f"socks5://{quote(conn['username'],safe='')}:{quote(conn['password'],safe='')}@{addr}:{port}"
    write(base/'tunnel.yml','device: tun://tun0\nproxy: '+json.dumps(proxy)+'\ninterface: '+peer+'\nloglevel: error\n')
    # Namespace deny rules remain if tun2socks dies: no fallback to host Internet.
    write(f'/etc/systemd/system/{unit(i,"proxy")}',f'''[Unit]
Description=Fermde proxy {i}
[Service]
NetworkNamespacePath=/run/netns/{ns}
ExecStart=/usr/local/bin/tun2socks -config {base}/tunnel.yml
Restart=on-failure
RestartSec=3
''',0o644)
    write(f'/etc/systemd/system/{unit(i,"adb")}',f'''[Unit]
Description=Fermde local ADB bridge {i}
[Service]
User={user}
NetworkNamespacePath=/run/netns/{ns}
ExecStart=/usr/bin/socat TCP4-LISTEN:15555,bind={guest},reuseaddr,fork TCP4:127.0.0.1:5555
Restart=on-failure
RestartSec=3
''',0o644)
    write(f'/etc/systemd/system/{unit(i,"dns")}',f'''[Unit]
Description=Fermde DNS over tunnel {i}
[Service]
NetworkNamespacePath=/run/netns/{ns}
ExecStart=/opt/fermde/venv/bin/python /opt/fermde/dns_forwarder.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
''',0o644)
    run('systemctl','daemon-reload')
    run('systemctl','start',unit(i,'proxy'),unit(i,'dns'),unit(i,'adb'))
    return guest, addr

def cleanup_network(i):
    user, base, ns = names(i)
    for kind in ('adb','dns','proxy'):
        run('systemctl','stop',unit(i,kind),check=False)
    m = json.loads((base/'manifest.json').read_text()) if (base/'manifest.json').exists() else {}
    guest = f'10.231.{i}.2'
    rules = [('nat','POSTROUTING',['-s',guest+'/32','-j','MASQUERADE']),
             ('filter','FORWARD',['-i',f'fdh{i}','-d',m.get('endpoint','0.0.0.0'),'-j','ACCEPT']),
             ('filter','FORWARD',['-o',f'fdh{i}','-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT'])]
    for table, chain, rule in rules:
        run('iptables','-w','5','-t',table,'-D',chain,*rule,check=False)
    run('ip','netns','del',ns,check=False)
    run('ip','link','del',f'fdh{i}',check=False)
    resolv=Path('/etc/netns')/ns/'resolv.conf'
    resolv.unlink(missing_ok=True)
    with __import__('contextlib').suppress(OSError): resolv.parent.rmdir()

def create(i, args):
    user, base, ns = names(i)
    if (base/'manifest.json').exists(): raise ValueError('Device already exists')
    profile = args['profile']
    if profile not in PROFILES: raise ValueError('Unknown profile')
    base.mkdir(parents=True,exist_ok=True)
    base.chmod(0o711)
    home = base/'home'
    try: pwd.getpwnam(user)
    except KeyError: run('useradd','-m','-d',home,'-s','/usr/sbin/nologin',user)
    run('usermod','-aG','kvm,video,render',user)
    home.mkdir(exist_ok=True)
    run('chown',f'{user}:{user}',home)
    env = ['runuser','-u',user,'--','env',f'HOME={home}',f'ANDROID_HOME={SDK}']
    run(*env,SDK+'/cmdline-tools/latest/bin/avdmanager','create','avd','--name','phone',
        '--package',IMAGE,'--device',profile,input='no\n',timeout=180,check=False)
    cfg = home/'.android/avd/phone.avd/config.ini'
    if not cfg.exists(): raise RuntimeError('AVD configuration missing')
    entries = dict(line.split('=',1) for line in cfg.read_text().splitlines() if '=' in line)
    p = PROFILES[profile]
    entries.update({'hw.ramSize':'4096','hw.cpu.ncore':'4','disk.dataPartition.size':'12G',
                    'hw.lcd.width':str(p['width']),'hw.lcd.height':str(p['height']),
                    'hw.lcd.density':str(p['density']),'hw.gpu.enabled':'yes','hw.gpu.mode':'host',
                    'fastboot.forceColdBoot':'yes','showDeviceFrame':'no'})
    cfg.write_text('\n'.join(f'{k}={v}' for k,v in entries.items())+'\n')
    write(base/'manifest.json',json.dumps({'id':i,'profile':profile,'ip':f'10.231.{i}.2'}))
    return public(i)

def start(i,args):
    if active(i): return public(i)
    user, base, ns = names(i)
    m = manifest(i)
    limits = json.loads(Path('/etc/fermde/limits.json').read_text())
    # Include legacy unmanaged emulators in the concurrent-device cap.
    processes = 0
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            c = p.read_bytes().split(b'\0')
            if c and b'qemu-system' in c[0] and b'-avd' in c: processes += 1
        except (OSError,PermissionError): pass
    g = gpu()
    if processes >= limits['max_running']: raise ValueError('Host device limit reached')
    if g['free'] < limits['reserve_mib']+limits['headroom_mib']+limits['device_budget_mib']:
        raise ValueError('Insufficient free VRAM; running devices will not be stopped')
    if g['ram_available_mib'] < 6144 or g['disk_free_gib'] < 16:
        raise ValueError('Insufficient RAM or disk space')
    cleanup_network(i)
    # A prior renderer failure may leave a stale multiinstance lock. Only this
    # service owns the device, and no emulator process may remain under its UID.
    uid=pwd.getpwnam(user).pw_uid
    for p in Path('/proc').glob('[0-9]*'):
        try:
            if p.stat().st_uid==uid and b'qemu-system' in (p/'cmdline').read_bytes():
                raise RuntimeError('Previous emulator process is still running')
        except (FileNotFoundError,PermissionError,ProcessLookupError): pass
    lock=base/'home/.android/avd/phone.avd/multiinstance.lock'
    if lock.is_file(): lock.unlink()
    keys=base/'home/.android'
    keys.mkdir(exist_ok=True)
    for name in ('adbkey','adbkey.pub'):
        shutil.copyfile('/var/lib/fermde/.android/'+name,keys/name)
        run('chown',f'{user}:{user}',keys/name)
        (keys/name).chmod(0o600)
    run('systemctl','start','android-display.service')
    # Wait for the existing X server rather than racing its socket creation.
    for _ in range(30):
        if Path('/tmp/.X11-unix/X99').exists(): break
        time.sleep(1)
    auth = base/'home/.Xauthority'
    shutil.copyfile('/home/android/.Xauthority',auth)
    run('chown',f'{user}:{user}',auth)
    auth.chmod(0o600)
    try:
        guest, endpoint = network(i,args['connection'])
        m.update(ip=guest,endpoint=endpoint)
        write(base/'manifest.json',json.dumps(m))
        # Netsim discovers its daemon through TMPDIR/XDG_RUNTIME_DIR. Sharing
        # /tmp/netsim.ini crosses network namespaces and collides between UIDs.
        # Keep the host /tmp visible for the NVIDIA X11 socket (no PrivateTmp).
        write(f'/etc/systemd/system/{unit(i,"phone")}',f'''[Unit]
Description=Fermde Android {i}
Requires=android-display.service
After=android-display.service
[Service]
User={user}
WorkingDirectory={base}/home
RuntimeDirectory=fermde-phone-{i}
RuntimeDirectoryMode=0700
Environment=HOME={base}/home
Environment=ANDROID_HOME={SDK}
Environment=TMPDIR=/run/fermde-phone-{i}
Environment=XDG_RUNTIME_DIR=/run/fermde-phone-{i}
Environment=ANDROID_TMP=/run/fermde-phone-{i}
Environment=DISPLAY=:99
Environment=XAUTHORITY={auth}
NetworkNamespacePath=/run/netns/{ns}
BindReadOnlyPaths=/etc/netns/{ns}/resolv.conf:/etc/resolv.conf
ExecStart={SDK}/emulator/emulator -avd phone -port 5554 -accel on -gpu host -no-window -no-snapshot -no-metrics -cores 4 -memory 4096 -camera-back none -camera-front none -dns-server 127.0.0.1 -timezone Europe/Samara
TimeoutStopSec=60
KillSignal=SIGTERM
KillMode=mixed
MemoryMax=8G
TasksMax=1024
''',0o644)
        run('systemctl','daemon-reload')
        run('systemctl','start',unit(i,'phone'))
        time.sleep(2)
        if not active(i): raise RuntimeError('Emulator exited; inspect device log')
        return public(i)
    except BaseException:
        cleanup_network(i)
        raise

def stop(i):
    run('systemctl','stop',unit(i,'phone'),check=False,timeout=80)
    if active(i): raise RuntimeError('Could not stop emulator')
    cleanup_network(i)
    return public(i)

def delete(i):
    user, base, ns = names(i)
    if (base/'manifest.json').exists(): stop(i)
    else:
        run('systemctl','stop',unit(i,'phone'),check=False)
        cleanup_network(i)
    # Only this validated device directory; do not follow symlinks.
    if base.is_symlink() or base.resolve().parent != ROOT.resolve(): raise ValueError('Invalid directory')
    if base.exists(): shutil.rmtree(base)
    run('userdel',user,check=False)
    for kind in ('phone','proxy','dns','adb'):
        Path('/etc/systemd/system',unit(i,kind)).unlink(missing_ok=True)
    shutdown_dir=Path('/etc/systemd/system',unit(i,'phone')+'.d')
    (shutdown_dir/'20-shutdown.conf').unlink(missing_ok=True)
    with __import__('contextlib').suppress(OSError): shutdown_dir.rmdir()
    run('systemctl','daemon-reload')
    return {'deleted':True}

def check_proxy(i):
    if not active(i):
        raise RuntimeError('Сначала запустите телефон')
    if run('systemctl','is-active',unit(i,'proxy'),check=False) != 'active':
        raise RuntimeError('Служба прокси не работает. Перезапустите телефон.')
    errors = []
    started = time.monotonic()
    for url in ('https://api.ipify.org', 'https://checkip.amazonaws.com'):
        p = subprocess.run(['ip','netns','exec',names(i)[2],'curl','--noproxy','*',
                            '-4','-fsS','--connect-timeout','12','--max-time','18',url],
                           capture_output=True,text=True,timeout=22)
        if p.returncode == 0:
            try:
                address = ipaddress.ip_address(p.stdout.strip())
                if address.version == 4 and address.is_global:
                    return {'ip':str(address),'checked_at':int(time.time()),
                            'elapsed_ms':round((time.monotonic()-started)*1000)}
            except ValueError:
                pass
        errors.append((p.returncode,p.stderr.lower()))
    if any(code == 6 or 'resolving' in error for code,error in errors):
        raise RuntimeError('DNS не отвечает через прокси. Перезапустите телефон после обновления DNS-службы.')
    if any(code == 60 for code,_ in errors):
        raise RuntimeError('Не удалось проверить TLS-сертификат сервиса проверки IP.')
    raise RuntimeError('HTTPS через прокси не отвечает. Проверьте доступность прокси-сессии.')


def main():
    if os.geteuid()!=0: raise PermissionError('Root agent required')
    ROOT.mkdir(mode=0o711,parents=True,exist_ok=True)
    req = json.loads(sys.stdin.read(32768))
    action = req.get('action')
    if action == 'stats': return gpu()
    if action == 'inventory':
        return [public(int(p.name)) for p in ROOT.iterdir() if p.name.isdigit() and (p/'manifest.json').exists()]
    i = req.get('id')
    if type(i) is not int or not 1<=i<=200: raise ValueError('Device id must be 1..200')
    # External diagnostics must not block lifecycle mutations.
    if action=='check_proxy': return check_proxy(i)
    with open('/run/lock/fermde-agent.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if action=='create': return create(i,req)
        if action=='start': return start(i,req)
        if action=='stop': return stop(i)
        if action=='delete': return delete(i)
        if action=='logs':
            return {'log':run('journalctl','-u',unit(i,'phone'),'-n','65','--no-pager')}
        raise ValueError('Unsupported action')

if __name__=='__main__':
    try: print(json.dumps({'ok':True,'result':main()}))
    except Exception as e:
        print(json.dumps({'ok':False,'error':str(e)[:1500]}))
        sys.exit(1)
