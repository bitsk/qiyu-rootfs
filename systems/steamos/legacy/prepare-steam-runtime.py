#!/usr/bin/env python3
"""Stage Reframed's pinned native Steam client helper runtime, without deployment."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import urllib.request


REPO = Path(__file__).resolve().parent.parent
PROFILE = REPO / 'steam-frame-research/sources/reframed-vr/profiles/steam-client.json'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPO / 'artifacts/steamos-mix/runtime')
    args = parser.parse_args()
    spec = json.loads(PROFILE.read_text())['runtime']
    if spec['file'] != 'steam-runtime-steamrt-arm64.tar.xz':
        raise SystemExit('Expected the native ARM64 client helper runtime')
    url = spec['base_url'] + spec['file']
    if not url.startswith('https://repo.steampowered.com/'):
        raise SystemExit('Expected the official Steam runtime distribution host')
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / spec['file']
    if not archive.exists():
        partial = archive.with_suffix(archive.suffix + '.partial')
        with urllib.request.urlopen(url, timeout=60) as response, partial.open('wb') as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        if partial.stat().st_size != spec['size'] or sha256(partial) != spec['sha256']:
            raise SystemExit(f'Runtime integrity mismatch; untrusted download retained at {partial}')
        partial.rename(archive)
    if archive.stat().st_size != spec['size'] or sha256(archive) != spec['sha256']:
        raise SystemExit('Existing runtime archive does not match the pinned size and SHA-256')

    helpers = []
    native_launcher = None
    count = 0
    unpacked = 0
    with tarfile.open(archive, 'r:xz') as source:
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise SystemExit(f'Unsafe archive member: {member.name}')
            if not path.parts or path.parts[0] != 'steam-runtime-steamrt-arm64':
                raise SystemExit(f'Unexpected runtime archive root: {member.name}')
            if member.ischr() or member.isblk() or member.isfifo():
                raise SystemExit(f'Unexpected device in client runtime: {member.name}')
            if member.islnk() or member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute():
                    raise SystemExit(f'Unexpected absolute link: {member.name}')
                # Resolve the archive link lexically; the runtime is staged
                # for extraction under a dedicated Steam client directory.
                base = path.parent if member.issym() else PurePosixPath()
                resolved = []
                for part in (base / target).parts:
                    if part == '..':
                        if not resolved:
                            raise SystemExit(f'Escaping archive link: {member.name}')
                        resolved.pop()
                    elif part != '.':
                        resolved.append(part)
            count += 1
            unpacked += member.size
            if 'launcher-service' in path.name or 'srt-launcher-service' in path.name:
                helpers.append({'path': member.name, 'bytes': member.size,
                                'link': member.linkname or None})
            if member.name == ('steam-runtime-steamrt-arm64/pressure-vessel/libexec/'
                               'steam-runtime-tools-0/aarch64-linux-gnu-srt-launcher-service'):
                payload = source.extractfile(member).read()
                if payload[:6] != b'\x7fELF\x02\x01' or int.from_bytes(payload[18:20], 'little') != 183:
                    raise SystemExit('Launcher service is not an AArch64 ELF64 executable')
                native_launcher = {'path': member.name, 'architecture': 'AArch64',
                                   'sha256': hashlib.sha256(payload).hexdigest()}
    if not native_launcher:
        raise SystemExit('Native runtime has no AArch64 launcher service')
    manifest = {
        'source_url': url, 'sha256': spec['sha256'], 'bytes': archive.stat().st_size,
        'profile': str(PROFILE.relative_to(REPO)), 'profile_sha256': sha256(PROFILE),
        'reframed_commit': subprocess.check_output(
            ['git', '-C', str(PROFILE.parent.parent), 'rev-parse', 'HEAD'], text=True).strip(),
        'archive_members': count, 'unpacked_file_bytes': unpacked, 'launcher_members': helpers,
        'native_launcher': native_launcher,
        'purpose': 'Native ARM64 Steam client helper service; no games, FEX, or system replacement',
        'deployment': 'Extract below the dedicated Steam client root and prepend steam-runtime-steamrt-arm64/bin to its PATH',
    }
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
