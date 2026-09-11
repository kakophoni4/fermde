# Fermde

Self-hosted multi-user Android workspace for an Ubuntu 22.04 NVIDIA/KVM server.
The web panel manages independent AVDs, streams scrcpy H.264 into browser WebCodecs,
and routes each managed device through a dedicated FloppyData SOCKS5 session.

**Status: initial implementation; target-server validation required. No local tests,
builds, emulator runs or deployments were performed, as requested. Do not onboard
other users or banking accounts before completing `docs/ACCEPTANCE.md`.**

## Included

- Russian responsive UI; users, password recovery, ownership and per-user quotas.
- Clean Pixel 5/6/7 Android 15 Google Play profiles; independent AVD homes and Linux UIDs.
- Create, start, browser view, stop, delete, rename, ownership transfer, reboot API.
- Mouse/touch input, navigation, Unicode clipboard paste, files and APK uploads.
- Quality presets and optional AAC audio using browser AudioDecoder/Web Audio.
- HTTPS WebSocket streaming. Viewing/disconnecting never stops an Android device.
- FloppyData encrypted API key, per-device sticky sessions, IP check, balance,
  stopped-device proxy replacement API.
- Separate network namespace and tun2socks per phone. Namespace firewall restricts
  physical egress to the SOCKS endpoint; DNS traverses the tunnel, IPv6 is disabled.
- Default hard cap 4 active emulators (including the legacy emulator), 8192 MiB
  reserve, 1024 MiB headroom and 1536 MiB admission budget per new emulator.
- Serialized host mutations, live status reconciliation, audit, config backups,
  explicit stopped-device backups and server-side tests.

## Important boundaries

- These remain Android emulators. Pixel hardware profiles do not remove `ranchu`,
  `sdk_gphone` or hardware-attestation differences. No guarantee for banks or Play Integrity.
- One person controls a device at a time. Different devices can be viewed concurrently.
- Browser video requires HTTPS and WebCodecs; start with current Chrome/Edge. Other
  browsers need testing. Audio additionally needs AudioDecoder; apps can prohibit
  audio capture. Sound starts only after the user presses «Включить звук».
- The memory policy refuses new starts; it is **not a VRAM partition**. Runtime growth
  can cross the target reserve. Running devices are never killed to enforce quotas.
- Sticky sessions are not dedicated permanent IPs. IP checks detect current changes
  and duplicates, not provider exclusivity. IP checks consume a small amount of traffic.
- UDP relay endpoints must use the resolved SOCKS endpoint IP. A provider returning a
  different relay IP will fail closed; confirm calls/DNS/video in server tests.
- The 4-device cap is conservative and provisional, not a measured server maximum.
- Service restarts preserve phones. After a complete host reboot, users manually start
  their phones, subject to normal admission checks. No automatic forced recovery.
- The legacy `/home/android/.android/avd/phone.avd` stays untouched, outside the panel.
  It still consumes GPU resources and counts toward the host cap. Do not import it by
  copying live account data into new devices.
- The first version has a lifetime ID range 1..200 (network addresses). Deleted IDs are
  intentionally not reused. Extend the allocator before provisioning more than 200.

## Architecture

Browser → Caddy HTTPS → FastAPI (unprivileged `fermde`, 127.0.0.1:8090).
The root-owned `agent.py` is invoked through an exact sudoers command and accepts
only bounded JSON operations. It controls `fermde-phone-N`, `fermde-proxy-N`, and
`fermde-adb-N` systemd units. No shell source or arbitrary paths are accepted.

Each phone has a network namespace `fermde-N`, address `10.231.N.2/30`, a UID `fdN`,
and disk at `/var/lib/fermde-agent/N/home`. Shared SDK files are read-only to device users.
The host-side `fermde-adb.service` runs ADB in the foreground on 5039, separate from
the existing 5037 server. It is supervised by systemd and starts after host reboot.
ADB forwarding and all streaming ports bind to localhost. Caddy is the only public UI.
Root-level access to the host remains trusted; this is not a hardened hostile-tenant
hypervisor offering. Android apps cannot directly access the panel's credentials.

## Install on the prepared server

Prerequisites already established in this project: Ubuntu x86_64, working NVIDIA
driver, `/dev/kvm`, Android SDK + API 35 Google Play image at `/home/android/sdk`,
and working NVIDIA X display `:99` via `android-display.service` and
`/home/android/.Xauthority`. Java 21 must be installed.

Use an unused domain pointing to this server, with inbound TCP 80/443 available.
For IP-only mode see below. Installer preserves the existing phone and SSH settings;
it enables IPv4 forwarding and adds per-device NAT/forwarding rules. Existing Docker,
UFW or custom firewall forwarding policies require the namespace acceptance tests.

