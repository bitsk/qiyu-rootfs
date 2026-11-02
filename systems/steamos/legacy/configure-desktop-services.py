#!/usr/bin/env python3
"""Migrate native A081 SteamOS networking and enable audio for the next session.

Run as root inside the installed SteamOS. Debian is read only. Credentials never
leave this machine or appear in subprocess arguments/output. Existing originals
are retained under /var/lib/mix-steamos/desktop-services-backup (root only).
"""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


STATE = Path('/var/lib/mix-steamos/desktop-services-backup')
DEBIAN = Path('/osstore/systems/debian/rootfs')
PROFILE = Path('/etc/NetworkManager/system-connections/a081-imported-wlan0.nmconnection')
OWNER = '# Managed by mix-steamos configure-desktop-services.py\n'
CONNECTION_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'a081-steamos/imported-wlan0'))
FRAME_AUDIO = ('10-access', '40-mic-processing', '50-alsa-config',
               '51-alsa-disable', '60-spatial-audio', '70-spatial-node-config')


def fail(message):
    raise RuntimeError(message)


def strip_comment(line):
    quoted = escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == '#' and not quoted:
            return line[:index].strip()
    if quoted or escaped:
        fail('Unterminated quoted WPA value')
    return line.strip()


def quoted_bytes(value):
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        fail('Expected a quoted WPA value')
    out = bytearray()
    body = value[1:-1]
    index = 0
    escapes = {'"': b'"', '\\': b'\\', 'n': b'\n', 'r': b'\r', 't': b'\t'}
    while index < len(body):
        char = body[index]
        index += 1
        if char == '"':
            fail('Unexpected quote in WPA value')
        if char != '\\':
            out.extend(char.encode('utf-8'))
            continue
        if index == len(body):
            fail('Incomplete WPA escape')
        escape = body[index]
        index += 1
        if escape in escapes:
            out.extend(escapes[escape])
        elif escape == 'x' and re.fullmatch(r'[0-9a-fA-F]{2}', body[index:index + 2]):
            out.append(int(body[index:index + 2], 16))
            index += 2
        else:
            fail('Unsupported WPA escape')
    return bytes(out)


def parse_wpa(text):
    """Accept one enabled WPA-PSK network; reject unsupported semantics early."""
    networks = []
    network = None
    for raw in text.splitlines():
        line = strip_comment(raw)
        if not line:
            continue
        if re.fullmatch(r'network\s*=\s*\{', line):
            if network is not None:
                fail('Nested WPA networks are unsupported')
            network = {}
            continue
        if line == '}':
            if network is None:
                fail('Unmatched WPA network delimiter')
            networks.append(network)
            network = None
            continue
        match = re.fullmatch(r'([a-zA-Z0-9_]+)\s*=\s*(.+)', line)
        if not match:
            fail('Unsupported WPA configuration syntax')
        key, value = match.groups()
        if network is None:
            if key not in ('ctrl_interface', 'update_config', 'country', 'ap_scan'):
                fail('Unsupported global WPA option')
            if key == 'ap_scan' and value != '1':
                fail('Only standard WPA scanning is supported')
            if key == 'country' and not re.fullmatch(r'[A-Z]{2}', value):
                fail('Invalid WPA regulatory country')
        else:
            if key not in ('ssid', 'psk', 'key_mgmt', 'scan_ssid', 'priority', 'disabled'):
                fail('Unsupported WPA network option')
            if key in network:
                fail('Duplicate WPA network option')
            network[key] = value
    if network is not None or len(networks) != 1:
        fail('Exactly one complete saved WPA network is required')
    network = networks[0]
    if network.get('key_mgmt', 'WPA-PSK') != 'WPA-PSK' or network.get('disabled', '0') != '0':
        fail('Only an enabled WPA-PSK network is supported')
    value = network.get('ssid', '')
    if value.startswith('"'):
        ssid = quoted_bytes(value)
    elif re.fullmatch(r'(?:[0-9a-fA-F]{2}){1,32}', value):
        ssid = bytes.fromhex(value)
    else:
        fail('SSID must be quoted or 1 to 32 hexadecimal bytes')
    if not 1 <= len(ssid) <= 32:
        fail('SSID must contain 1 to 32 bytes')
    value = network.get('psk', '')
    if value.startswith('"'):
        password = quoted_bytes(value)
        if not 8 <= len(password) <= 63 or any(c < 32 or c > 126 for c in password):
            fail('WPA passphrase must contain 8 to 63 printable ASCII bytes')
        psk = password.decode('ascii')
    elif re.fullmatch(r'[0-9a-fA-F]{64}', value):
        psk = value.lower()
    else:
        fail('PSK must be a quoted passphrase or 64 hexadecimal digits')
    if network.get('scan_ssid', '0') not in ('0', '1'):
        fail('Invalid WPA hidden-network flag')
    priority = network.get('priority', '100')
    if not re.fullmatch(r'-?\d{1,4}', priority) or not -999 <= int(priority) <= 999:
        fail('WPA priority is outside the NetworkManager range')
    # GLib keyfile escaping, including leading/trailing spaces in passphrases.
    escaped_psk = ''.join('\\\\' if c == '\\' else '\\s' if c == ' ' else c for c in psk)
    return (OWNER + '[connection]\nid=A081 saved Wi-Fi\nuuid=' + CONNECTION_UUID
            + '\ntype=wifi\ninterface-name=wlan0\nautoconnect=true\nautoconnect-priority='
            + str(int(priority)) + '\n\n[wifi]\nmode=infrastructure\nssid='
            + ''.join(str(c) + ';' for c in ssid) + '\nhidden='
            + ('true' if network.get('scan_ssid') == '1' else 'false')
            + '\ncloned-mac-address=permanent\npowersave=2\n\n[wifi-security]\n'
              'key-mgmt=wpa-psk\npsk-flags=0\npsk=' + escaped_psk
            + '\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n')


