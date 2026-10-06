"""Validate the release archive, file permissions, footprint and private-file guard."""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import os
import re
import shlex
import subprocess
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ipk_builder',ROOT/'tools/build_ipk.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)

class PackageTests(unittest.TestCase):
    def test_control_hook_secret_prevents_release_artifact_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(builder, 'violations', side_effect=lambda name, data:
                                   ['synthetic credential'] if name == 'postinst' else []):
                with self.assertRaisesRegex(ValueError, 'postinst'):
                    builder.build(temp)
            self.assertFalse(list(Path(temp).glob('*.ipk')))

    def test_ipk_is_reproducible_small_and_contains_native_gui_without_desktop_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            first=builder.build(Path(temp)/'first').read_bytes()
            second=builder.build(Path(temp)/'second').read_bytes()
        self.assertEqual(first,second)
        self.assertLess(len(first),128*1024)
        with tarfile.open(fileobj=io.BytesIO(first),mode='r:gz') as outer:
            self.assertEqual(outer.extractfile('./debian-binary').read(),b'2.0\n')
            data=outer.extractfile('./data.tar.gz').read()
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tar:
            names=tar.getnames()
            self.assertIn('./www/luci-static/resources/hitwh-mwan/index.html',names)
            self.assertIn('./usr/share/rpcd/ucode/hitwh-mwan.uc',names)
            self.assertFalse(any('auth.d/' in n or n.endswith('server.py') or 'config.toml' in n for n in names))
            self.assertEqual(tar.getmember('./usr/sbin/hitwh-mwan').mode,0o755)
            self.assertEqual(tar.getmember('./etc/config/hitwh_mwan').uid,0)
            self.assertLess(sum(m.size for m in tar.getmembers()),512*1024)

    @unittest.skipUnless(os.name=='posix','POSIX package-hook test')
    def test_remove_and_upgrade_hooks_preserve_network_identities_and_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);bindir=base/'bin';bindir.mkdir()
            network=base/'network';network.write_text('wan2 MAC=02:11:22:33:44:55\n')
            credentials=base/'wan2.json';credentials.write_text('{"password":"fake-secret"}')
            events=base/'events'
            wrapper='#!/bin/sh\nprintf "%s %s\\n" "${0##*/}" "$*" >>"$HOOK_EVENTS"\n'
            for name in ('hitwh-mwan','hitwh-mwan-dns','dnsmasq','nft','ip','uci'):
                path=bindir/name;path.write_text(wrapper);path.chmod(0o755)
            # A destructive uninstall helper makes this fixture fail immediately.
            destructive=bindir/'hitwh-mwan-uninstall'
            destructive.write_text('#!/bin/sh\nrm -f "$HOOK_NETWORK" "$HOOK_CREDENTIALS"\n');destructive.chmod(0o755)
            source=(ROOT/'packaging/prerm').read_text()
            source=re.sub(r'/etc/init.d/([a-z0-9-]+)',lambda m:shlex.quote(str(bindir/m[1])),source)
            source=source.replace('/usr/libexec/hitwh-mwan-uninstall',shlex.quote(str(destructive)))
            script=base/'prerm';script.write_text(source)
            env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],'IPKG_INSTROOT':'',
                 'HOOK_EVENTS':str(events),'HOOK_NETWORK':str(network),'HOOK_CREDENTIALS':str(credentials)}
            for action in ('remove','upgrade'):
                result=subprocess.run(['sh',str(script),action],env=env,capture_output=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(network.read_text(),'wan2 MAC=02:11:22:33:44:55\n')
                self.assertIn('fake-secret',credentials.read_text())
