"""Explicit root operation to disable extra GPU reserves in an existing install."""
import json
import os
import sqlite3
from pathlib import Path

if os.geteuid()!=0:
    raise SystemExit('Run as root')
path=Path('/etc/fermde/limits.json')
limits=json.loads(path.read_text())
limits.update(reserve_mib=0,headroom_mib=0)
temp=path.with_suffix('.json.new')
temp.write_text(json.dumps(limits))
temp.chmod(0o600)
temp.replace(path)
with sqlite3.connect('/var/lib/fermde/panel.db',timeout=30) as db:
    for key in ('reserve_mib','headroom_mib'):
        db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,'0'))
print('GPU reserve: 0 MiB. Headroom: 0 MiB. Device budget:',limits['device_budget_mib'],'MiB.')
