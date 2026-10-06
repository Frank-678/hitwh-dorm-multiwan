"""Exercise the real update shell script with isolated network command fixtures."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def rewrite_runtime_paths(source, fixture):
    # Replace original paths once: the fixture itself may start with /tmp/.
    return re.sub(r'/var/run/|/tmp/', lambda match: fixture + '/', source)


class RuntimePathTests(unittest.TestCase):
    def test_fixture_paths_are_not_replaced_again(self):
        source = 'LOCK=/var/run/update.lock\nCHECKDIR="/tmp/check.$$"\n'
        for fixture in ('/tmp/example', 'C:/Users/example/AppData/Local/Temp/example'):
            with self.subTest(fixture=fixture):
                expected = f'LOCK={fixture}/update.lock\nCHECKDIR="{fixture}/check.$$"\n'
                self.assertEqual(rewrite_runtime_paths(source, fixture), expected)


def mock_command():
    state_path = Path(os.environ['MOCK_STATE'])
    state = json.loads(state_path.read_text())
    name, *args = sys.argv[2:]
    result = 0
    output = ''
    change = False
    if name == 'uci':
        key = args[-1]
        values = {'hitwh_mwan.main.max_paths': '2', **state.get('config', {})}
        if key in values:
            output = values[key]
            result = 0 if output else 1
        elif key.startswith('network.'):
            section, _, option = key[8:].partition('.')
            iface = section.removesuffix('dev')
            path = state['paths'].get(iface)
            if not path: result = 1
            elif not option: output = 'device' if section.endswith('dev') else 'interface'
            else: output = {'type':'macvlan','ifname':'eth1','device':path['dev'],'proto':'dhcp'}.get(option,'')
        else:
            output = values.get(key, '')
            result = 0 if output else 1
    elif name == 'ubus':
        iface = args[1].split('.')[-1]
        path = state['paths'].get(iface)
        output = json.dumps({'l3_device': path['dev'], 'ipv4-address': [{'address': path['ip'], 'mask': path.get('mask',16)}], 'route': [{'nexthop': path.get('gateway',state['gateway'])}], 'dns-server': path.get('dns', []), 'inactive': {'dns-server': path.get('inactive_dns', [])}}) if path else '{}'
    elif name == 'jsonfilter':
        data = json.load(sys.stdin)
        rows = []
        for index, arg in enumerate(args):
            if arg != '-e': continue
            expression = args[index + 1]
            if expression == '@.l3_device': rows.append(data.get('l3_device', ''))
            elif 'ipv4-address' in expression: rows.append(str(data.get('ipv4-address', [{}])[0].get('mask' if '.mask' in expression else 'address', '')))
            elif expression == '@.route[0].nexthop': rows.append(data.get('route', [{}])[0].get('nexthop', ''))
            elif expression == '@["dns-server"][*]': rows.extend(data.get('dns-server', []))
            elif expression == '@.inactive["dns-server"][*]': rows.extend(data.get('inactive', {}).get('dns-server', []))
        output = '\n'.join(rows)
    elif name == 'curl':
        source = args[args.index('--interface') + 1]
        dns_ok = True
        if state.get('require_path_dns'):
            dnsfile = Path(state['config']['dhcp.@dnsmasq[0].serversfile'])
            contents = dnsfile.read_text() if dnsfile.exists() else ''
            dns_ok = any('@' + path['ip'] in contents and path['ip'] not in state['unhealthy'] for path in state['paths'].values())
        output = '000' if not dns_ok else ('500' if source in state['unhealthy'] else '204')
    elif name == 'killall':
        if args != ['-HUP', 'dnsmasq']: raise AssertionError(args)
        change = True
    elif name == 'logger':
        pass
    elif name == 'nft':
        if '-f' in args:
            change = True
            if state.get('nft_failure'): result = 1
            else: state['chain'] = Path(args[-1]).read_text()
        elif 'chain' in args:
            output = state['chain']
    elif name == 'ip':
        args = [a for a in args if a != '-4']
        kind, action, *rest = args
        if kind == 'rule':
            if action == 'show':
                output = '\n'.join(f'{p}:\t{s}' for p, s in sorted(state['rules']))
            else:
                change = True
                priority = int(rest[1])
                selector = ' '.join(rest[2:])
                entry = [priority, selector]
                if action == 'add': state['rules'].append(entry)
                elif not selector:
                    entry = next((r for r in state['rules'] if r[0] == priority), None)
                    if entry: state['rules'].remove(entry)
                    else: result = 1
                elif entry in state['rules']: state['rules'].remove(entry)
                else: result = 1
        elif kind == 'route':
            table = int(rest[rest.index('table') + 1])
            if action == 'show':
                prefix = rest[rest.index('exact') + 1]
                rows = []
                for row in state['routes']:
                    if row['table'] != table or row['prefix'] != prefix: continue
                    text = 'unreachable default' if row.get('guard') else row['prefix'].replace('/32', '')
                    for field in ('via', 'dev', 'src', 'metric'):
                        if row.get(field): text += f" {field} {row[field]}"
                    rows.append(text)
                output = '\n'.join(rows)
            elif action == 'replace':
                change = True
                prefix = 'default' if 'default' in rest else rest[2]
                row = {'table': table, 'prefix': prefix, 'guard': 'unreachable' in rest}
                for field in ('via', 'dev', 'src', 'metric'):
                    if field in rest: row[field] = rest[rest.index(field) + 1]
                state['routes'] = [r for r in state['routes'] if (r['table'], r['prefix'], r.get('metric') or '0') != (table, prefix, row.get('metric') or '0')]
                state['routes'].append(row)
            else:
                raise AssertionError(f'Unexpected destructive route command: {args}')
    else:
        raise AssertionError(name)
    if change:
        state['changes'].append([name, *sys.argv[3:]])
        state_path.write_text(json.dumps(state))
    if output: sys.stdout.buffer.write((output + ('' if name == 'curl' else '\n')).encode('utf-8'))
    raise SystemExit(result)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / 'state.json'
        self.state = {'gateway': '10.0.0.1', 'paths': {'wan': {'dev': 'eth1', 'ip': '10.0.0.2'}, 'wan2': {'dev': 'macwan2', 'ip': '10.0.0.3'}}, 'unhealthy': [], 'routes': [], 'rules': [], 'chain': 'initial chain\n', 'changes': []}
        self.save()
        fixture = self.root.as_posix()
        source = (ROOT / 'files/usr/sbin/hitwh-mwan-update').read_text()
        self.script = self.root / 'update.sh'
        with self.script.open('w', newline='\n') as script_file:
            script_file.write(rewrite_runtime_paths(source, fixture))
        bindir = self.root / 'bin'
        bindir.mkdir()
        for name in ('uci', 'ubus', 'jsonfilter', 'curl', 'ip', 'nft', 'logger', 'killall'):
            file = bindir / name
            with file.open('w', newline='\n') as command_file:
                command_file.write(f'#!/bin/sh\nexec "$MOCK_PYTHON" "$MOCK_DISPATCH" --mock {name} "$@"\n')
            file.chmod(0o755)
        self.env = {**os.environ, 'MOCK_STATE': self.state_path.as_posix(), 'MOCK_DISPATCH': Path(__file__).as_posix(), 'MOCK_PYTHON': Path(sys.executable).as_posix()}
        self.shell = shutil.which('sh')
        if not self.shell and os.name == 'nt': self.shell = str(Path(os.environ['ProgramFiles']) / 'Git/bin/bash.exe')
        if not self.shell: self.skipTest('POSIX shell required')
        # Let Git Bash construct its normal utility PATH before prepending mocks.
        self.command = [self.shell, '-c', 'export PATH="$MOCK_BIN:$PATH"; exec sh "$MOCK_SCRIPT"']
        bin_path = bindir.as_posix()
        if os.name == 'nt': bin_path = '/' + bin_path[0].lower() + bin_path[2:]
        self.env.update(MOCK_BIN=bin_path, MOCK_SCRIPT=self.script.as_posix())

    def save(self): self.state_path.write_text(json.dumps(self.state))
    def load(self): self.state = json.loads(self.state_path.read_text()); return self.state

    def run_update(self, expected=0):
        result = subprocess.run(self.command, env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.load()

    def clear_changes(self): self.load(); self.state['changes'] = []; self.save()

    def test_unchanged_paths_do_not_mutate_routes_rules_or_nft(self):
        self.run_update()
        self.assertEqual(len(self.state['rules']), 6)
        self.clear_changes()
        self.run_update()
        self.assertEqual(self.state['changes'], [])

    def test_other_school_dhcp_prefix_and_gateway_are_used(self):
        self.state['paths']['wan2'].update(ip='192.168.44.27',mask=24,gateway='192.168.44.1'); self.save()
        self.run_update()
        routes=[r for r in self.state['routes'] if r['table']==102]
        self.assertTrue(any(r['prefix']=='192.168.44.0/24' for r in routes))
        self.assertTrue(any(r.get('via')=='192.168.44.1' for r in routes))

    def test_fresh_install_has_no_enrolled_paths_and_preserves_physical_interface(self):
        self.state['paths'].pop('wan2')
        self.state['config']={'hitwh_mwan.main.main_enrolled':'0'}; self.save()
        self.run_update()
        self.assertIn('active_count:0',(self.root/'hitwh-mwan.status').read_text())
        self.assertFalse(any(c[0]=='curl' for c in self.state['changes']))

    def test_disabling_main_withdraws_it_without_interrupting_virtual_path(self):
        self.run_update()
        self.state['config']={'hitwh_mwan.main.main_enrolled':'0'}; self.save()
        self.clear_changes(); self.run_update()
        self.assertIn('active_interfaces: wan2',(self.root/'hitwh-mwan.status').read_text())
        self.assertFalse(any(r['table']==101 and r.get('via') for r in self.state['routes']))
        self.assertTrue(any(r['table']==102 and r.get('via') for r in self.state['routes']))

    def test_address_change_replaces_only_its_path_and_adds_rule_first(self):
        self.run_update()
        self.clear_changes()
        self.state['paths']['wan2']['ip'] = '10.0.0.4'
        self.save()
        self.run_update()
        changes = self.state['changes']
        self.assertTrue(changes)
        self.assertTrue(all('101' not in c for c in changes))
        add = next(i for i, c in enumerate(changes) if c[0] == 'ip' and 'rule' in c and 'add' in c)
        delete = next(i for i, c in enumerate(changes) if c[0] == 'ip' and 'rule' in c and 'del' in c)
        self.assertLess(add, delete)
        self.assertIn([1202, 'from 10.0.0.4 lookup 102'], self.state['rules'])
        self.assertNotIn([1202, 'from 10.0.0.3 lookup 102'], self.state['rules'])

    def test_health_change_updates_only_nft_once(self):
        self.run_update()
        self.clear_changes()
        self.state['unhealthy'] = ['10.0.0.3']
        self.save()
        self.run_update()
        self.assertEqual([c[0] for c in self.state['changes']], ['nft'])
        self.clear_changes()
        self.run_update()
        self.assertEqual(self.state['changes'], [])

    def test_live_firewall_drift_is_repaired_despite_cache(self):
        self.run_update()
        self.clear_changes()
        self.state['chain'] = 'empty reloaded chain\n'
        self.save()
        self.run_update()
        self.assertEqual([c[0] for c in self.state['changes']], ['nft'])
        self.assertIn('numgen inc mod 2', self.state['chain'])

    def test_missing_interface_keeps_mark_rule_and_terminal_route(self):
        self.run_update()
        self.clear_changes()
        del self.state['paths']['wan2']
        self.state['routes'] = [r for r in self.state['routes'] if r['table'] != 102 or r['guard']]
        self.save()
        self.run_update()
        self.assertIn([1102, 'from all fwmark 0x102/0xfff lookup 102'], self.state['rules'])
        self.assertEqual([r for r in self.state['routes'] if r['table'] == 102], [{'table': 102, 'prefix': 'default', 'guard': True, 'metric': '42760'}])
        self.assertEqual([c[0] for c in self.state['changes']], ['nft'])

    def test_failed_nft_update_does_not_publish_new_cache(self):
        self.run_update()
        cache = (self.root / 'hitwh-mwan.rules').read_text()
        self.state['unhealthy'] = ['10.0.0.3']
        self.state['nft_failure'] = True
        self.save()
        self.run_update(expected=1)
        self.assertEqual((self.root / 'hitwh-mwan.rules').read_text(), cache)

    def test_round_robin_separates_tcp_udp_and_is_not_reset_on_refresh(self):
        self.run_update()
        self.assertIn('meta l4proto tcp ct mark set numgen inc mod 2', self.state['chain'])
        self.assertIn('meta l4proto udp ct mark set numgen inc mod 2', self.state['chain'])
        self.assertNotIn('numgen random', self.state['chain'])
        self.clear_changes()
        self.run_update()
        self.assertEqual(self.state['changes'], [])

    def test_switching_modes_changes_only_new_flow_rules(self):
        self.run_update()
        routes = self.state['routes'][:]
        rules = self.state['rules'][:]
        self.clear_changes()
        self.state['config'] = {'hitwh_mwan.main.balance_mode': 'random'}
        self.save()
        self.run_update()
        self.assertIn('numgen random mod 2', self.state['chain'])
        self.assertEqual(self.state['routes'], routes)
        self.assertEqual(self.state['rules'], rules)
        self.assertEqual([c[0] for c in self.state['changes']], ['nft'])
        self.assertIn('ct state established,related meta mark set ct mark', self.state['chain'])
        self.clear_changes()
        self.run_update()
        self.assertEqual(self.state['changes'], [])
        self.state['config']['hitwh_mwan.main.balance_mode'] = 'round_robin'
        self.save()
        self.run_update()
        self.assertIn('numgen inc mod 2', self.state['chain'])
        self.assertEqual(self.state['routes'], routes)
        self.assertEqual(self.state['rules'], rules)
        self.assertEqual([c[0] for c in self.state['changes']], ['nft'])

    def test_invalid_mode_fails_before_network_mutations(self):
        self.state['config'] = {'hitwh_mwan.main.balance_mode': 'invalid'}
        self.save()
        self.run_update(expected=2)
        self.assertEqual(self.state['changes'], [])

    def status(self):
        return (self.root / 'hitwh-mwan.status').read_text()

    def router_rule(self):
        return [r for r in self.state['rules'] if r[0] == 32001]

    def enable_dns(self):
        self.state['config'] = {'dhcp.@dnsmasq[0].serversfile': (self.root / 'hitwh-mwan.dns').as_posix()}
        self.state['paths']['wan']['dns'] = ['172.26.26.3']
        self.state['paths']['wan2']['inactive_dns'] = ['172.26.26.3', '219.146.1.66']
        self.state['require_path_dns'] = True
        self.save()

    def test_main_failure_switches_router_and_recovery_switches_back(self):
        self.run_update()
        original_routes = self.state['routes'][:]
        self.assertIn('router_interface:wan\n', self.status())
        self.clear_changes()
        self.state['unhealthy'] = ['10.0.0.2']
        self.save()
        self.run_update()
        self.assertEqual(self.router_rule(), [[32001, 'from all fwmark 0/0xfff iif lo lookup 102']])
        self.assertIn([32000, 'from all lookup main suppress_prefixlength 0'], self.state['rules'])
        self.assertIn('router_interface:wan2\n', self.status())
        self.assertEqual(self.state['routes'], original_routes)
        self.assertIn([1101, 'from all fwmark 0x101/0xfff lookup 101'], self.state['rules'])
        switch = [c for c in self.state['changes'] if c[0] == 'ip']
        self.assertEqual([c[3] for c in switch], ['add', 'del'])
        self.clear_changes()
        self.run_update()
        self.assertEqual(self.state['changes'], [])
        self.state['unhealthy'] = []
        self.save()
        self.run_update()
        self.assertEqual(self.router_rule(), [[32001, 'from all fwmark 0/0xfff iif lo lookup 101']])
        self.assertIn('router_interface:wan\n', self.status())

    def test_backup_failure_selects_another_and_keeps_it_when_first_recovers(self):
        self.state['config'] = {'hitwh_mwan.main.max_paths': '3'}
        self.state['paths']['wan3'] = {'dev': 'macwan3', 'ip': '10.0.0.4'}
        self.state['unhealthy'] = ['10.0.0.2']
        self.save()
        self.run_update()
        self.assertIn('router_interface:wan2\n', self.status())
        self.state['unhealthy'].append('10.0.0.3')
        self.save()
        self.run_update()
        self.assertIn('router_interface:wan3\n', self.status())
        self.state['unhealthy'] = ['10.0.0.2']
        self.save()
        self.run_update()
        self.assertIn('router_interface:wan3\n', self.status())

    def test_cold_boot_without_main_dhcp_uses_backup_dns_before_health(self):
        self.enable_dns()
        del self.state['paths']['wan']
        self.save()
        self.run_update()
        self.assertIn('active_count:1\n', self.status())
        self.assertIn('router_interface:wan2\n', self.status())
        self.assertIn('router_dns_configured:1\n', self.status())
        dnsfile = self.root / 'hitwh-mwan.dns'
        self.assertEqual(dnsfile.read_text(), 'server=172.26.26.3@10.0.0.3\nserver=219.146.1.66@10.0.0.3\n')
        self.clear_changes()
        self.run_update()
        self.assertEqual(self.state['changes'], [])

    def test_dhcp_dns_address_change_refreshes_only_changed_dns_file(self):
        self.enable_dns()
        self.run_update()
        self.clear_changes()
        self.state['paths']['wan2']['ip'] = '10.0.0.4'
        self.save()
        self.run_update()
        dns = (self.root / 'hitwh-mwan.dns').read_text()
        self.assertNotIn('@10.0.0.3', dns)
        self.assertIn('@10.0.0.4', dns)
        self.assertEqual(sum(c[0] == 'killall' for c in self.state['changes']), 1)

    def test_all_health_checks_fail_keeps_previous_exit_as_unverified(self):
        self.state['unhealthy'] = ['10.0.0.2']
        self.save()
        self.run_update()
        previous_rule = self.router_rule()
        self.state['unhealthy'].append('10.0.0.3')
        self.save()
        self.run_update()
        self.assertEqual(self.router_rule(), previous_rule)
        self.assertIn('router_state:unverified\n', self.status())
        self.assertIn('active_count:0\n', self.status())
        self.assertNotIn('ct state new', self.state['chain'])

    def test_cold_boot_without_healthy_paths_does_not_promote_one(self):
        self.state['unhealthy'] = ['10.0.0.2', '10.0.0.3']
        self.save()
        self.run_update()
        self.assertEqual(self.router_rule(), [])
        self.assertIn('router_state:unavailable\n', self.status())
        self.assertIn('active_count:0\n', self.status())

    def test_no_dhcp_on_any_path_publishes_zero_and_removes_router_rules(self):
        self.run_update()
        self.state['paths'] = {}
        self.state['routes'] = [r for r in self.state['routes'] if r['guard']]
        self.save()
        self.run_update()
        self.assertEqual(self.router_rule(), [])
        self.assertIn('router_state:unavailable\n', self.status())
        self.assertIn('active_count:0\n', self.status())
        self.assertIn([1102, 'from all fwmark 0x102/0xfff lookup 102'], self.state['rules'])

    def test_disabling_failover_removes_only_router_rules_and_dns(self):
        self.enable_dns()
        self.run_update()
        self.clear_changes()
        self.state['config']['hitwh_mwan.main.router_failover'] = '0'
        self.state['require_path_dns'] = False
        self.save()
        self.run_update()
        self.assertEqual(len(self.state['rules']), 4)
        self.assertEqual(self.router_rule(), [])
        self.assertEqual((self.root / 'hitwh-mwan.dns').read_text(), '')
        self.assertIn('router_state:disabled\n', self.status())
        self.assertFalse(any(c[0] == 'ip' and c[1] == '-4' and c[2] == 'route' for c in self.state['changes']))

    def test_custom_dns_serversfile_is_not_overwritten(self):
        custom = self.root / 'custom.dns'
        custom.write_text('server=192.0.2.1\n')
        self.state['config'] = {'dhcp.@dnsmasq[0].serversfile': custom.as_posix()}
        self.save()
        self.run_update()
        self.assertEqual(custom.read_text(), 'server=192.0.2.1\n')
        self.assertIn('router_dns_configured:0\n', self.status())

    def test_invalid_failover_option_fails_before_network_mutations(self):
        self.state['config'] = {'hitwh_mwan.main.router_failover': 'invalid'}
        self.save()
        self.run_update(expected=2)
        self.assertEqual(self.state['changes'], [])

    def test_early_dns_service_creates_file_and_preserves_existing_contents(self):
        self.enable_dns()
        boot_source = (ROOT / 'files/etc/init.d/hitwh-mwan-dns').read_text()
        with self.script.open('w', newline='\n') as script_file:
            script_file.write(rewrite_runtime_paths(boot_source, self.root.as_posix()) + '\nstart\n')
        self.run_update()
        dnsfile = self.root / 'hitwh-mwan.dns'
        self.assertEqual(dnsfile.read_text(), '')
        dnsfile.write_text('server=172.26.26.3@10.0.0.3\n')
        self.run_update()
        self.assertEqual(dnsfile.read_text(), 'server=172.26.26.3@10.0.0.3\n')

    def test_early_dns_service_leaves_custom_dns_setup_alone(self):
        boot_source = (ROOT / 'files/etc/init.d/hitwh-mwan-dns').read_text()
        with self.script.open('w', newline='\n') as script_file:
            script_file.write(rewrite_runtime_paths(boot_source, self.root.as_posix()) + '\nstart\n')
        self.run_update()
        self.assertFalse((self.root / 'hitwh-mwan.dns').exists())


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--mock': mock_command()
    unittest.main()
