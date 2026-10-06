"""Build a reproducible OpenWrt 24.10 data-only IPK using Python's standard library."""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from check_secrets import violations

NAME = 'luci-app-hitwh-mwan'
VERSION = '1.0.0-3'
DEPENDS = ('luci-base, rpcd-mod-ucode, ucode, ucode-mod-fs, ucode-mod-uci, curl, jsonfilter, '
           'ip-full, kmod-macvlan, firewall4, coreutils-stat')

def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.GNU_FORMAT) as tar:
        parents = set()
        for name, _, _ in entries:
            parts = name.split('/')
            for count in range(1,len(parts)): parents.add('/'.join(parts[:count]))
        for name in sorted(parents):
            item = tarfile.TarInfo('./' + name + '/')
            item.type, item.mode, item.uid, item.gid, item.mtime = tarfile.DIRTYPE, 0o755, 0, 0, 0
            item.uname = item.gname = 'root'
            tar.addfile(item)
        for name, data, mode in sorted(entries):
            item = tarfile.TarInfo('./' + name)
            item.size, item.mode, item.uid, item.gid, item.mtime = len(data), mode, 0, 0, 0
            item.uname = item.gname = 'root'
            tar.addfile(item, io.BytesIO(data))
    return gzip.compress(buffer.getvalue(), mtime=0)

def payload():
    entries = []
    for source in sorted((ROOT / 'files').rglob('*')):
        if not source.is_file(): continue
        path = source.relative_to(ROOT / 'files').as_posix()
        data = source.read_bytes()
        findings = violations(path, data)
        if findings: raise ValueError(f'Refusing private/secret artifact: {path}')
        mode = 0o755 if path.startswith(('usr/sbin/', 'usr/libexec/', 'etc/init.d/', 'etc/hotplug.d/')) else 0o644
        entries.append((path, data, mode))
    for name in ('index.html', 'styles.css', 'app.js', 'transport.js'):
        data = (ROOT / 'dashboard/static' / name).read_bytes()
        if violations(name, data): raise ValueError('Static artifact failed credential scan')
        entries.append(('www/luci-static/resources/hitwh-mwan/' + name, data, 0o644))
    return entries

def build(output):
    entries = payload()
    installed = sum(len(data) for _, data, _ in entries)
    if installed > 512 * 1024: raise ValueError('Payload exceeds the 512 KiB storage budget')
    control = (f'Package: {NAME}\nVersion: {VERSION}\nArchitecture: all\n'
               f'Maintainer: HITwh multi-WAN contributors\nSection: luci\nPriority: optional\n'
               f'Depends: {DEPENDS}\nInstalled-Size: {installed}\n'
               'Description: LuCI multi-WAN management with manually triggered Ruijie ePortal authentication\n').encode()
    controls = [('control', control, 0o644), ('conffiles', b'/etc/config/hitwh_mwan\n', 0o644)]
    for name in ('preinst', 'postinst', 'prerm', 'postrm'):
        controls.append((name, (ROOT / 'packaging' / name).read_bytes(), 0o755))
    for name, content, _ in controls:
        if violations(name, content): raise ValueError(f'Control artifact failed credential scan: {name}')
    data = archive([('debian-binary', b'2.0\n', 0o644), ('control.tar.gz', archive(controls), 0o644),
                    ('data.tar.gz', archive(entries), 0o644)])
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    ipk = output / f'{NAME}_{VERSION}_all.ipk'; ipk.write_bytes(data)
    (output / 'SHA256SUMS').write_text(f'{hashlib.sha256(data).hexdigest()}  {ipk.name}\n', encoding='utf-8')
    print(f'{ipk}: {len(data)} bytes compressed, {installed} bytes payload')
    return ipk

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=str(ROOT / 'dist'))
    build(parser.parse_args().output)
