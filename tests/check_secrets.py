"""Reject private credential artifacts and common literal secrets without echoing them.

This is a conservative project guard, not a general-purpose secret detector.
Tests may use explicitly fictional literals starting with fake-/fake_.
"""
import argparse
import glob
import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
LITERAL = re.compile(r'''(?i)(?:["'](?:username|password|passwd|userid)["']\s*[:=]\s*|\b(?:username|password|passwd|userid)\b\s*=\s*|^\s*(?:username|password|passwd|userid)\s*:\s*)(["'])([^\r\n]*?)\1''')
ACCOUNT = re.compile(r"\b20\d{8}\b")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
NUMERIC_LITERAL = re.compile(r'''(?i)(?:["'](?:username|password|passwd|userid)["']\s*[:=]\s*|\b(?:username|password|passwd|userid)\b\s*[:=]\s*)(\d{4,})(?=\s*(?:[,;}#]|$))''')
TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{80,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b")


def violations(path, content):
    parts = Path(path).parts
    name = Path(path).name
    if ('auth.d' in parts or name in {'config.toml', 'mac-history', '.env', 'id_rsa', 'id_ed25519'}
            or (name.startswith('.env.') and name != '.env.example') or path.endswith('.credentials.json')):
        return ['private runtime file']
    text = content.decode('utf-8', errors='replace')
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        if ACCOUNT.search(line) or PRIVATE_KEY.search(line) or TOKEN.search(line):
            problems.append(f'possible secret at line {number}')
        if NUMERIC_LITERAL.search(line):
            problems.append(f'numeric credential literal at line {number}')
        for match in LITERAL.finditer(line):
            value = match[2]
            if value and not value.lower().startswith(('fake-', 'fake_')):
                problems.append(f'credential literal at line {number}')
    return problems


def history_files():
    """Read reachable blobs and commit messages without putting their contents in logs."""
    listing = subprocess.check_output(['git', 'rev-list', '--objects', '--all'], cwd=ROOT).decode('utf-8')
    objects = [line.split(' ', 1) for line in listing.splitlines()]
    process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for fields in objects:
            oid, path = fields[0], fields[1] if len(fields) == 2 else '(commit message)'
            process.stdin.write((oid + '\n').encode()); process.stdin.flush()
            header = process.stdout.readline().decode().split()
            content = process.stdout.read(int(header[2])); process.stdout.read(1)
            if header[1] == 'blob':
                yield f'history/{oid[:12]}/{path}', content
            elif header[1] == 'commit':
                yield f'history/{oid[:12]}/(commit message)', content.split(b'\n\n', 1)[-1]
    finally:
        process.stdin.close(); process.stdout.close(); process.wait()


def ipk_files(path):
    """Scan control hooks and payload without extracting files to the workspace."""
    with tarfile.open(path, 'r:gz') as outer:
        for layer in ('control.tar.gz', 'data.tar.gz'):
            content = outer.extractfile('./' + layer).read()
            with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as inner:
                for member in inner.getmembers():
                    if member.isfile():
                        yield f'ipk/{Path(path).name}/{layer}/{member.name}', inner.extractfile(member).read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true', help='inspect staged blobs for pre-commit')
    parser.add_argument('--history', action='store_true', help='also inspect all reachable Git history')
    parser.add_argument('--ipk', action='extend', nargs='+', default=[], help='also inspect IPK archives (supports globs)')
    args = parser.parse_args()
    command = ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACM', '-z'] if args.staged else [
        'git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z']
    names = subprocess.check_output(command, cwd=ROOT).decode('utf-8').split('\0')
    failed = False
    def check(name, content):
        findings = violations(name, content)
        for problem in findings:
            print(f'{name}: {problem}', file=sys.stderr)
        return bool(findings)
    for name in sorted(set(filter(None, names))):
        path = ROOT / name
        if args.staged:
            content = subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
        elif path.is_file():
            content = path.read_bytes()
        else:
            continue
        failed = check(name, content) or failed
    if args.history:
        for name, content in history_files(): failed = check(name, content) or failed
    for package in args.ipk:
        matches = glob.glob(package)
        if not matches: parser.error('IPK path does not match any files')
        for match in matches:
            for name, content in ipk_files(match): failed = check(name, content) or failed
    if not failed: print('Credential artifact and literal checks passed.')
    return int(failed)


if __name__ == '__main__': sys.exit(main())
