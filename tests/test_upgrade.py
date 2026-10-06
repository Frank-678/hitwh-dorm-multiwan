"""Run the real updater with local release bytes and a recording opkg fixture."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ipk', ROOT/'tools/build_ipk.py')
builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)

def dispatch():
    state_file=Path(os.environ['UPGRADE_TEST_STATE']); state=json.loads(state_file.read_text())
    name,*args=sys.argv[2:]
    code=0
    if name=='id': print('0')
    elif name=='stat': print('0:'+format(stat.S_IMODE(Path(args[-1]).stat().st_mode),'o'))
    elif name=='df': print('Filesystem 1024-blocks Used Available Capacity Mounted\nmock 20000 10000 '+str(state.get('free',10000))+' 50% /overlay')
    elif name=='curl':
        state['requests'].append(args)
        if state.get('network_error'): code=7
        else:
            url=args[-1]; target=Path(args[args.index('-o')+1])
            content=(json.dumps(state['release']).encode() if url.endswith('/latest') else
                     Path(state['sums']).read_bytes() if url.endswith('/SHA256SUMS') else
                     b'tampered' if state.get('tampered') else Path(state['ipk']).read_bytes())
            target.write_bytes(content)
    elif name=='jsonfilter':
        data=json.loads(Path(args[args.index('-i')+1]).read_text()); expr=args[args.index('-e')+1]
        match=re.fullmatch(r'@\.assets\[(\d+)\]\.(\w+)',expr)
        value=data['assets'][int(match[1])].get(match[2],'') if match and int(match[1])<len(data['assets']) else '' if match else data.get(expr[2:],'')
        if isinstance(value,bool): value=str(value).lower()
        if value!='': print(value)
    elif name=='opkg':
        if args[0]=='status': print('Version: '+state['current'])
        elif args[0]=='compare-versions':
            parts=lambda v:tuple(int(n) for n in re.split(r'[.-]',v))
            code=0 if (parts(args[1])>=parts(args[3]) if args[2]=='>' else parts(args[1])>parts(args[3])) else 1
        elif args[:2]==['--noaction','install']:
            state['preflight']+=1; code=1 if state.get('dependency_failure') else 0
        elif args[0]=='install':
            state['installs']+=1; state['install_flag']=os.environ.get('HITWH_MWAN_IN_APP_UPGRADE')
            if state.get('install_failure'): code=1
            else: state['current']=builder.VERSION
        else: raise AssertionError(args)
    elif name=='setsid':
        if args[0]=='opkg': os.execv('/usr/bin/setsid',['setsid',*args])
        state['restart_scheduled']=True
    else: raise AssertionError(name)
    state_file.write_text(json.dumps(state)); raise SystemExit(code)

@unittest.skipUnless(os.name=='posix','POSIX private files and flock')
class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name); self.state_file=self.base/'state.json'
        self.control={'Package':'luci-app-hitwh-mwan','Version':builder.VERSION,'Architecture':'all','X-OpenWrt-Series':'24.10'}
        self.state={'current':'1.0.0-5','requests':[],'installs':0,'preflight':0}
        self.make_release()
        bindir=self.base/'bin'; bindir.mkdir()
        for name in ('id','stat','df','curl','jsonfilter','opkg','setsid'):
            file=bindir/name; file.write_text(f'#!/bin/sh\nexec "$UPGRADE_TEST_PYTHON" "$UPGRADE_TEST_DISPATCH" --mock {name} "$@"\n'); file.chmod(0o755)
        release=self.base/'openwrt_release'; release.write_text('OpenWrt 24.10-SNAPSHOT\n')
        source=(ROOT/'files/usr/libexec/hitwh-mwan-upgrade').read_text().replace('/etc/openwrt_release',str(release)).replace('/var/run/hitwh-mwan-management.lock',str(self.base/'lock'))
        self.script=self.base/'upgrade'; self.script.write_text(source)
        self.env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH'],'UPGRADE_TEST_STATE':str(self.state_file),
                  'UPGRADE_TEST_PYTHON':sys.executable,'UPGRADE_TEST_DISPATCH':str(Path(__file__).resolve())}

    def make_release(self):
        control=''.join(f'{k}: {v}\n' for k,v in self.control.items()).encode()
        content=builder.archive([('debian-binary',b'2.0\n',0o644),('control.tar.gz',builder.archive([('control',control,0o644)]),0o644),('data.tar.gz',builder.archive([]),0o644)])
        ipk=self.base/'release.ipk'; ipk.write_bytes(content)
        name=f'luci-app-hitwh-mwan_{builder.VERSION}_all.ipk'
        sums=self.base/'SHA256SUMS'; sums.write_text(hashlib.sha256(content).hexdigest()+'  '+name+'\n')
        self.state.update(ipk=str(ipk),sums=str(sums),release={'tag_name':'v'+builder.VERSION,'draft':False,'prerelease':False,
            'assets':[{'name':name,'digest':'sha256:'+hashlib.sha256(content).hexdigest()},
                      {'name':'SHA256SUMS','digest':'sha256:'+hashlib.sha256(sums.read_bytes()).hexdigest()}]})

    def run_upgrade(self,expected=0):
        self.state_file.write_text(json.dumps(self.state))
        result=subprocess.run(['sh',str(self.script)],env=self.env,capture_output=True,text=True,timeout=20)
        self.state=json.loads(self.state_file.read_text())
        self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
        if 'UPGRADE_RESULT installed' in result.stdout:
            deadline=time.monotonic()+2
            while not self.state.get('restart_scheduled') and time.monotonic()<deadline:
                time.sleep(.02); self.state=json.loads(self.state_file.read_text())
        return result.stdout

    def test_verified_compatible_release_is_installed_without_authentication(self):
        self.assertIn('UPGRADE_RESULT installed',self.run_upgrade())
        self.assertEqual(self.state['installs'],1); self.assertEqual(self.state['preflight'],1)
        self.assertEqual(self.state['install_flag'],'1'); self.assertTrue(self.state['restart_scheduled'])
        for request in self.state['requests']:
            self.assertIn('--proto',request); self.assertIn('--proto-redir',request); self.assertNotIn('-k',request)

    def test_current_version_only_checks_and_does_not_download_or_install(self):
        self.state['current']=builder.VERSION
        self.assertIn('no_update',self.run_upgrade())
        self.assertEqual(len(self.state['requests']),1); self.assertEqual(self.state['installs'],0)

    def test_tampered_archive_is_rejected_before_opkg(self):
        self.state['tampered']=True
        self.assertIn('verification_failed',self.run_upgrade(1)); self.assertEqual(self.state['preflight'],0)

    def test_incompatible_control_is_rejected_even_with_valid_hashes(self):
        for key,value in [('Package','other-package'),('Architecture','x86_64'),('X-OpenWrt-Series','25.12')]:
            with self.subTest(key=key):
                original=self.control[key]; self.control[key]=value; self.make_release()
                self.assertIn('incompatible_package',self.run_upgrade(1)); self.assertEqual(self.state['installs'],0)
                self.control[key]=original

    def test_space_network_and_dependency_failures_never_install(self):
        for key,value,reason in [('free',100,'insufficient_space'),('network_error',True,'check_failed'),('dependency_failure',True,'dependency_failed')]:
            with self.subTest(key=key):
                self.state[key]=value
                self.assertIn(reason,self.run_upgrade(1)); self.assertEqual(self.state['installs'],0)
                self.state.pop(key)

    def test_invalid_tag_and_missing_asset_digest_never_install(self):
        self.state['release']['tag_name']='vbad;command'
        self.assertIn('invalid_release',self.run_upgrade(1))
        self.make_release(); self.state['release']['assets'][0]['digest']=''
        self.assertIn('invalid_release',self.run_upgrade(1)); self.assertEqual(self.state['installs'],0)

    def test_shared_management_lock_prevents_install_during_another_operation(self):
        import fcntl
        with (self.base/'lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertIn('busy',self.run_upgrade(1)); self.assertEqual(self.state['requests'],[])

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--mock': dispatch()
    else: unittest.main()
