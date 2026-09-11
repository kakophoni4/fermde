"""Download version-pinned upstream binaries on the server, validate GitHub digest
when available, and record actual SHA256. No credentials or latest URLs."""
import hashlib
import json
import os
import urllib.request
import zipfile
from pathlib import Path

def get(url):
    request=urllib.request.Request(url,headers={'User-Agent':'fermde-installer','Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(request,timeout=120) as r: return r.read()

def release(repo,tag,name):
    data=json.loads(get(f'https://api.github.com/repos/{repo}/releases/tags/{tag}'))
    asset=next(x for x in data['assets'] if x['name']==name)
    content=get(asset['browser_download_url'])
    digest=hashlib.sha256(content).hexdigest()
    if asset.get('digest') and asset['digest']!='sha256:'+digest:
        raise RuntimeError('Upstream asset checksum mismatch')
    return content,dict(repository=repo,tag=tag,asset=name,sha256=digest,upstream_digest=asset.get('digest'))

out=Path('/opt/fermde/vendor');out.mkdir(exist_ok=True)
records=[]
content,record=release('Genymobile/scrcpy','v3.3.4','scrcpy-server-v3.3.4')
(out/'scrcpy-server.jar').write_bytes(content);records.append(record)
content,record=release('xjasonlyu/tun2socks','v2.6.0','tun2socks-linux-amd64.zip')
archive=out/'tun2socks.zip';archive.write_bytes(content)
with zipfile.ZipFile(archive) as z:
    name=next(n for n in z.namelist() if Path(n).name=='tun2socks-linux-amd64')
    target=Path('/usr/local/bin/tun2socks');target.write_bytes(z.read(name));target.chmod(0o755)
records.append(record);archive.unlink()
(out/'sources.json').write_text(json.dumps(records,indent=2)+'\n')
print('Pinned upstream assets downloaded. Checksums: /opt/fermde/vendor/sources.json')