def check_parents(path):
    for parent in path.parents:
        if parent.is_symlink():
            fail('Refusing a symlink ancestor at ' + str(parent))
        if parent.exists() and not parent.is_dir():
            fail('Expected a directory at ' + str(parent))


def run(*args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Service diagnostics may contain network identifiers; leave them in the
        # native journal instead of copying them into the maintenance transcript.
        fail('Command failed: ' + args[0] + ' ' + args[1] + '; inspect the native journal')


def main():
    if os.geteuid() != 0 or platform.machine() != 'aarch64':
        fail('Run as root on native A081 SteamOS')
    if Path('/etc/a081-rootfs-id').read_text().strip() != 'a081-steamos-arm64':
        fail('Expected the independent A081 SteamOS root')
    if b'A081' not in Path('/proc/device-tree/model').read_bytes():
        fail('Expected the A081 device tree')
    if (DEBIAN / 'etc/a081-rootfs-id').read_text().strip() != 'a081-debian-trixie-arm64':
        fail('The retained Debian root is required for the UCM link')
    source_wpa = Path('/etc/wpa_supplicant/wpa_supplicant-wlan0.conf')
    if source_wpa.is_symlink() or not source_wpa.is_file():
        fail('Expected the saved regular WPA configuration file')
    profile = parse_wpa(source_wpa.read_text())
    if PROFILE.is_symlink() or (PROFILE.exists() and not PROFILE.read_text().startswith(OWNER)):
        fail('Refusing to replace an unrelated NetworkManager profile')

    ucm = Path('/usr/share/alsa/ucm2/conf.d/sm8250/Qiyu MIX WCD9385 DMIC.conf')
    ucm_target = '../../Qualcomm/sm8250/a081/A081.conf'
    source_ucm = DEBIAN / str(ucm).lstrip('/')
    if not source_ucm.is_symlink():
        fail('Retained Debian UCM selection must be a symlink')
    source_target = os.readlink(source_ucm)
    target_namespace = os.path.normpath(os.path.join(str(ucm.parent), source_target))
    expected_namespace = '/usr/share/alsa/ucm2/Qualcomm/sm8250/a081/A081.conf'
    if target_namespace != expected_namespace or not Path(expected_namespace).is_file():
        fail('Expected the existing A081 UCM profile and matching Debian link')
    if ucm.exists() or ucm.is_symlink():
        if not ucm.is_symlink() or os.readlink(ucm) != ucm_target:
            fail('Refusing to replace an unrelated ALSA UCM selection')

    # Every item is validated before backup, service stop or configuration write.
    # Tuples are (operation, destination, content/target, permissions).
    edits = [('file', PROFILE, profile, 0o600),
             ('file', Path('/etc/modules-load.d/a081-steamos.conf'), 'fuse\n', 0o644)]
    config = '[main]\ndns=systemd-resolved\n\n[device-a081-wlan0]\nmatch-device=interface-name:wlan0\nmanaged=1\nwifi.backend=wpa_supplicant\n'
    edits += [('file', Path('/etc/NetworkManager/conf.d/90-a081.conf'), config, 0o644),
              ('link', ucm, ucm_target, None)]
    system = Path('/etc/systemd/system')
    for unit in ('systemd-networkd.service', 'systemd-networkd.socket',
                 'systemd-networkd-wait-online.service', 'a081-wpa@.service',
                 'NetworkManager-dispatcher.service', 'dbus-org.freedesktop.nm-dispatcher.service',
                 'NetworkManager-wait-online.service'):
        edits.append(('link', system / unit, '/dev/null', None))
    edits.append(('link', Path('/etc/udev/rules.d/80-a081-wlan.rules'), '/dev/null', None))
    for unit in ('NetworkManager.service', 'wpa_supplicant.service',
                 'dbus-fi.w1.wpa_supplicant1.service'):
        for directory in (system, Path('/run/systemd/system')):
            path = directory / unit
            if path.is_symlink() and os.readlink(path) == '/dev/null':
                edits.append(('remove', path, None, None))
    edits.append(('link', system / 'multi-user.target.wants/NetworkManager.service',
                  '/usr/lib/systemd/system/NetworkManager.service', None))
    # These are the exact global links created by the vendor units' [Install]
    # sections. Do not start or restart the current user audio session.
    user = Path('/etc/systemd/user')
    for unit in ('pipewire.socket', 'pipewire-pulse.socket'):
        edits.append(('link', user / ('sockets.target.wants/' + unit),
                      '/usr/lib/systemd/user/' + unit, None))
    for name in ('pipewire.service.wants/wireplumber.service', 'pipewire-session-manager.service'):
        edits.append(('link', user / name, '/usr/lib/systemd/user/wireplumber.service', None))
    edits.append(('file', user / 'wireplumber.service.d/wireplumber-steamvr.conf',
                  '# A081: use upstream WirePlumber ordering; no SteamVR dependency.\n', 0o644))
    for name in FRAME_AUDIO:
        edits.append(('remove', Path('/etc/wireplumber/wireplumber.conf.d') / (name + '.conf'), None, None))

    for unit in ('NetworkManager.service', 'wpa_supplicant.service'):
        if not (Path('/usr/lib/systemd/system') / unit).is_file():
            fail('Missing native system unit: ' + unit)
    for unit in ('pipewire.socket', 'pipewire-pulse.socket', 'wireplumber.service'):
        if not (Path('/usr/lib/systemd/user') / unit).is_file():
            fail('Missing native audio unit: ' + unit)
        masked = user / unit
        if masked.is_symlink() and os.readlink(masked) == '/dev/null':
            edits.append(('remove', masked, None, None))
    for operation, path, content, mode in edits:
        check_parents(path)
        if path.exists() and path.is_dir() and not path.is_symlink():
            fail('Refusing to replace directory: ' + str(path))
    check_parents(STATE / 'manifest.json')
    if STATE.is_symlink():
        fail('Refusing a symlink backup directory')

    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    manifest_path = STATE / 'manifest.json'
    if manifest_path.is_symlink():
        fail('Refusing a symlink backup manifest')
    backups = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for operation, path, content, mode in edits:
        key = str(path)
        if key in backups:
            continue
        saved = STATE / 'originals' / key.lstrip('/')
        if path.exists() or path.is_symlink():
            check_parents(saved)
            saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if saved.exists() or saved.is_symlink():
                fail('Unrecorded backup already exists: ' + str(saved))
            shutil.copy2(path, saved, follow_symlinks=False)
            backups[key] = {'present': True, 'symlink': path.is_symlink()}
        else:
            backups[key] = {'present': False}
    manifest_path.write_text(json.dumps(backups, indent=2) + '\n')
    manifest_path.chmod(0o600)
    os.sync()
    for operation, path, content, mode in edits:
        path.parent.mkdir(parents=True, exist_ok=True)
        if operation == 'remove':
            path.unlink(missing_ok=True)
        elif operation == 'link':
            if path.is_symlink() and os.readlink(path) == content:
                continue
            path.unlink(missing_ok=True)
            path.symlink_to(content)
        else:
            descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
            try:
                os.fchmod(descriptor, mode)
                with os.fdopen(descriptor, 'w') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    # Stop the two previous owners before NM obtains wlan0. The USB console is
    # independent of networking and stays available if association needs work.
    run('systemctl', 'daemon-reload')
    for unit in ('a081-wpa@wlan0.service', 'systemd-networkd.socket', 'systemd-networkd.service'):
        status = subprocess.run(['systemctl', 'is-active', unit],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if status.stdout.strip() in ('active', 'activating', 'reloading', 'deactivating'):
            run('systemctl', 'stop', unit)
    run('udevadm', 'control', '--reload-rules')
    run('systemctl', 'start', 'NetworkManager.service')
    run('nmcli', 'connection', 'reload')
    os.sync()
    report = {'network_manager_started': True, 'imported_profiles': 1,
              'network_association_verified': False, 'audio_enabled_next_session': True,
              'frame_wireplumber_overrides_removed': list(FRAME_AUDIO),
              'debian_modified': False, 'backup': str(STATE)}
    (STATE / 'last-result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print('Desktop service configuration failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
