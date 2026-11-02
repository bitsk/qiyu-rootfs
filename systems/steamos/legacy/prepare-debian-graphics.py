#!/usr/bin/env python3
"""Copy private A081 Mesa/Weston runtimes from retained Debian into SteamOS.

Run on native SteamOS. Source Debian is read-only to this tool; no mounts,
services or devices are opened. /usr and the system loader remain unchanged.
"""
import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess

DEBIAN = Path('/osstore/systems/debian/rootfs')
OUT = Path('/opt/mix-steamos-graphics')
MULTIARCH = '/usr/lib/aarch64-linux-gnu'
GLIBC = re.compile(r'^(ld-linux|lib(c|m|pthread|dl|rt|resolv|anl|util|BrokenLocale)\.so\.|libnss_)')
COPIES = {}
HOST_LIBS = {}
SEEN = set()
PENDING = deque()


def resolve_guest(path):
    """Resolve even absolute symlinks inside Debian, never against host /."""
    pending = deque(PurePosixPath('/' + str(path).lstrip('/')).parts[1:])
    parts = []
    links = 0
    while pending:
        part = pending.popleft()
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                raise ValueError('Source path escapes Debian root')
            parts.pop()
            continue
        trial = DEBIAN.joinpath(*parts, part)
        if trial.is_symlink():
            links += 1
            if links > 40:
                raise ValueError('Too many source symlinks')
            target = PurePosixPath(os.readlink(trial))
            if target.is_absolute():
                parts = []
                target_parts = target.parts[1:]
            else:
                target_parts = target.parts
            pending.extendleft(reversed(target_parts))
        else:
            parts.append(part)
    return DEBIAN.joinpath(*parts)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def elf(path):
    with path.open('rb') as stream:
        header = stream.read(20)
    return header[:6] == b'\x7fELF\x02\x01' and header[18:20] == b'\xb7\x00'


def copy(path, area, alias=None):
    source = resolve_guest(path)
    if not source.is_file():
        raise RuntimeError(f'Missing Debian runtime input: {path}')
    name = alias or PurePosixPath(path).name
    target = OUT / area / name
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = sha(source)
    if target.exists():
        if sha(target) != digest:
            raise RuntimeError(f'Conflicting library name: {target}')
    else:
        shutil.copy2(source, target)
    COPIES[str(target.relative_to(OUT))] = {'source': str(source), 'sha256': digest}
    if elf(source):
        PENDING.append((str(source.relative_to(DEBIAN)), area))
    return target


