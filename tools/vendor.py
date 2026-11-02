#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch checksum-locked private vendor assets, then verify and map them offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO / 'devices/a081/vendor.lock.json'

def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()

def load_lock(path):
    lock = json.loads(path.read_text())
    if lock['schema'] != 1 or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', lock['repository']):
        raise ValueError('Unsupported vendor lock')
    return lock

def relative(value):
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in value:
        raise ValueError('Invalid relative vendor path')
    return path

def verify_cache(cache, lock_path=DEFAULT_LOCK, required_bundles=None):
    cache = Path(cache).resolve()
    lock_path = Path(lock_path)
    lock = load_lock(lock_path)
    receipt = json.loads((cache / 'receipt.json').read_text())
    # Identity and per-file hashes keep metadata-only updates cache-compatible.
    if any(receipt.get(key) != lock.get(key) for key in ['repository', 'tag', 'model']):
        raise ValueError('Cache was created for a different vendor release or model')
    selected = receipt['bundles']
    if len(selected) != len(set(selected)) or set(selected) - set(lock['bundles']):
        raise ValueError('Unknown or duplicate cached bundle')
    if set(required_bundles or []) - set(selected):
        raise ValueError('Required vendor bundles are missing')
    artifacts = []
    destinations = set()
    for name in selected:
        relative(name)
        for entry in lock['bundles'][name]['files']:
            path = relative(entry['path'])
            destination = relative(entry['install_path'])
            if destination.parts[0] not in ['lib', 'usr', 'opt', 'etc'] or str(destination) in destinations:
                raise ValueError('Invalid or duplicate install destination')
            source = cache / name / str(path)
            if (source.is_symlink() or not source.is_file()
                    or not source.resolve().is_relative_to(cache)
                    or source.stat().st_size != entry['bytes']
                    or digest(source) != entry['sha256']):
                raise ValueError('Vendor file verification failed: ' + name + '/' + str(path))
            destinations.add(str(destination))
            artifacts.append({'source': str(source), 'destination': str(destination), 'sha256': entry['sha256']})
    return artifacts

def unpack(archive, destination, bundle):
    expected = {entry['path']: entry for entry in bundle['files']}
    seen = set()
    with tarfile.open(archive, 'r:gz') as stream:
        for member in stream:
            name = str(relative(member.name))
            if name in seen or name not in {*expected, 'bundle.json'} or not member.isfile():
                raise ValueError('Unexpected archive member: ' + name)
            seen.add(name)
            data = stream.extractfile(member)
            if data is None:
                raise ValueError('Missing archive data')
            if name == 'bundle.json':
                # The archive itself is pinned; install decisions come from the lock.
                if member.size > 2_000_000:
                    raise ValueError('Oversize manifest')
                json.load(data)
                continue
            entry = expected[name]
            if member.size != entry['bytes']:
                raise ValueError('Archive member size mismatch')
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('xb') as target:
                shutil.copyfileobj(data, target)
            output.chmod(entry['mode'] & 0o755)
    if seen != {*expected, 'bundle.json'}:
        raise ValueError('Incomplete vendor archive')

def fetch(lock_path, output, selected):
    lock = load_lock(lock_path)
    selected = selected or list(lock['bundles'])
    if len(selected) != len(set(selected)) or set(selected) - set(lock['bundles']):
        raise ValueError('Unknown or duplicate requested bundle')
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Choose a new vendor-cache directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.qiyu-vendor-', dir=output.parent) as temp:
        staged = Path(temp) / 'cache'
        downloads = Path(temp) / 'downloads'
        staged.mkdir()
        downloads.mkdir()
        for name in selected:
            relative(name)
            bundle = lock['bundles'][name]
            asset = bundle['asset']
            if relative(asset).name != asset:
                raise ValueError('Invalid asset name')
            archive = downloads / asset
            if 'asset_id' in bundle:
                command = ['gh', 'api', 'repos/' + lock['repository'] + '/releases/assets/' + str(int(bundle['asset_id'])), '-H', 'Accept: application/octet-stream']
                with archive.open('wb') as target:
                    subprocess.run(command, stdout=target, check=True)
            else:
                subprocess.run(['gh', 'release', 'download', lock['tag'], '--repo', lock['repository'], '--pattern', asset, '--dir', str(downloads)], check=True)
            if archive.stat().st_size != bundle['bytes'] or digest(archive) != bundle['sha256']:
                raise ValueError('Release asset checksum mismatch: ' + asset)
            unpack(archive, staged / name, bundle)
        (staged / 'receipt.json').write_text(json.dumps({'schema': 1, 'repository': lock['repository'], 'tag': lock['tag'], 'model': lock['model'], 'lock_sha256': digest(lock_path), 'bundles': selected, 'device_tested': False}, indent=2) + '\n')
        verify_cache(staged, lock_path, selected)
        os.rename(staged, output)
    artifacts = verify_cache(output, lock_path, selected)
    (output / 'artifact-map.json').write_text(json.dumps(artifacts, indent=2) + '\n')
    return artifacts

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['fetch', 'verify'])
    parser.add_argument('--lock', type=Path, default=DEFAULT_LOCK)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--bundle', action='append')
    args = parser.parse_args()
    if args.command == 'fetch':
        if args.output is None:
            parser.error('fetch requires --output')
        artifacts = fetch(args.lock, args.output, args.bundle)
    else:
        if args.cache is None:
            parser.error('verify requires --cache')
        artifacts = verify_cache(args.cache, args.lock, args.bundle)
    print(json.dumps({'files_verified': len(artifacts), 'device_tested': False}))

if __name__ == '__main__':
    main()
