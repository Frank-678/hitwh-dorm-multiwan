"""Guard private paths and credential literals without reporting their contents."""
import unittest
import json
from check_secrets import violations


class SecretGuardTests(unittest.TestCase):
    def test_private_artifacts_are_rejected(self):
        for path in ('auth.d/wan.json', 'nested/auth.d/wan2.json', 'tool/config.toml', 'wan.credentials.json',
                     '.env', '.env.local', 'keys/id_ed25519'):
            self.assertTrue(violations(path, b'{}'))

    def test_fictional_values_empty_values_and_prompt_code_are_allowed(self):
        text = b'''{"username":"fake-user","password":"fake-secret"}
{"username":"","password":""}
until(b'Username: '); os.write(terminal, b'fake-user')
username = data.get("username")
'''
        self.assertFalse(violations('tests/fictional.py', text))

    def test_nonfictional_literals_are_rejected_without_echo(self):
        # Assemble the value so this scanner's own test is not a secret fixture.
        value = 'made' + 'up-sensitive-value'
        for name, separator in (('password', ':'), ('username', '=')):
            content = f'"{name}"{separator}"{value}"'
            findings = violations('sample.txt', content.encode())
            self.assertTrue(findings)
            self.assertNotIn(value, repr(findings))
        self.assertTrue(violations('sample.txt', ('20' + '12345678').encode()))

    def test_numeric_credentials_and_access_tokens_are_rejected_without_echo(self):
        value = '7654' + '3210'
        findings = violations('sample.json', json.dumps({'pass' + 'word': int(value)}).encode())
        self.assertTrue(findings)
        self.assertNotIn(value, repr(findings))
        token = 'gh' + 'p_' + 'X' * 36
        findings = violations('sample.txt', token.encode())
        self.assertTrue(findings)
        self.assertNotIn(token, repr(findings))


if __name__ == '__main__': unittest.main()