def dependencies(source):
    # --list resolves DT_NEEDED without executing the application or its
    # constructors. No /dev, /proc or host libraries are bound into Debian.
    result = subprocess.run(['chroot', str(DEBIAN), '/lib/ld-linux-aarch64.so.1',
                             '--list', '/' + source.lstrip('/')], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode or 'not found' in result.stdout:
        raise RuntimeError(f'Debian dependency resolution failed for {source}:\n{result.stdout}')
    result_paths = []
    for line in result.stdout.splitlines():
        match = re.match(r'\s*(\S+) => (/\S+) \(', line)
        if match:
            result_paths.append((match[1], match[2]))
    return result_paths


def close_dependencies():
    while PENDING:
        source, area = PENDING.popleft()
        key = (source, area)
        if key in SEEN:
            continue
        SEEN.add(key)
        for soname, path in dependencies(source):
            if GLIBC.match(soname):
                continue
            # Keep Frame's newer C++ runtime, Qt/Wayland and common libraries
            # for KWin. Only absent SONAMEs (such as libLLVM-19) are imported.
            if area in ('mesa', 'dri', 'gbm', 'mesa-extra'):
                if soname in HOST_LIBS:
                    continue
                copy(path, 'mesa-extra', soname)
            else:
                if soname in ('libstdc++.so.6', 'libgcc_s.so.1') and soname in HOST_LIBS:
                    continue
                copy(path, 'weston-lib', soname)


def put(name, content):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--with-weston', action='store_true', help='Also package Weston14 and its KMS/GLES modules')
    args = parser.parse_args()
    if os.geteuid() != 0 or platform.machine() != 'aarch64':
        raise SystemExit('Run as root on native A081 SteamOS')
    if Path('/etc/a081-rootfs-id').read_text().strip() != 'a081-steamos-arm64':
        raise SystemExit('Expected independently booted A081 SteamOS')
    if DEBIAN.resolve() != DEBIAN or resolve_guest('/etc/a081-rootfs-id').read_text().strip() != 'a081-debian-trixie-arm64':
        raise SystemExit('Retained Debian identity mismatch')
    if OUT.exists():
        raise SystemExit(f'{OUT} exists; preserve this candidate before building another')
    output = subprocess.check_output(['ldconfig', '-p'], text=True)
    for line in output.splitlines():
        match = re.match(r'\s*(\S+) \([^)]*\) => (/\S+)', line)
        if match:
            HOST_LIBS[match[1]] = match[2]
    OUT.mkdir()
    for directory in ('mesa', 'mesa-extra', 'dri', 'gbm', 'bin', 'share', 'logs'):
        (OUT / directory).mkdir()
    seeds = ['libgbm.so.1', 'libEGL_mesa.so.0', 'libGLX_mesa.so.0']
    for name in seeds:
        copy(MULTIARCH + '/' + name, 'mesa')
    # libgbm loads this backend with dlopen; mixing Frame's plugin with Debian
    # libgbm silently reintroduces the incompatible Zink KMS path.
    copy(MULTIARCH + '/gbm/dri_gbm.so', 'gbm')
    for name in ('libglapi.so.0',):
        if resolve_guest(MULTIARCH + '/' + name).is_file():
            copy(MULTIARCH + '/' + name, 'mesa')
    # libdril dispatches to the versioned Gallium DSO at runtime: this edge is
    # not in DT_NEEDED and must be included explicitly.
    gallium = []
    for directory in (MULTIARCH, MULTIARCH + '/dri'):
        for path in resolve_guest(directory).glob('libgallium*.so*'):
            if path.is_file():
                gallium.append(path)
                copy('/' + str(path.relative_to(DEBIAN)), 'mesa')
    if not gallium:
        raise RuntimeError('Missing Debian Gallium runtime')
    for name in ('libdril_dri.so', 'msm_dri.so'):
        copy(MULTIARCH + '/dri/' + name, 'dri')
    # kmsro driver aliases share the same hardware dispatch DSO. Keep aliases
    # supplied by this exact Debian build; no software-renderer override.
    for name in ('msm-kms_dri.so', 'kmsro_dri.so'):
        if resolve_guest(MULTIARCH + '/dri/' + name).is_file():
            copy(MULTIARCH + '/dri/' + name, 'dri')
    close_dependencies()
    vendor = {'file_format_version': '1.0.0', 'ICD': {'library_path': str(OUT / 'mesa/libEGL_mesa.so.0')}}
    (OUT / 'share/50_mesa.json').write_text(json.dumps(vendor, indent=2) + '\n')
    # Frame's KWin has cap_sys_nice=ep: AT_SECURE discards LD_LIBRARY_PATH.
    # Keep /usr/bin unchanged and copy only its bytes, without file capabilities.
    # Logind still hands the ordinary user the seat's DRM descriptors.
    kwin = OUT / 'bin/kwin_wayland'
    shutil.copyfile('/usr/bin/kwin_wayland', kwin)
    kwin.chmod(0o755)
    if os.listxattr(kwin):
        raise RuntimeError('Unexpected xattrs on the private KWin executable')
    put('bin/with-mesa', '''#!/bin/bash
set -euo pipefail
prefix=$(dirname "$(dirname "$(readlink -f "$0")")")
[[ $# -gt 0 ]] || { echo 'Usage: with-mesa COMMAND [ARG...]' >&2; exit 2; }
unset MESA_LOADER_DRIVER_OVERRIDE LIBGL_ALWAYS_SOFTWARE
export LD_LIBRARY_PATH="$prefix/mesa:$prefix/mesa-extra"
export LIBGL_DRIVERS_PATH="$prefix/dri"
export GBM_BACKENDS_PATH="$prefix/gbm"
export __EGL_VENDOR_LIBRARY_FILENAMES="$prefix/share/50_mesa.json"
if [[ ${1##*/} == kwin_wayland ]]; then
    shift
    set -- "$prefix/bin/kwin_wayland" "$@"
fi
exec "$@"
''')
    if args.with_weston:
        copy('/usr/bin/weston', 'bin', 'weston.real')
        modules = []
        for subdir, names in (
                ('libweston-14', ('drm-backend.so', 'gl-renderer.so', 'xwayland.so')),
                ('weston', ('kiosk-shell.so',))):
            for name in names:
                target = copy(MULTIARCH + '/' + subdir + '/' + name, 'weston-modules')
                modules.append(f'{name}={target}')
        close_dependencies()
        (OUT / 'share/weston-module-map').write_text(';'.join(modules) + '\n')
        # The kiosk shell has no external desktop-shell process or assets.
        # Xwayland is optional; launch it separately with Frame's environment
        # if used, so Debian's private Weston dependencies cannot leak into it.
        put('bin/weston-private', '''#!/bin/bash
set -euo pipefail
prefix=$(dirname "$(dirname "$(readlink -f "$0")")")
unset MESA_LOADER_DRIVER_OVERRIDE LIBGL_ALWAYS_SOFTWARE
export LD_LIBRARY_PATH="$prefix/mesa:$prefix/mesa-extra:$prefix/weston-lib"
export LIBGL_DRIVERS_PATH="$prefix/dri"
export GBM_BACKENDS_PATH="$prefix/gbm"
export __EGL_VENDOR_LIBRARY_FILENAMES="$prefix/share/50_mesa.json"
export WESTON_MODULE_MAP="$(cat "$prefix/share/weston-module-map")"
exec "$prefix/bin/weston.real" "$@"
''')
    # Verify load-time closure using the SteamOS loader, without opening a DRM
    # device. This checks both executable and plugin dependencies, not pixels.
    library_path = ':'.join(str(OUT / p) for p in ('mesa', 'mesa-extra', 'weston-lib'))
    checks = {}
    for relative in sorted(COPIES):
        target = OUT / relative
        if not elf(target):
            continue
        result = subprocess.run(['/lib/ld-linux-aarch64.so.1', '--library-path', library_path,
                                 '--list', str(target)], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        checks[relative] = {'returncode': result.returncode, 'output': result.stdout}
        if result.returncode or 'not found' in result.stdout:
            (OUT / 'logs/loader-checks.json').write_text(json.dumps(checks, indent=2))
            raise RuntimeError(f'SteamOS loader cannot resolve {relative}; see logs/loader-checks.json')
    (OUT / 'logs/loader-checks.json').write_text(json.dumps(checks, indent=2) + '\n')
    packages = subprocess.check_output(['chroot', str(DEBIAN), 'dpkg-query', '-W',
                                       'libgbm1', 'libegl-mesa0', 'libgl1-mesa-dri', 'weston'], text=True)
    (OUT / 'manifest.json').write_text(json.dumps({
        'status': 'private_runtime_prepared_no_graphics_trial', 'source': str(DEBIAN),
        'debian_packages': packages.splitlines(), 'files': COPIES,
        'glibc_source': 'SteamOS host; no Debian glibc copied',
        'kwin_sha256': sha(kwin), 'kwin_file_capabilities': False,
        'host_sonames': HOST_LIBS, 'with_weston': args.with_weston,
        'device_accessed': False, 'hardware_rendering_verified': False}, indent=2) + '\n')
    print(json.dumps({'status': 'prepared', 'prefix': str(OUT), 'file_count': len(COPIES),
                      'hardware_rendering_verified': False}))


if __name__ == '__main__':
    main()
