"""Exercise the real update shell script with isolated network command fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def mock_command():
    state_path = Path(os.environ['MOCK_STATE'])
    state = json.loads(state_path.read_text())
    name, *args = sys.argv[2:]
    result = 0
    output = ''
    change = False
    if name == 'uci':
        key = args[-1]
        values = {'hitwh_mwan.main.max_paths': '2'}
        if key.startswith('network.'):
            result = 0 if key[8:] in state['paths'] else 1
        else:
            output = values.get(key, '')
            result = 0 if output else 1
    elif name == 'ubus':
        iface = args[1].split('.')[-1]
        path = state['paths'].get(iface)
        output = json.dumps({'l3_device': path['dev'], 'ipv4-address': [{'address': path['ip']}], 'route': [{'nexthop': state['gateway']}]}) if path else '{}'
    elif name == 'jsonfilter':
        data = json.load(sys.stdin)
        expression = args[-1]
        if expression == '@.l3_device': output = data.get('l3_device', '')
        elif 'ipv4-address' in expression: output = data.get('ipv4-address', [{}])[0].get('address', '')
        elif expression == '@.route[0].nexthop': output = data.get('route', [{}])[0].get('nexthop', '')
    elif name == 'curl':
        output = '500' if args[args.index('--interface') + 1] in state['unhealthy'] else '204'
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
                state['routes'] = [r for r in state['routes'] if (r['table'], r['prefix'], r.get('metric')) != (table, prefix, row.get('metric'))]
                state['routes'].append(row)
            else:
                raise AssertionError(f'Unexpected destructive route command: {args}')
    else:
        raise AssertionError(name)
    if change:
        state['changes'].append([name, *sys.argv[3:]])
        state_path.write_text(json.dumps(state))
    if output: print(output, end='' if name == 'curl' else '\n')
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
        self.script.write_text(source.replace('/var/run/', fixture + '/').replace('/tmp/', fixture + '/'), newline='\n')
        bindir = self.root / 'bin'
        bindir.mkdir()
        for name in ('uci', 'ubus', 'jsonfilter', 'curl', 'ip', 'nft', 'logger'):
            file = bindir / name
            file.write_text(f'#!/bin/sh\nexec "$MOCK_PYTHON" "$MOCK_DISPATCH" --mock {name} "$@"\n', newline='\n')
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
        self.assertEqual(len(self.state['rules']), 4)
        self.clear_changes()
        self.run_update()
        self.assertEqual(self.state['changes'], [])

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
        self.assertIn('numgen random mod 2', self.state['chain'])

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


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--mock': mock_command()
    unittest.main()
