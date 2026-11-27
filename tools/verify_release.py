#!/usr/bin/env python3
"""Verify the source snapshot without network, device or service operations."""
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def verify():
    manifest = ROOT / 'SHA256SUMS'
    expected = {}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        digest, name = line.split('  ', 1)
        path = pathlib.PurePosixPath(name)
        if not re.fullmatch('[0-9a-f]{64}', digest) or path.is_absolute() or '..' in path.parts or name in expected:
            raise ValueError('Invalid checksum entry')
        expected[name] = digest
    actual = {p.relative_to(ROOT).as_posix(): p for p in ROOT.rglob('*')
              if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts and p.name != 'SHA256SUMS'}
    if set(actual) != set(expected):
        raise ValueError('Source inventory changed')
    for name, p in actual.items():
        if p.is_symlink() or any(x.is_symlink() for x in p.parents):
            raise ValueError('Symlink rejected: ' + name)
        if p.suffix.lower() in ('.img', '.ext4', '.tar', '.gz', '.zip', '.deb', '.ko', '.so', '.pyc', '.ttf', '.ttc', '.otf'):
            raise ValueError('Non-source release artifact: ' + name)
        if p.name in ('authorized_keys', 'main-pool-layout.json', 'production-layout.json', 'registry.json') or 'private-key' in p.name or p.name.startswith(('id_ed25519', 'ssh_host_')):
            raise ValueError('Private device file: ' + name)
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected[name]:
            raise ValueError('Checksum mismatch: ' + name)
        if re.search(rb'(?m)^-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----', data):
            raise ValueError('Private key content: ' + name)
        if p.suffix == '.py':
            compile(data.decode('utf-8'), name, 'exec')
    print('OK: ' + str(len(actual)) + ' source files; checksums, inventory and private-artifact exclusions verified')


if __name__ == '__main__':
    try:
        verify()
    except (OSError, ValueError, SyntaxError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