```bash
apt-get update && apt-get install -y git tmux
tmux new -A -s fermde-install
git clone https://github.com/kakophoni4/fermde.git /opt/fermde-src
cd /opt/fermde-src
bash deploy/install.sh panel.example.com
```

For the user's existing IP (private CA, see next section):

```bash
bash deploy/install.sh 45.74.3.230
```

Enter a new panel admin password when prompted (12+ characters). Do not use the
server root password. Installation downloads fixed scrcpy 3.3.4 and tun2socks 2.6.0
assets; GitHub asset digests are checked when present and actual SHA256 values are
recorded in `/opt/fermde/vendor/sources.json`. Python dependency versions are pinned;
transitive dependencies and Ubuntu package updates are resolved on the server.

Then run **on the server**:

```bash
bash /opt/fermde/deploy/check-server.sh
```

Open the panel. In **Настройки**, enter the real FloppyData API key, save and verify
balance. Create one device; wait for completion; start and open it. Follow
`docs/ACCEPTANCE.md` before increasing concurrent usage.

Private GitHub repositories require your GitHub authentication for cloning. Use a
read-only deploy key on the server; do not embed a GitHub token in the clone URL.

## IP-only HTTPS

The installer uses Caddy's private CA for an IP address. **The client must trust its
public CA certificate** for reliable HTTPS/WebCodecs. No API credentials or server
private keys are needed on clients.

On the administrator's Windows PC:

```powershell
scp root@45.74.3.230:/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt .\fermde-root.crt
certutil -user -addstore Root .\fermde-root.crt
```

Verify the certificate fingerprint through the administrator's SSH connection before
distributing it. Trust this CA only on devices intended to use the panel. Distribute
**root.crt only**, never the CA private key. Users receive panel credentials, not SSH
credentials. A real domain with a publicly trusted certificate avoids this client setup.

## Operations

```bash
# Panel / device diagnostics
journalctl -u fermde -n 100 --no-pager
journalctl -u fermde-phone-1 -n 100 --no-pager
systemctl status fermde

# Admin recovery, no browser required
cd /opt/fermde
runuser -u fermde -- env HOME=/var/lib/fermde PYTHONPATH=/opt/fermde /opt/fermde/venv/bin/python -m fermde.admin reset-password --username admin

# Config backup (also runs daily); archives contain secrets, keep root-only
fermde-backup

# Full device backup: first explicitly stop device 1 in the panel
bash /opt/fermde/deploy/backup-device.sh 1
```

Quotas in the UI may be lowered or made more conservative. Raising above 4 devices
requires changing the root limits, API validation and UI bounds deliberately after
multi-device measurements. The installer does not reset an existing root limits file.

Device disks survive updates. For an update:

```bash
cd /opt/fermde-src
git pull --ff-only
bash deploy/install.sh panel.example.com
bash /opt/fermde/deploy/check-server.sh
```

Do not update while device lifecycle operations or file uploads are in progress.
The panel service restarts; browser viewers reconnect, but Android systemd units remain.
Use the same hostname when updating. To roll back, check out the previous release
commit and rerun the installer; restore the matching DB backup if a future migration
changes schema. Initial release only uses additive `CREATE TABLE IF NOT EXISTS`.

## Restore procedure

1. Stop **the panel** and explicitly stop affected phones; retain original archives.
2. Restore `/etc/fermde/secret.key` together with the matching SQLite backup. Without
   that key the stored proxy credentials cannot be decrypted. Restore to `panel.db`
   while the service is stopped; remove stale WAL/SHM only after ensuring no DB users.
3. Restore device archives to their original numeric paths, recreate matching `fdN`
   users if restoring on another host, and restore ownership by username. Never restore
   over a running AVD. Root agent manifests and original device IDs must match the DB.
4. Start the panel, verify users/settings, start one device through the UI. The agent
   rebuilds its namespace and systemd units from the stored connection on start.
5. Complete IP/ownership checks before allowing normal use. Caddy CA state requires a
   separate backup if preserving IP-mode trust across rebuilding the server.

## Upstream references

- Android Emulator: https://developer.android.com/studio/run/emulator-commandline
- scrcpy protocol/source (Apache-2.0): https://github.com/Genymobile/scrcpy/tree/v3.3.4
- tun2socks (MIT): https://github.com/xjasonlyu/tun2socks/tree/v2.6.0
- FloppyData: https://floppydata.com/docs/api-reference/v2/endpoint/rotating-connection
- Caddy installation: https://caddyserver.com/docs/install#debian-ubuntu-raspbian

Upstream binaries are downloaded on the server, not committed to this repository.
