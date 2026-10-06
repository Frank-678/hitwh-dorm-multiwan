"""Run the actual manager against isolated UCI, DHCP and interface fixtures."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def mock_command():
    state_path = Path(os.environ['EDIT_MOCK_STATE'])
    state = json.loads(state_path.read_text())
    name, *args = sys.argv[2:]
    output = ''
    result = 0
    change = False
    if name == 'uci':
        args = [a for a in args if a != '-q']
        action, *rest = args
        if action == 'get':
            output = state['config'].get(rest[0], '')
            result = 0 if output else 1
        elif action == 'show':
            output = '\n'.join(f'{key}={value if key.count(".") == 1 else chr(39) + value + chr(39)}'
                               for key, value in state['config'].items() if key.startswith(rest[0] + '.'))
        elif action == 'set':
            key, value = rest[0].split('=', 1)
            if state.get('fail_set') == key: raise SystemExit(1)
            state['config'][key] = value
            change = True
        elif action == 'delete':
            key = rest[0]
            state['config'] = {k: v for k, v in state['config'].items() if k != key and not k.startswith(key + '.')}
            change = True
        elif action in ('add_list', 'del_list'):
            key, value = rest[0].split('=', 1)
            values = state['config'].get(key, '').split()
            values = [v for v in values if v != value]
            if action == 'add_list': values.append(value)
            state['config'][key] = ' '.join(values)
            change = True
        elif action == 'commit':
            change = True
        else:
            raise AssertionError(args)
    elif name in ('ifdown', 'ifup'):
        iface = args[0]
        state['up'][iface] = name == 'ifup'
        if name == 'ifup': state['ips'][iface] = state['renewed_ips'].get(iface, None if state.get('no_new_dhcp') else '10.0.0.44')
        change = True
    elif name == 'network':
        if args != ['reload']: raise AssertionError(args)
        # Model netifd's device-level MAC taking precedence over an interface
        # option. A config-only fixture would miss this real-router failure.
        if not state.get('ignore_mac_apply'):
            config = state['config']
            main = config['hitwh_mwan.main.main_interface']
            parent = config['hitwh_mwan.main.parent_device']
            for iface in [key.split('.')[1] for key, value in config.items() if key.count('.') == 1 and value == 'interface']:
                dev = config.get(f'network.{iface}.device', parent if iface == main else '')
                device_sections = [key for key, value in config.items()
                                   if value == 'device' and config.get(key + '.name') == dev]
                mac = next((config.get(key + '.macaddr') for key in device_sections if config.get(key + '.macaddr')), '')
                mac = mac or config.get(f'network.{iface}.macaddr', '')
                if dev and mac:
                    address = Path(os.environ['EDIT_MOCK_ROOT']) / 'sys' / dev / 'address'
                    address.parent.mkdir(parents=True, exist_ok=True)
                    address.write_text(mac + '\n')
        change = True
    elif name == 'ubus':
        iface = args[1].split('.')[-1]
        address = state['ips'].get(iface) if state['up'].get(iface) else None
        output = json.dumps({'ipv4-address': [{'address': address}]} if address else {})
    elif name == 'jsonfilter':
        data = json.loads(Path(args[args.index('-i') + 1]).read_text()) if '-i' in args else json.load(sys.stdin)
        output = data.get('mac', '') if '@.mac' in args else data.get('ipv4-address', [{}])[0].get('address', '')
    elif name == 'update':
        if state.get('update_failure'): raise SystemExit(1)
        rows = []
        for iface, ip in state['ips'].items():
            if not state['up'][iface] or not ip: continue
            healthy = state['healthy'].get(iface, True)
            rows.append(f'{iface} ip={ip} state={"active" if healthy else "inactive"}')
        Path(os.environ['HITWH_MWAN_STATUS']).write_text('\n'.join(rows) + '\n')
        change = True
    elif name == 'sleep':
        # Keep only the global watchdog real; DHCP/updater waits are simulated.
        if args == ['270']:
            import time
            time.sleep(270)
    elif name == 'auth':
        directory = Path(os.environ['HITWH_MWAN_AUTH_DIR'])
        if args[0] in ('prepare','prepare-import'):
            if state.get('cancel_input'): raise SystemExit(1)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            pending = Path(tempfile.mkdtemp(prefix='.pending.', dir=directory))
            value = pending / 'value.json'
            values = json.load(sys.stdin) if args[0] == 'prepare-import' else {'username': 'fake-user', 'password': 'fake-secret &账'}
            value.write_text(json.dumps(values))
            value.chmod(0o600)
            output = str(pending)
        elif args[0] == 'import':
            if state.get('fail_import'): raise SystemExit(1)
            data = json.load(sys.stdin)
            data['mac'] = state['config'][f'network.{args[1]}dev.macaddr']
            file = directory / (args[1] + '.json')
            file.write_text(json.dumps(data)); file.chmod(0o600)
            output = f'AUTH_RESULT {args[1]} saved'
        elif args[0] == 'remove':
            (directory / (args[1] + '.json')).unlink(missing_ok=True)
            output = f'AUTH_RESULT {args[1]} removed'
        elif args[0] == 'login':
            if state.get('block_auth'):
                import time
                (Path(os.environ['EDIT_MOCK_ROOT']) / 'auth.running').write_text(str(os.getpid()))
                time.sleep(120)
            result = 1 if state.get('auth_failure') else 0
            state['healthy'][args[1]] = not result
            output = f'AUTH_RESULT {args[1]} {"authentication_failed" if result else "success"}'
        else: raise AssertionError(args)
        change = True
    elif name == 'id':
        output = '0'
    elif name == 'stat':
        output = f'0:{stat.S_IMODE(Path(args[-1]).stat().st_mode):o}'
    elif name == 'dd':
        state['random_calls'] = state.get('random_calls', 0) + 1
        state_path.write_text(json.dumps(state))
        if state.get('random_failure'): raise SystemExit(1)
        candidates = state.get('random_candidates', ['10:20:30:40:50'])
        sys.stdout.buffer.write(bytes.fromhex(candidates[min(state['random_calls'] - 1, len(candidates) - 1)].replace(':', '')))
        raise SystemExit(0)
    elif name == 'hexdump':
        if '/dev/urandom' in args:
            state['random_calls'] = state.get('random_calls', 0) + 1
            state_path.write_text(json.dumps(state))
            if state.get('random_failure'): raise SystemExit(1)
            candidates = state.get('random_candidates', ['10:20:30:40:50'])
            output = candidates[min(state['random_calls'] - 1, len(candidates) - 1)] + ':'
        else:
            output = ''.join(f'{byte:02x}:' for byte in sys.stdin.buffer.read())
    elif name == 'firewall':
        change = True
    else:
        raise AssertionError(name)
    if change:
        state['events'].append([name, *args])
        state_path.write_text(json.dumps(state))
    if output: sys.stdout.buffer.write((output + '\n').encode('utf-8'))
    raise SystemExit(result)


class EditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / 'state.json'
        config = {
            'hitwh_mwan.main.max_paths': '3',
            'hitwh_mwan.main.main_interface': 'wan',
            'hitwh_mwan.main.parent_device': 'eth1',
            'network.wan': 'interface',
            'network.wan.device': 'eth1',
            'network.wan.macaddr': '02:00:00:00:00:01',
            'firewall.wan': 'zone', 'firewall.wan.name': 'wan', 'firewall.wan.network': 'wan wan2 wan3',
        }
        for slot in (2, 3):
            config.update({
                f'network.wan{slot}': 'interface',
                f'network.wan{slot}dev': 'device',
                f'network.wan{slot}dev.name': f'macwan{slot}',
                f'network.wan{slot}dev.type': 'macvlan',
                f'network.wan{slot}dev.ifname': 'eth1',
                f'network.wan{slot}dev.macaddr': f'02:00:00:00:00:0{slot}',
                f'network.wan{slot}.device': f'macwan{slot}',
                f'network.wan{slot}.proto': 'dhcp',
                f'network.wan{slot}.metric': str(slot * 10),
            })
        self.state = {
            'config': config,
            'up': {iface: True for iface in ('wan', 'wan2', 'wan3')},
            'ips': {'wan': '10.0.0.1', 'wan2': '10.0.0.2', 'wan3': '10.0.0.3'},
            'renewed_ips': {'wan': '10.0.0.11', 'wan2': '10.0.0.22', 'wan3': '10.0.0.33'},
            'healthy': {}, 'events': [],
        }
        self.save()
        (self.root / 'network').write_text('original network\n')
        (self.root / 'module/macvlan').mkdir(parents=True)
        physical = self.root / 'sys/eth1'
        physical.mkdir(parents=True)
        (physical / 'address').write_text('02:00:00:00:00:01\n')
        for slot in (2, 3):
            device = self.root / 'sys' / f'macwan{slot}'
            device.mkdir(parents=True)
            (device / 'address').write_text(f'02:00:00:00:00:0{slot}\n')
        fixture = self.root.as_posix()
        source = (ROOT / 'files/usr/sbin/hitwh-mwan').read_text()
        for old, new in (
            ('/var/run/', fixture + '/'),
            ('/root/hitwh-mwan-backups', fixture + '/backups'),
            ('/etc/config/network', fixture + '/network'),
            ('/etc/init.d/network', f'"{fixture}/bin/network"'),
            ('/etc/init.d/firewall', f'"{fixture}/bin/firewall"'),
            ('/sys/class/net', fixture + '/sys'),
            ('/sys/module/macvlan', fixture + '/module/macvlan'),
        ):
            source = source.replace(old, new)
        self.script = self.root / 'manager.sh'
        with self.script.open('w', newline='\n') as file:
            file.write(source)
        bindir = self.root / 'bin'
        bindir.mkdir()
        for name in ('uci', 'ubus', 'jsonfilter', 'ifdown', 'ifup', 'network', 'update', 'sleep', 'auth', 'id', 'stat', 'dd', 'hexdump', 'firewall'):
            file = bindir / name
            with file.open('w', newline='\n') as command_file:
                command_file.write(f'#!/bin/sh\nexec "$EDIT_MOCK_PYTHON" "$EDIT_MOCK_DISPATCH" --mock {name} "$@"\n')
            file.chmod(0o755)
        shell = shutil.which('sh')
        if not shell and os.name == 'nt': shell = str(Path(os.environ['ProgramFiles']) / 'Git/bin/bash.exe')
        if not shell: self.skipTest('POSIX shell required')
        bin_path = bindir.as_posix()
        if os.name == 'nt': bin_path = '/' + bin_path[0].lower() + bin_path[2:]
        self.env = {
            **os.environ,
            'EDIT_MOCK_STATE': self.state_path.as_posix(),
            'EDIT_MOCK_ROOT': self.root.as_posix(),
            'EDIT_MOCK_PYTHON': Path(sys.executable).as_posix(),
            'EDIT_MOCK_DISPATCH': Path(__file__).as_posix(),
            'EDIT_MOCK_BIN': bin_path,
            'EDIT_MOCK_SCRIPT': self.script.as_posix(),
            'HITWH_MWAN_UPDATE': (bindir / 'update').as_posix(),
            'HITWH_MWAN_STATUS': (self.root / 'status').as_posix(),
            'HITWH_MWAN_AUTH': (bindir / 'auth').as_posix(),
            'HITWH_MWAN_AUTH_DIR': (self.root / 'private/auth.d').as_posix(),
        }
        self.command = [shell, '-c', 'export PATH="$EDIT_MOCK_BIN:$PATH"; exec sh "$EDIT_MOCK_SCRIPT" "$@"', 'manager']

    def save(self): self.state_path.write_text(json.dumps(self.state))

    def run_manager(self, *args, expected=0):
        result = subprocess.run([*self.command, *args], env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.state = json.loads(self.state_path.read_text())
        return result.stdout + result.stderr

    def assert_no_changes(self):
        self.assertEqual(self.state['events'], [])
        self.assertFalse((self.root / 'backups').exists())

    def test_refresh_authenticates_only_offline_and_does_not_restart_addressed_paths(self):
        self.state['healthy']['wan2'] = False
        self.save()
        self.assertIn('RESULT 1 1 0', self.run_manager('refresh'))
        self.assertEqual([e for e in self.state['events'] if e[0] == 'auth'], [['auth', 'login', 'wan2']])
        self.assertFalse(any(e[0] in ('ifdown', 'ifup') for e in self.state['events']))

    def test_refresh_recovers_dhcp_before_authenticating(self):
        self.state['ips']['wan2'] = None
        self.state['healthy']['wan2'] = False
        self.save()
        self.assertIn('RESULT 1 1 0', self.run_manager('refresh'))
        self.assertEqual([e for e in self.state['events'] if e[0] in ('ifdown', 'ifup', 'auth')],
                         [['ifdown', 'wan2'], ['ifup', 'wan2'], ['auth', 'login', 'wan2']])

    def test_all_online_and_list_never_authenticate(self):
        self.assertIn('RESULT 0 0 0', self.run_manager('refresh'))
        self.state['healthy']['wan2'] = False
        self.save()
        self.run_manager('list')
        self.assertFalse(any(e[0] == 'auth' for e in self.state['events']))

    @unittest.skipUnless(os.name == 'posix', 'POSIX signal and lock behavior')
    def test_cancel_refresh_terminates_authentication_and_releases_lock(self):
        import time
        self.state['healthy']['wan2'] = False
        self.state['block_auth'] = True; self.save()
        process = subprocess.Popen([*self.command, 'refresh'], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        marker = self.root / 'auth.running'
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline: time.sleep(0.02)
        self.assertTrue(marker.exists())
        auth_pid = int(marker.read_text())
        process.terminate()
        process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143)
        with self.assertRaises(ProcessLookupError): os.kill(auth_pid, 0)
        self.state = json.loads(self.state_path.read_text())
        self.state['block_auth'] = False
        self.state['healthy']['wan2'] = True; self.save()
        self.assertIn('RESULT 0 0 0', self.run_manager('refresh'))

    def test_remove_always_protects_reserved_wan6(self):
        self.state['config']['hitwh_mwan.main.max_paths'] = '7'
        for key, value in list(self.state['config'].items()):
            if key.startswith('network.wan2'):
                self.state['config'][key.replace('wan2', 'wan6')] = value.replace('macwan2', 'macwan6')
        self.save()
        self.run_manager('remove', 'wan6', expected=4)
        self.assert_no_changes()

    def test_refresh_does_not_authenticate_without_a_successful_route_update(self):
        self.state['healthy']['wan2'] = False
        self.state['update_failure'] = True; self.save()
        self.run_manager('refresh', expected=1)
        self.assertFalse(any(e[0] in ('auth', 'ifdown', 'ifup') for e in self.state['events']))
        self.state['update_failure'] = False; self.save()
        (self.root / 'hitwh-mwan-update.lock').mkdir()
        self.run_manager('refresh', expected=1)
        self.assertFalse(any(e[0] == 'auth' for e in self.state['events']))

    def test_single_refresh_authenticates_only_selected_roommate_path(self):
        self.state['healthy'].update(wan2=False,wan3=False); self.save()
        self.assertIn('RESULT 1 1 0',self.run_manager('refresh','wan2'))
        self.assertEqual([e for e in self.state['events'] if e[0]=='auth'],[['auth','login','wan2']])
        self.assertFalse(self.state['healthy']['wan3'])

    def test_refresh_rejects_nonmanaged_target_before_any_update(self):
        self.run_manager('refresh','wan6',expected=2)
        self.assertFalse(self.state['events'])

    def test_unenrolled_physical_wan_is_not_authenticated(self):
        self.state['config']['hitwh_mwan.main.main_enrolled']='0'
        self.state['healthy']['wan']=False; self.save()
        self.assertIn('RESULT 0 0 0',self.run_manager('refresh'))
        self.assertFalse(any(e[0]=='auth' for e in self.state['events']))


    def test_edit_subwan_preserves_other_configuration_and_reconnects_only_target(self):
        original = self.state['config'].copy()
        output = self.run_manager('edit', 'wan2', '02:AA:BB:CC:DD:22')
        original['network.wan2dev.macaddr'] = '02:aa:bb:cc:dd:22'
        self.assertEqual(self.state['config'], original)
        self.assertEqual([e for e in self.state['events'] if e[0] in ('ifdown', 'ifup')], [['ifdown', 'wan2'], ['ifup', 'wan2']])
        self.assertEqual([e[0] for e in self.state['events']], ['uci', 'uci', 'ifdown', 'update', 'network', 'ifup', 'update'])
        self.assertIn('IP=10.0.0.22 state=active', output)
        backups = list((self.root / 'backups').glob('network-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'original network\n')

    def test_edit_main_uses_main_interface_mac_option(self):
        self.run_manager('edit', 'wan', '02:aa:bb:cc:dd:11')
        self.assertEqual(self.state['config']['network.wan.macaddr'], '02:aa:bb:cc:dd:11')
        self.assertEqual(self.state['config']['network.hitwh_main_device'], 'device')
        self.assertEqual(self.state['config']['network.hitwh_main_device.name'], 'eth1')
        self.assertEqual(self.state['config']['network.hitwh_main_device.macaddr'], '02:aa:bb:cc:dd:11')
        self.assertEqual((self.root / 'sys/eth1/address').read_text().strip(), '02:aa:bb:cc:dd:11')
        self.assertEqual([e for e in self.state['events'] if e[0] in ('ifdown', 'ifup')], [['ifdown', 'wan'], ['ifup', 'wan']])

    def test_edit_numeric_slot(self):
        self.run_manager('edit', '2', '02:aa:bb:cc:dd:22')
        self.assertEqual(self.state['config']['network.wan2dev.macaddr'], '02:aa:bb:cc:dd:22')

    def test_edit_main_aliases_do_not_restart_when_mac_is_unchanged(self):
        for alias in ('1', 'wan1'):
            with self.subTest(alias=alias):
                self.assertIn('No changes', self.run_manager('edit', alias, '02:00:00:00:00:01'))
                self.assert_no_changes()

    def test_edit_by_old_subwan_mac(self):
        self.run_manager('edit', '02:00:00:00:00:02', '02:aa:bb:cc:dd:22')
        self.assertEqual(self.state['config']['network.wan2dev.macaddr'], '02:aa:bb:cc:dd:22')

    def test_edit_same_mac_is_case_insensitive_and_has_no_side_effects(self):
        self.state['config']['network.wan2dev.macaddr'] = '02:aa:bb:cc:dd:22'
        (self.root / 'sys/macwan2/address').write_text('02:aa:bb:cc:dd:22\n')
        self.save()
        self.assertIn('No changes', self.run_manager('edit', 'wan2', '02:AA:BB:CC:DD:22'))
        self.assert_no_changes()

    def test_edit_rejects_malformed_zero_and_multicast_macs(self):
        for mac in ('bad', '020000000022', '00:00:00:00:00:00', '01:00:00:00:00:01', 'ff:ff:ff:ff:ff:ff'):
            with self.subTest(mac=mac):
                self.run_manager('edit', 'wan2', mac, expected=2)
                self.assert_no_changes()

    def test_edit_rejects_main_mac_on_subwan(self):
        self.assertIn('already used by wan', self.run_manager('edit', 'wan2', '02:00:00:00:00:01', expected=3))
        self.assert_no_changes()

    def test_edit_rejects_other_subwan_mac(self):
        self.assertIn('already used by wan3', self.run_manager('edit', 'wan2', '02:00:00:00:00:03', expected=3))
        self.assert_no_changes()

    def test_set_main_also_rejects_subwan_mac_conflicts(self):
        self.run_manager('set-main', '02:00:00:00:00:02', expected=3)
        self.assert_no_changes()

    def test_set_main_remains_supported(self):
        self.run_manager('set-main', '02:aa:bb:cc:dd:11')
        self.assertEqual(self.state['config']['network.wan.macaddr'], '02:aa:bb:cc:dd:11')

    def test_edit_rejects_invalid_identifiers_before_mutation(self):
        for iface in ('wan0', 'wan02', 'wan4', 'lan', 'wan2dev.macaddr'):
            with self.subTest(iface=iface):
                self.run_manager('edit', iface, '02:aa:bb:cc:dd:22', expected=2)
                self.assert_no_changes()

    def test_edit_rejects_missing_interface(self):
        del self.state['config']['network.wan2']
        self.save()
        self.assertIn('does not exist', self.run_manager('edit', 'wan2', '02:aa:bb:cc:dd:22', expected=3))
        self.assert_no_changes()

    def test_edit_rejects_nonmanaged_interface(self):
        self.state['config']['network.wan2dev.ifname'] = 'another-port'
        self.save()
        self.run_manager('edit', 'wan2', '02:aa:bb:cc:dd:22', expected=4)
        self.assert_no_changes()

    def test_edit_protects_reserved_wan6(self):
        self.state['config']['hitwh_mwan.main.max_paths'] = '17'
        self.state['config']['network.wan6'] = 'interface'
        self.save()
        self.run_manager('edit', 'wan6', '02:aa:bb:cc:dd:66', expected=4)
        self.assert_no_changes()

    def test_edit_reports_dhcp_timeout_after_saving_mac(self):
        self.state['renewed_ips']['wan2'] = ''
        self.save()
        self.assertIn('state=no-dhcp', self.run_manager('edit', 'wan2', '02:aa:bb:cc:dd:22'))
        self.assertEqual(self.state['config']['network.wan2dev.macaddr'], '02:aa:bb:cc:dd:22')

    def test_edit_reports_unauthenticated_replacement(self):
        self.state['healthy']['wan2'] = False
        self.save()
        self.assertIn('state=inactive', self.run_manager('edit', 'wan2', '02:aa:bb:cc:dd:22'))

    def test_edit_requires_exactly_interface_and_mac_arguments(self):
        for args in (('edit',), ('edit', 'wan2'), ('edit', 'wan2', '02:aa:bb:cc:dd:22', 'extra')):
            with self.subTest(args=args):
                self.run_manager(*args, expected=1)
                self.assert_no_changes()

    def test_edit_handles_configured_main_interface_name(self):
        self.state['config']['hitwh_mwan.main.main_interface'] = 'uplink'
        for key in ('network.wan', 'network.wan.macaddr', 'network.wan.device'):
            self.state['config'][key.replace('wan', 'uplink')] = self.state['config'].pop(key)
        for field in ('up', 'ips', 'renewed_ips'):
            self.state[field]['uplink'] = self.state[field].pop('wan')
        self.save()
        self.run_manager('edit', 'uplink', '02:aa:bb:cc:dd:11')
        self.assertEqual(self.state['config']['network.uplink.macaddr'], '02:aa:bb:cc:dd:11')

    def test_edit_rejects_physical_main_mac_when_no_override_is_configured(self):
        del self.state['config']['network.wan.macaddr']
        self.save()
        self.run_manager('edit', 'wan2', '02:00:00:00:00:01', expected=3)
        self.assert_no_changes()

    def test_edit_waits_for_updater_and_refuses_persistent_busy_lock(self):
        (self.root / 'hitwh-mwan-update.lock').mkdir()
        self.assertIn('updater is busy', self.run_manager('edit', 'wan2', '02:aa:bb:cc:dd:22', expected=1))
        self.assert_no_changes()

    def configure_physical_device(self, section='network.wanphys', mac='02:00:00:00:00:01'):
        self.state['config'].update({section: 'device', section + '.name': 'eth1', section + '.macaddr': mac})
        self.save()

    def test_edit_main_repairs_interface_only_change_overridden_by_wanphys(self):
        target = '02:aa:bb:cc:dd:11'
        self.state['config']['network.wan.macaddr'] = target
        self.configure_physical_device()
        self.assertIn('Updated wan:', self.run_manager('edit', 'wan', target))
        self.assertEqual(self.state['config']['network.wanphys.macaddr'], target)
        self.assertEqual((self.root / 'sys/eth1/address').read_text().strip(), target)
        self.assertNotIn('network.hitwh_main_device', self.state['config'])

    def test_edit_main_updates_anonymous_device_and_preserves_other_options(self):
        section = 'network.@device[0]'
        self.configure_physical_device(section)
        self.state['config'][section + '.mtu'] = '1500'
        self.save()
        self.run_manager('edit', 'wan', '02:aa:bb:cc:dd:11')
        self.assertEqual(self.state['config'][section + '.macaddr'], '02:aa:bb:cc:dd:11')
        self.assertEqual(self.state['config'][section + '.mtu'], '1500')
        self.assertEqual((self.root / 'sys/eth1/address').read_text().strip(), '02:aa:bb:cc:dd:11')

    def test_edit_reapplies_matching_config_when_runtime_mac_is_stale(self):
        target = '02:aa:bb:cc:dd:11'
        self.state['config']['network.wan.macaddr'] = target
        self.configure_physical_device(mac=target)
        self.run_manager('edit', 'wan', target)
        self.assertIn(['ifdown', 'wan'], self.state['events'])
        self.assertEqual((self.root / 'sys/eth1/address').read_text().strip(), target)

    def test_edit_does_not_report_success_if_runtime_mac_did_not_change(self):
        self.configure_physical_device()
        self.state['ignore_mac_apply'] = True
        self.save()
        output = self.run_manager('edit', 'wan', '02:aa:bb:cc:dd:11', expected=1)
        self.assertIn('MAC change was not applied', output)
        self.assertNotIn('Updated wan:', output)
        self.assertEqual((self.root / 'sys/eth1/address').read_text().strip(), '02:00:00:00:00:01')

    def test_edit_rejects_current_physical_main_mac_even_with_different_interface_config(self):
        self.state['config']['network.wan.macaddr'] = '02:aa:bb:cc:dd:11'
        self.save()
        self.run_manager('edit', 'wan2', '02:00:00:00:00:01', expected=3)
        self.assert_no_changes()

    def test_edit_rejects_device_level_main_mac_on_subwan(self):
        self.configure_physical_device(mac='02:aa:bb:cc:dd:11')
        self.run_manager('edit', 'wan2', '02:aa:bb:cc:dd:11', expected=3)
        self.assert_no_changes()

    def test_add_also_rejects_device_level_main_mac(self):
        self.configure_physical_device(mac='02:aa:bb:cc:dd:11')
        self.run_manager('add', '02:aa:bb:cc:dd:11', expected=3)
        self.assert_no_changes()

    def test_edit_subwan_reapplies_configured_mac_when_runtime_is_stale(self):
        self.state['config']['network.wan2dev.macaddr'] = '02:aa:bb:cc:dd:22'
        self.save()
        self.run_manager('edit', 'wan2', '02:aa:bb:cc:dd:22')
        self.assertEqual((self.root / 'sys/macwan2/address').read_text().strip(), '02:aa:bb:cc:dd:22')

    def test_edit_subwan_syncs_an_existing_interface_override(self):
        self.state['config']['network.wan2.macaddr'] = '02:00:00:00:00:02'
        self.save()
        self.run_manager('edit', 'wan2', '02:aa:bb:cc:dd:22')
        self.assertEqual(self.state['config']['network.wan2.macaddr'], '02:aa:bb:cc:dd:22')
        self.assertEqual((self.root / 'sys/macwan2/address').read_text().strip(), '02:aa:bb:cc:dd:22')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--mock': mock_command()
    unittest.main()
