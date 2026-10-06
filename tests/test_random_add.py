"""Run the actual add-random transaction with fictional interactive credentials."""
import json
import os
from pathlib import Path
import subprocess
import unittest
import test_manager as fixtures


@unittest.skipUnless(os.name == 'posix', 'Private filesystem + flock tests run on Linux')
class RandomAddTests(unittest.TestCase):
    setUp = fixtures.EditTests.setUp
    save = fixtures.EditTests.save
    run_manager = fixtures.EditTests.run_manager

    def free_slot(self, number=4):
        self.state['config']['hitwh_mwan.main.max_paths'] = str(number)
        self.save()

    def logins(self):
        return [e for e in self.state['events'] if e[:2] == ['auth', 'login']]

    def test_success_creates_fixed_local_unicast_mac_and_only_authenticates_new_wan(self):
        self.free_slot()
        self.state['healthy']['wan'] = False
        self.save()
        output = self.run_manager('add', 'random')
        self.assertIn('Added and authenticated wan4', output)
        self.assertEqual(self.logins(), [['auth', 'login', 'wan4']])
        self.assertEqual(self.state['config']['network.wan4dev.macaddr'], '02:10:20:30:40:50')
        credentials = self.root / 'private/auth.d/wan4.json'
        self.assertEqual(json.loads(credentials.read_text())['mac'], '02:10:20:30:40:50')
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertIn('02:10:20:30:40:50', (self.root / 'private/mac-history').read_text())
        self.assertNotIn('fake-secret', output + repr(self.state['events']) + (self.root / 'private/mac-history').read_text())
        self.assertFalse(list((self.root / 'private/auth.d').glob('.pending.*')))

    def test_collision_checks_all_configuration_runtime_credentials_backups_and_history(self):
        self.free_slot()
        self.state['config']['network.wan6dev.macaddr'] = '02:10:00:00:00:01'
        self.state['config']['network.unmanaged.macaddr'] = '02:10:00:00:00:02'
        runtime = self.root / 'sys/wifi0'; runtime.mkdir()
        (runtime / 'address').write_text('02:10:00:00:00:03\n')
        base = self.root / 'private'; base.mkdir(mode=0o700)
        (base / 'mac-history').write_text('02:10:00:00:00:04\n'); (base / 'mac-history').chmod(0o600)
        auth = base / 'auth.d'; auth.mkdir(mode=0o700)
        record = auth / 'wan9.json'; record.write_text(json.dumps({'mac': '02:10:00:00:00:05'})); record.chmod(0o600)
        backups = self.root / 'backups'; backups.mkdir()
        (backups / 'network-old').write_text("option macaddr '02:10:00:00:00:06'\noption password 'fake-secret'\n")
        self.state['random_candidates'] = [f'10:00:00:00:0{i}' for i in range(1, 8)]
        self.save()
        self.run_manager('add', 'random')
        self.assertEqual(self.state['random_calls'], 7)
        self.assertEqual(self.state['config']['network.wan4dev.macaddr'], '02:10:00:00:00:07')
        self.assertNotIn('password', (base / 'mac-history').read_text())

    def test_capacity_and_cancel_do_not_create_or_overwrite_any_wan(self):
        original = self.state['config'].copy()
        self.run_manager('add', 'random', expected=4)
        self.assertEqual(self.state['events'], [])
        self.assertEqual(self.state['config'], original)
        self.free_slot(); original = self.state['config'].copy()
        self.state['cancel_input'] = True; self.save()
        self.run_manager('add', 'random', expected=1)
        self.assertEqual(self.state['config'], original)
        self.assertFalse(self.logins())

    def test_random_failure_and_collision_exhaustion_never_create_wan(self):
        self.free_slot(); original = self.state['config'].copy()
        self.state['random_failure'] = True; self.save()
        self.assertIn('random source failed', self.run_manager('add', 'random', expected=1))
        self.assertEqual(self.state['config'], original)
        self.state.pop('random_failure')
        self.state['random_calls'] = 0
        self.state['random_candidates'] = ['00:00:00:00:01']; self.save()
        self.assertIn('collision limit', self.run_manager('add', 'random', expected=1))
        self.assertEqual(self.state['config'], original)
        self.assertEqual(self.state['random_calls'], 32)

    def test_creation_and_credential_failure_roll_back_but_keep_attempted_mac(self):
        for failure in ('fail_set', 'fail_import'):
            with self.subTest(failure=failure):
                self.free_slot()
                self.state[failure] = 'network.wan4.proto' if failure == 'fail_set' else True
                self.save()
                self.run_manager('add', 'random', expected=1)
                self.assertNotIn('network.wan4', self.state['config'])
                self.assertNotIn('network.wan4dev', self.state['config'])
                self.assertFalse((self.root / 'private/auth.d/wan4.json').exists())
                expected_mac = '02:' + self.state.get('random_candidates', ['10:20:30:40:50'])[0]
                self.assertIn(expected_mac, (self.root / 'private/mac-history').read_text())
                self.assertFalse(self.logins())
                self.state.pop(failure)
                self.state['random_candidates'] = ['11:20:30:40:50']; self.save()

    def test_dhcp_and_auth_failure_keep_new_identity_without_background_retry(self):
        self.free_slot(); self.state['no_new_dhcp'] = True; self.save()
        self.assertIn('retained offline (no DHCP)', self.run_manager('add', 'random', expected=1))
        self.assertFalse(self.logins())
        self.assertTrue((self.root / 'private/auth.d/wan4.json').exists())
        self.state['no_new_dhcp'] = False
        self.state['ips']['wan4'] = '10.0.0.44'
        self.state['healthy']['wan4'] = False
        self.state['auth_failure'] = True; self.save()
        self.run_manager('refresh', expected=1)
        self.assertEqual(self.logins(), [['auth', 'login', 'wan4']])
        self.run_manager('list')
        self.assertEqual(self.logins(), [['auth', 'login', 'wan4']])

    def test_auth_failure_is_nonzero_and_retains_credentials(self):
        self.free_slot(); self.state['auth_failure'] = True; self.save()
        self.assertIn('retained offline', self.run_manager('add', 'random', expected=1))
        self.assertEqual(self.logins(), [['auth', 'login', 'wan4']])
        self.assertTrue((self.root / 'private/auth.d/wan4.json').exists())
        self.assertEqual(self.state['random_calls'], 1)

    def test_remove_deletes_credentials_but_history_prevents_mac_reuse(self):
        self.free_slot(); self.run_manager('add', 'random')
        self.run_manager('remove', 'wan4')
        self.assertFalse((self.root / 'private/auth.d/wan4.json').exists())
        # Real netifd deletes its runtime device on network reload.
        import shutil
        shutil.rmtree(self.root / 'sys/macwan4')
        self.state['random_candidates'] = ['10:20:30:40:50', '11:20:30:40:50']
        self.state['random_calls'] = 0; self.save()
        self.run_manager('add', 'random')
        self.assertEqual(self.state['random_calls'], 2)
        self.assertEqual(self.state['config']['network.wan4dev.macaddr'], '02:11:20:30:40:50')

    def test_management_lock_blocks_add_refresh_edit_and_remove_without_changes(self):
        import fcntl
        self.free_slot()
        with (self.root / 'hitwh-mwan-management.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for args in [('add', 'random'), ('refresh',), ('edit', 'wan2', '02:11:22:33:44:55'), ('remove', 'wan2')]:
                self.run_manager(*args, expected=1)
        self.assertFalse(any(e[0] == 'uci' for e in self.state['events']))
        self.assertFalse(self.logins())
        self.assertFalse(list((self.root / 'private/auth.d').glob('.pending.*')))

    def test_skips_reserved_and_orphan_slots(self):
        self.free_slot(8)
        self.state['config']['network.wan4dev'] = 'device'
        self.state['config']['network.other'] = 'device'
        self.state['config']['network.other.name'] = 'macwan5'
        self.save()
        self.run_manager('add', 'random')
        self.assertEqual(self.logins(), [['auth', 'login', 'wan7']])
        self.assertNotIn('network.wan6', self.state['config'])

    def test_unsupported_runtime_fails_before_credential_prompt(self):
        self.free_slot()
        (self.root / 'module/macvlan').rmdir()
        self.run_manager('add', 'random', expected=1)
        self.assertFalse(self.state['events'])
        self.assertNotIn('network.wan4', self.state['config'])

    def test_runtime_mac_mismatch_rolls_back_the_new_path(self):
        self.free_slot(); self.state['ignore_mac_apply'] = True; self.save()
        self.assertIn('MAC did not apply', self.run_manager('add', 'random', expected=1))
        self.assertNotIn('network.wan4', self.state['config'])
        self.assertFalse((self.root / 'private/auth.d/wan4.json').exists())
        self.assertFalse(self.logins())
        self.assertTrue(self.state['up']['wan'])

    def test_ordinary_add_never_prompts_or_authenticates(self):
        self.free_slot(); self.state['healthy']['wan'] = False; self.save()
        self.run_manager('add', '02:10:20:30:40:50')
        self.assertFalse(any(e[0] == 'auth' for e in self.state['events']))
        self.assertFalse((self.root / 'private/auth.d/wan4.json').exists())

    def test_gui_stdin_add_uses_private_credentials_without_interactive_prompt(self):
        self.free_slot()
        values={'username':'fake-user','password':'fake-secret &账'}
        result=subprocess.run([*self.command,'add','random','--stdin'],env=self.env,input=json.dumps(values),
                              text=True,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.state=json.loads(self.state_path.read_text())
        self.assertEqual(self.logins(),[['auth','login','wan4']])
        self.assertIn(['auth','prepare-import'],self.state['events'])
        self.assertNotIn(values['password'],result.stdout+result.stderr+repr(self.state['events']))


if __name__ == '__main__': unittest.main()
