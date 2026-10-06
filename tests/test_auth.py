"""Execute the real POSIX credential/auth helper against fake ePortal responses.

Only invented credentials are used. Linux tests check actual filesystem modes;
UID is modelled so CI need not run as root.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def mock_command():
    state_path = Path(os.environ['AUTH_TEST_STATE'])
    state = json.loads(state_path.read_text())
    name, *args = sys.argv[2:]
    if name == 'id':
        print(state.get('caller_uid', 0))
    elif name == 'stat':
        file = Path(args[-1])
        print(f"{state.get('owner_uid', 0)}:{stat.S_IMODE(file.stat().st_mode):o}")
    elif name == 'uci':
        key = args[-1]
        value = state['config'].get(key)
        if value is None: raise SystemExit(1)
        print(value)
    elif name == 'ubus':
        iface = args[1].split('.')[-1]
        address = state['ips'].get(iface, '')
        print(json.dumps({'ipv4-address': [{'address': address}]} if address else {}))
    elif name == 'jsonfilter':
        try:
            data = json.loads(Path(args[args.index('-i') + 1]).read_text()) if '-i' in args else json.load(sys.stdin)
            expression = args[args.index('-e') + 1]
            value = data.get('ipv4-address', [{}])[0].get('address', '') if 'ipv4-address' in expression else data.get(expression[2:], '')
            if isinstance(value, bool): value = str(value).lower()
            if value != '': print(value)
        except (ValueError, OSError, TypeError): raise SystemExit(1)
    elif name == 'hexdump':
        print(' '.join(f'{byte:02X}' for byte in sys.stdin.buffer.read()), end='')
    elif name == 'curl':
        url = args[-1]
        method = parse_qs(urlsplit(url).query).get('method', ['health'])[0]
        payload = sys.stdin.buffer.read().decode('utf-8') if '@-' in args else ''
        state['requests'].append({'args': args, 'payload': payload, 'method': method,
                                  'exported': {key: value for key, value in os.environ.items()
                                               if value in (state['username'], state['password'])}})
        if state.get('network_error'):
            state_path.write_text(json.dumps(state))
            raise SystemExit(7)
        if method == 'health':
            code = '204' if state['healthy'] else '200'
            body = '' if state['healthy'] else f"<script>top.self.location.href='{state['redirect']}'</script>"
            if state.get('http_redirect') and not state['healthy']: code, body = '302', ''
        elif method == 'pageInfo':
            code, body = '200', json.dumps(state['page'])
        elif method == 'getServices':
            code, body = '200', json.dumps({'isService': state.get('is_service', 'false')})
        elif method == 'login':
            fields = parse_qs(payload, keep_blank_values=True)
            state['received'] = {key: unquote(value[0]) for key, value in fields.items()}
            if state.get('login_result', 'success') == 'success' and not state.get('verification_failure'):
                state['healthy'] = True
            if state.get('change_address'):
                state['ips']['wan'] = '10.0.0.99'
            code, body = '200', json.dumps({'result': state.get('login_result', 'success'),
                                          'message': state['password']})
        else:
            raise AssertionError(method)
        if '-o' in args:
            destination = args[args.index('-o') + 1]
            if destination != '/dev/null': Path(destination).write_text(body)
        else:
            sys.stdout.write(body)
        if '-D' in args:
            Path(args[args.index('-D') + 1]).write_text(f'HTTP/1.1 {code}\r\nLocation: {state["redirect"]}\r\n' if state.get('http_redirect') else f'HTTP/1.1 {code}\r\n')
        if '-w' in args: sys.stdout.write(code)
        state_path.write_text(json.dumps(state))
    else:
        raise AssertionError(name)


@unittest.skipUnless(os.name == 'posix', 'POSIX ownership/mode tests run on Linux')
class AuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state_path = self.root / 'state.json'
        self.auth_dir = self.root / 'private' / 'auth.d'
        self.state = {
            'config': {'hitwh_mwan.main.main_interface': 'wan', 'hitwh_mwan.main.parent_device': 'eth1',
                       'hitwh_mwan.main.portal_url': 'http://portal.test/eportal',
                       'hitwh_mwan.main.health_url': 'http://check.test/generate_204',
                       'network.wan': 'interface', 'network.wan.device': 'eth1',
                       'network.wan.macaddr': '02:00:00:00:00:01'},
            'ips': {'wan': '10.0.0.10'}, 'healthy': False, 'requests': [],
            'page': {'passwordEncrypt': 'false', 'validCodeUrl': ''},
            'redirect': 'http://portal.test/eportal/index.jsp?wlanuserip=token+one&mac=encrypted&url=http%3A%2F%2Fexample.test',
            'username': 'fake-user+&%"账', 'password': 'fake-secret &+%"\\$`字',
        }
        self.save()
        device = self.root / 'sys' / 'eth1'
        device.mkdir(parents=True)
        (device / 'address').write_text('02:00:00:00:00:01\n')
        self.script = self.root / 'auth.sh'
        self.script.write_text((ROOT / 'files/usr/sbin/hitwh-mwan-auth').read_text().replace('/sys/class/net', str(self.root / 'sys')))
        bindir = self.root / 'bin'
        bindir.mkdir()
        for command in ('id', 'stat', 'uci', 'ubus', 'jsonfilter', 'hexdump', 'curl'):
            wrapper = bindir / command
            wrapper.write_text(f'#!/bin/sh\nexec "$AUTH_TEST_PYTHON" "$AUTH_TEST_DISPATCH" --mock {command} "$@"\n')
            wrapper.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
                    'AUTH_TEST_STATE': str(self.state_path), 'AUTH_TEST_PYTHON': sys.executable,
                    'AUTH_TEST_DISPATCH': str(Path(__file__).resolve()),
                    'HITWH_MWAN_AUTH_DIR': str(self.auth_dir), 'TMPDIR': str(self.root)}

    def save(self): self.state_path.write_text(json.dumps(self.state))

    def run_helper(self, action, iface='wan', payload=None, expected=0):
        result = subprocess.run(['sh', str(self.script), action, iface], input=payload, text=True,
                                capture_output=True, env=self.env, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.state = json.loads(self.state_path.read_text())
        return result

    def configure(self):
        payload = json.dumps({'username': self.state['username'], 'password': self.state['password']})
        result = self.run_helper('import', payload=payload)
        self.assertNotIn(self.state['password'], result.stdout + result.stderr)

    def test_import_get_status_remove_and_private_modes(self):
        self.configure()
        self.assertEqual(stat.S_IMODE(self.auth_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.auth_dir.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.auth_dir / 'wan.json').stat().st_mode), 0o600)
        value = json.loads(self.run_helper('get').stdout)
        self.assertEqual(value['password'], self.state['password'])
        self.assertEqual(value['username'], self.state['username'])
        self.assertTrue(json.loads(self.run_helper('status').stdout)['configured'])
        self.assertFalse(self.state['requests'])
        self.run_helper('remove')
        self.assertFalse(json.loads(self.run_helper('get').stdout)['configured'])
        self.assertFalse(list(self.root.glob('hitwh-mwan-auth.*')))

    def test_login_binds_every_request_and_correctly_encodes_special_characters(self):
        self.configure()
        result = self.run_helper('login')
        self.assertIn('AUTH_RESULT wan success', result.stdout)
        self.assertNotIn(self.state['password'], result.stdout + result.stderr)
        self.assertEqual(self.state['received']['userId'], self.state['username'])
        self.assertEqual(self.state['received']['password'], self.state['password'])
        self.assertEqual(self.state['received']['queryString'], urlsplit(self.state['redirect']).query)
        self.assertEqual([request['method'] for request in self.state['requests']], ['health', 'pageInfo', 'getServices', 'login', 'health'])
        for request in self.state['requests']:
            self.assertEqual(request['args'][request['args'].index('--interface') + 1], '10.0.0.10')
            self.assertNotIn(self.state['password'], ' '.join(request['args']))
            self.assertFalse(request['exported'])

    def test_auth_failure_never_echoes_portal_message_or_retries(self):
        self.configure()
        self.state['login_result'] = 'fail'; self.save()
        result = self.run_helper('login', expected=1)
        self.assertIn('authentication_failed', result.stdout)
        self.assertNotIn(self.state['password'], result.stdout + result.stderr)
        self.assertEqual(sum(request['method'] == 'login' for request in self.state['requests']), 1)

    def test_online_check_does_not_submit_login(self):
        self.configure(); self.state['healthy'] = True; self.save()
        self.assertIn('already_online', self.run_helper('login').stdout)
        self.assertEqual([request['method'] for request in self.state['requests']], ['health'])

    def test_untrusted_portal_never_receives_a_password(self):
        self.configure(); self.state['redirect'] = 'http://other.test/eportal/index.jsp?token=untrusted'; self.save()
        self.assertIn('portal_not_detected', self.run_helper('login', expected=1).stdout)
        self.assertEqual([request['method'] for request in self.state['requests']], ['health'])

    def test_permissions_owner_and_symlink_are_rejected(self):
        self.configure()
        file = self.auth_dir / 'wan.json'
        file.chmod(0o644)
        self.assertIn('insecure_storage', self.run_helper('get', expected=1).stdout)
        file.chmod(0o600)
        self.state['owner_uid'] = 1; self.save()
        self.assertIn('insecure_storage', self.run_helper('get', expected=1).stdout)
        self.state['owner_uid'] = 0; self.save()
        file.unlink(); file.symlink_to(self.state_path)
        self.assertIn('insecure_storage', self.run_helper('get', expected=1).stdout)

    def test_missing_address_credentials_and_changed_mac_do_not_submit_login(self):
        self.assertIn('missing_credentials', self.run_helper('login', expected=1).stdout)
        self.configure()
        self.state['ips']['wan'] = ''; self.save()
        self.assertIn('no_dhcp', self.run_helper('login', expected=1).stdout)
        self.state['ips']['wan'] = '10.0.0.10'; self.save()
        (self.root / 'sys/eth1/address').write_text('02:00:00:00:00:99\n')
        self.assertIn('mac_mismatch', self.run_helper('login', expected=1).stdout)
        self.assertFalse(self.state['requests'])

    def test_captcha_and_encryption_fail_closed(self):
        self.configure()
        self.state['page']['passwordEncrypt'] = 'true'; self.save()
        self.assertIn('encryption_required', self.run_helper('login', expected=1).stdout)
        self.state['page'] = {'passwordEncrypt': 'false', 'validCodeUrl': '/captcha'}; self.save()
        self.assertIn('captcha_required', self.run_helper('login', expected=1).stdout)
        self.assertFalse(any(request['method'] == 'login' for request in self.state['requests']))

    def test_success_requires_204_and_unchanged_source_address(self):
        self.configure(); self.state['verification_failure'] = True; self.save()
        self.assertIn('verification_failed', self.run_helper('login', expected=1).stdout)
        self.state['verification_failure'] = False; self.state['change_address'] = True; self.save()
        self.assertIn('address_changed', self.run_helper('login', expected=1).stdout)

    def test_invalid_interface_and_import_are_rejected_without_configuration(self):
        for iface in ('wan6', '../wan', 'wan02', 'lan'):
            self.assertIn('invalid_interface', self.run_helper('status', iface, expected=1).stdout)
        self.assertIn('invalid_credentials', self.run_helper('import', payload='{"username":"fake-user"}', expected=1).stdout)
        self.assertFalse((self.auth_dir / 'wan.json').exists())

    def test_interactive_prepare_masks_password_and_returns_only_private_handoff(self):
        import pty
        import select
        import time
        child, terminal = pty.fork()
        if child == 0:
            os.execvpe('sh', ['sh', str(self.script), 'prepare'], self.env)
        self.addCleanup(os.close, terminal)
        output = b''
        def until(marker):
            nonlocal output
            deadline = time.monotonic() + 8
            while marker not in output and time.monotonic() < deadline:
                if select.select([terminal], [], [], 0.2)[0]: output += os.read(terminal, 4096)
            self.assertIn(marker, output)
        until(b'Username: '); os.write(terminal, b'fake-user\n')
        until(b'Password (hidden): ')
        # Wait for the terminal flag, avoiding a race between prompt and stty.
        import termios
        deadline = time.monotonic() + 5
        while termios.tcgetattr(terminal)[3] & termios.ECHO and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(termios.tcgetattr(terminal)[3] & termios.ECHO)
        os.write(terminal, b'fake-secret-hidden\n')
        until(b'.pending.')
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertNotIn(b'fake-secret-hidden', output)
        self.assertTrue(termios.tcgetattr(terminal)[3] & termios.ECHO)
        pending = next(self.auth_dir.glob('.pending.*'))
        self.assertEqual(pending.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((pending / 'value.json').read_text())['password'], 'fake-secret-hidden')
        self.assertFalse((self.auth_dir / 'wan.json').exists())

    def test_decoded_controls_are_rejected_and_literal_escape_text_is_preserved(self):
        for control in ('\n', '\r', '\t', '\0', '\x7f'):
            payload = json.dumps({'username': 'fake-user', 'password': 'fake-secret' + control})
            self.assertIn('invalid_credentials', self.run_helper('import', payload=payload, expected=1).stdout)
        values = {'username': 'fake-user', 'password': r'fake-secret\u0000\n'}
        self.run_helper('import', payload=json.dumps(values))
        self.assertEqual(json.loads(self.run_helper('get').stdout)['password'], values['password'])

    def test_no_background_entrypoint_calls_authentication(self):
        for path in ('files/usr/sbin/hitwh-mwan-update', 'files/usr/sbin/hitwh-mwan-monitor',
                     'files/etc/init.d/hitwh-mwan', 'files/etc/hotplug.d/iface/95-hitwh-mwan'):
            source = (ROOT / path).read_text()
            self.assertNotIn('hitwh-mwan-auth', source)
            self.assertNotIn('auth.d', source)

    def test_prepare_import_accepts_stdin_and_creates_only_private_handoff(self):
        values = {'username':'fake-user','password':'fake-secret &账'}
        result = self.run_helper('prepare-import', payload=json.dumps(values))
        pending = Path(result.stdout.strip())
        self.assertEqual(json.loads((pending/'value.json').read_text())['password'],values['password'])
        self.assertEqual(pending.stat().st_mode & 0o777,0o700)
        self.assertFalse((self.auth_dir/'wan.json').exists())
        self.assertFalse(self.state['requests'])

    def test_http_location_redirect_is_supported_and_discovery_submits_no_credentials(self):
        self.state['http_redirect'] = True; self.save()
        result = self.run_helper('discover')
        self.assertEqual(json.loads(result.stdout),{'portal_url':'http://portal.test/eportal'})
        self.assertEqual([r['method'] for r in self.state['requests']],['health'])
        self.assertNotIn('encrypted',result.stdout)
        self.configure()
        self.assertIn('success',self.run_helper('login').stdout)



if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--mock':
        mock_command(); raise SystemExit(0)
    unittest.main()
