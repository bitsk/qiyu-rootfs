#!/usr/bin/env python3
"""Apply the A081 board overlay to a staged SteamOS root, from installed Debian.

This script only updates /osstore/systems/steamos/rootfs. It neither writes a
partition nor changes the Debian installation. It must run natively on A081.
"""
import fnmatch
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import uuid

ROOT = Path('/osstore/systems/steamos/rootfs')
BACKUP = ROOT / 'var/lib/mix-steamos/vendor-backup'
STATE = ROOT / 'var/lib/mix-steamos'
COPIED = []
MASKED = []


def destination(name):
    path = ROOT / name.lstrip('/')
    # Imported absolute leaf symlinks are replaced below; never traverse one
    # in an ancestor and accidentally write into the running Debian system.
    parent = path.parent
    while parent != ROOT:
        if parent.is_symlink():
            raise RuntimeError(f'Symlink ancestor in guest path: {parent}')
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def preserve(name):
    src = destination(name)
    if not src.exists() and not src.is_symlink():
        return
    dst = BACKUP / name.lstrip('/')
    if dst.exists() or dst.is_symlink():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def write(name, text, mode=0o644):
    path = destination(name)
    if path.is_symlink():
        path.unlink()
    path.write_text(text)
    path.chmod(mode)


def link(name, target):
    path = destination(name)
    remove(path)
    path.symlink_to(target)


def copy_host(name, target=None, required=True):
    src = Path(name)
    if not src.exists() and not src.is_symlink():
        if required:
            raise RuntimeError(f'Missing Debian board input: {src}')
        return
    dst = destination(target or name)
    if src.is_dir() and not src.is_symlink():
        remove(dst)
        shutil.copytree(src, dst, symlinks=True)
    else:
        remove(dst)
        shutil.copy2(src, dst, follow_symlinks=False)
    COPIED.append(str(src))


def mask(unit, user=False):
    area = 'user' if user else 'system'
    link(f'/etc/systemd/{area}/{unit}', '/dev/null')
    MASKED.append(f'{area}/{unit}')


def enable(unit, target='multi-user.target'):
    local = ROOT / 'etc/systemd/system' / unit
    source = '/etc/systemd/system/' if local.exists() else '/usr/lib/systemd/system/'
    if source.startswith('/usr') and not (ROOT / source.lstrip('/') / unit).exists():
        raise RuntimeError(f'Missing guest service: {unit}')
    link(f'/etc/systemd/system/{target}.wants/{unit}', source + unit)


def main():
    if os.geteuid() != 0 or platform.machine() != 'aarch64':
        raise SystemExit('Run as root on native A081 Debian')
    if Path('/etc/a081-rootfs-id').read_text().strip() != 'a081-debian-trixie-arm64':
        raise SystemExit('Expected the installed A081 Debian host')
    if b'A081' not in Path('/proc/device-tree/model').read_bytes():
        raise SystemExit('Device-tree identity mismatch')
    if ROOT.resolve() != ROOT or not (ROOT / 'usr/lib/systemd/systemd').is_file():
        raise SystemExit('Extract the complete rootfs into the fixed SteamOS path first')
    if 'ID=steamos' not in (ROOT / 'usr/lib/os-release').read_text():
        raise SystemExit('Staged root is not SteamOS')
    kernel = platform.release()
    if not Path('/usr/lib/modules', kernel).is_dir():
        raise SystemExit('Running Debian kernel has no matching installed modules')
    marker = ROOT / 'etc/a081-rootfs-id'
    if marker.exists():
        raise SystemExit('Overlay already configured; preserve this rootfs and use a new extracted root for a rebuild')
    BACKUP.mkdir(parents=True, exist_ok=True)
    for name in ('/etc/systemd', '/etc/fstab', '/etc/crypttab', '/etc/hostname',
                 '/etc/machine-id', '/etc/shadow', '/etc/resolv.conf', '/etc/udev/rules.d',
                 '/etc/modules-load.d', '/etc/modprobe.d', '/etc/sysctl.d'):
        preserve(name)
    # Factory enablement and drop-ins reference Frame devices and partition
    # layouts. Retain the vendor unit definitions, but replace their enablement.
    remove(ROOT / 'etc/systemd')
    (ROOT / 'etc/systemd/system').mkdir(parents=True)
    (ROOT / 'etc/systemd/user').mkdir(parents=True)
    write('/etc/fstab', '# A081: root and /osstore are mounted by the recovery initramfs.\n')
    write('/etc/crypttab', '# No Frame partition activation on A081.\n')
    write('/etc/hostname', 'a081-steamos\n')
    link('/etc/localtime', '/usr/share/zoneinfo/Asia/Hong_Kong')
    write('/etc/machine-id', uuid.uuid4().hex + '\n', 0o444)
    link('/var/lib/dbus/machine-id', '/etc/machine-id')
    write('/etc/motd', 'SteamOS ARM64 on Qiyu MIX A081. Local USB root maintenance console.\n')
    link('/etc/systemd/system/default.target', '/usr/lib/systemd/system/multi-user.target')

    patterns = ('deckard*', 'steamos*', 'steam*', 'atomupd*', 'holo-*', 'adbd*', 'usb-*',
                'NetworkManager*', 'softap*', 'dsp_service*', 'iris-*', 'gpu-trace*',
                'setubootenv*', 'healthd*', 'fwupd*', 'rauc*', 'fancontrol*', 'mkinitcpio*',
                'systemd-repart*', 'systemd-bootctl*', 'systemd-boot-update*',
                'systemd-sysupdate*', 'system-update*', 'ostree*', 'sshd*',
                'systemd-pcr*', 'systemd-tpm*')
    for unit in (ROOT / 'usr/lib/systemd/system').iterdir():
        if not unit.name.endswith(('.service', '.socket', '.target', '.timer', '.path', '.mount', '.automount')):
            continue
        if any(fnmatch.fnmatch(unit.name, pattern) for pattern in patterns):
            mask(unit.name)
    for unit in ('etc.mount', 'home.mount', 'efi.mount', 'esp.mount', 'persist.mount',
                 'root.mount', 'srv.mount', 'var-cache-pacman.mount', 'var-lib-containers.mount',
                 'var-lib-docker.mount', 'var-lib-flatpak.mount', 'var-lib-systemd-coredump.mount',
                 'var-lib-steamos\\x2dlog\\x2dsubmitter.mount', 'var-log.mount', 'var-tmp.mount',
                 'var-lib-machines.mount', 'display-manager.service', 'sddm.service',
                 'dev-usb\\x2dffs-adb.mount', 'systemd-networkd-wait-online.service',
                 'systemd-binfmt.service', 'proc-sys-fs-binfmt_misc.mount',
                 'proc-sys-fs-binfmt_misc.automount', 'systemd-hibernate-resume.service',
                 'systemd-boot-random-seed.service',
                 'systemd-hibernate-clear.service', 'hibernate.target', 'hybrid-sleep.target',
                 'suspend-then-hibernate.target', 'suspend.target'):
        mask(unit)
    for unit in (ROOT / 'usr/lib/systemd/user').glob('*'):
        if unit.name.startswith(('steam', 'deckard', 'gamescope')) and not unit.is_dir():
            mask(unit.name, user=True)
    allowed_generators = {'systemd-fstab-generator', 'systemd-getty-generator', 'systemd-debug-generator'}
    for generator in (ROOT / 'usr/lib/systemd/system-generators').iterdir():
        if generator.name not in allowed_generators:
            link('/etc/systemd/system-generators/' + generator.name, '/dev/null')
    # These hooks execute outside the service enablement graph. Keep originals
    # in the guest backup, and remove them from systemd's executable hook path.
    for directory in ('usr/lib/systemd/system-sleep', 'usr/lib/systemd/system-shutdown'):
        preserve('/' + directory)
        remove(ROOT / directory)
        (ROOT / directory).mkdir(parents=True)

    dangerous_rules = ('*steamos*', '*deckard*', '*steamvr*', '*eeprom*', '*hwmon-max*',
                       '*exp5*', '*gpiod*', '*spidev*', '*rgb-cam*', '*cdsp*',
                       '*typec-plug*', '*autosuspend*', '*mac-addresses*', '*brltty*',
                       '*sdcard-rescan*', '*vlv-wobt*', '*inputplumber*')
    for rule in (ROOT / 'usr/lib/udev/rules.d').glob('*.rules'):
        content = rule.read_text(errors='replace')
        if (any(fnmatch.fnmatch(rule.name, p) for p in dangerous_rules)
                or any(token in content for token in ('setubootenv', 'fw_setenv', 'atomupd',
                       '/usr/lib/deckard', '/usr/share/deckard', '/usr/lib/steamos'))):
            link('/etc/udev/rules.d/' + rule.name, '/dev/null')
    for folder in ('modules-load.d', 'sysctl.d', 'tmpfiles.d'):
        for config in (ROOT / 'usr/lib' / folder).glob('*.conf'):
            if folder == 'modules-load.d' or any(s in config.name for s in ('steamos', 'deckard', 'provision', 'gpu')):
                link('/etc/' + folder + '/' + config.name, '/dev/null')
    for folder in ('modules-load.d', 'modprobe.d', 'sysctl.d'):
        # Original /etc is backed up. Use only the board's accepted overrides.
        remove(ROOT / 'etc' / folder)
        (ROOT / 'etc' / folder).mkdir()
        for config in (ROOT / 'usr/lib' / folder).glob('*.conf'):
            if folder == 'modules-load.d' or any(s in config.name for s in ('steamos', 'deckard', 'gpu')):
                link('/etc/' + folder + '/' + config.name, '/dev/null')
        for config in (Path('/etc') / folder).glob('*a081*'):
            copy_host(str(config))

    # Only this running board kernel and its firmware are imported. No Frame
    # kernel, DTB, bootloader, FPGA image or update program is installed to MIX.
    preserve('/usr/lib/modules')
    remove(ROOT / 'usr/lib/modules')
    (ROOT / 'usr/lib/modules').mkdir()
    copy_host('/usr/lib/modules/' + kernel)
    for name in ('qcom', 'ath11k', 'qca'):
        preserve('/usr/lib/firmware/' + name)
        copy_host('/usr/lib/firmware/' + name, required=(name == 'qcom'))
    for source in Path('/usr/lib/firmware').glob('regulatory.db*'):
        copy_host(str(source))
    copy_host('/usr/share/qcom')
    copy_host('/usr/local/bin/hexagonrpcd')
    for source in Path('/usr/local/lib').glob('libhexagonrpc*'):
        copy_host(str(source))
    write('/etc/ld.so.conf.d/a081-hexagonrpc.conf', '/usr/local/lib\n')
    copy_host('/etc/systemd/system/a081-sensors-files.service')
    sensors = ROOT / 'etc/systemd/system/a081-sensors-files.service'
    sensors.write_text(sensors.read_text().replace('/usr/sbin/modprobe', '/usr/bin/modprobe'))
    copy_host('/etc/udev/rules.d/80-a081-sensors.rules', required=False)
    copy_host('/usr/share/alsa/ucm2/Qualcomm/sm8250/a081', required=False)
    copy_host('/etc/systemd/system/a081-usb-console.service')
    console = ROOT / 'etc/systemd/system/a081-usb-console.service'
    console.write_text(console.read_text().replace('A081-Debian', 'A081-SteamOS'))

    # Wi-Fi credentials remain on-device and are never included in the report.
    for name in ('10-a081-wlan.link', '20-a081-wlan.network'):
        copy_host('/etc/systemd/network/' + name)
    copy_host('/etc/wpa_supplicant/wpa_supplicant-wlan0.conf')
    (ROOT / 'etc/wpa_supplicant/wpa_supplicant-wlan0.conf').chmod(0o600)
    write('/etc/systemd/system/a081-wpa@.service', '''[Unit]
Description=A081 wireless authentication on %i
BindsTo=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device
Before=network.target
[Service]
ExecStart=/usr/bin/wpa_supplicant -i%i -c/etc/wpa_supplicant/wpa_supplicant-%i.conf
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
''')
    write('/etc/udev/rules.d/80-a081-wlan.rules',
          'ACTION=="add", SUBSYSTEM=="net", DRIVERS=="ath11k_pci", TAG+="systemd", ENV{SYSTEMD_WANTS}+="a081-wpa@%k.service"\n')
    link('/etc/resolv.conf', '/run/systemd/resolve/stub-resolv.conf')
    link('/etc/systemd/system/dbus-org.freedesktop.resolve1.service', '/usr/lib/systemd/system/systemd-resolved.service')
    link('/etc/systemd/system/dbus-org.freedesktop.timesync1.service', '/usr/lib/systemd/system/systemd-timesyncd.service')
    for unit in ('a081-usb-console.service', 'a081-sensors-files.service',
                 'systemd-networkd.service', 'systemd-resolved.service', 'systemd-timesyncd.service'):
        enable(unit)
    enable('systemd-networkd.socket', 'sockets.target')
    # Logind's Frame overrides and platform power handlers are not appropriate
    # for bring-up. The maintenance session remains accessible on USB.
    write('/etc/systemd/logind.conf.d/a081.conf', '[Login]\nHandlePowerKey=ignore\nHandleSuspendKey=ignore\nHandleLidSwitch=ignore\n')
    write('/etc/systemd/journald.conf.d/a081.conf', '[Journal]\nStorage=persistent\nSystemMaxUse=128M\nRuntimeMaxUse=32M\n')

    account = next(line.split(':') for line in (ROOT / 'etc/passwd').read_text().splitlines() if line.startswith('steamos:'))
    if account[2:4] != ['1000', '1000'] or account[5] != '/home/steamos':
        raise RuntimeError('Unexpected factory Steam user identity')
    steam_home = ROOT / 'home/steamos'
    steam_home.mkdir(parents=True, exist_ok=True)
    steam_home.chmod(0o700)
    os.chown(steam_home, 1000, 1000)
    shadow = ROOT / 'etc/shadow'
    entries = []
    for line in shadow.read_text().splitlines():
        fields = line.split(':')
        if fields[0] in ('root', 'steamos'):
            fields[1] = '!'
        entries.append(':'.join(fields))
    write('/etc/shadow', '\n'.join(entries) + '\n', 0o600)
    guest_launcher = Path(__file__).resolve().with_name('guest-session.sh')
    if not guest_launcher.is_file():
        raise RuntimeError('Deploy guest-session.sh beside this configurator')
    shutil.copy2(guest_launcher, destination('/usr/local/bin/mix-steam'))
    (ROOT / 'usr/local/bin/mix-steam').chmod(0o755)
    # Calls below only rebuild the guest's caches/accounts; no guest service is
    # started and /dev is not bind-mounted into the staged root.
    subprocess.run(['chroot', str(ROOT), '/usr/bin/ldconfig'], check=True)
    subprocess.run(['chroot', str(ROOT), '/usr/bin/systemd-sysusers'], check=True)
    dependencies = subprocess.run(
        ['chroot', str(ROOT), '/usr/bin/ldd', '/usr/local/bin/hexagonrpcd'],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout
    (STATE / 'hexagonrpcd-libraries.txt').write_text(dependencies)
    if 'not found' in dependencies:
        raise RuntimeError('The SteamOS root is missing an imported sensor service dependency; see hexagonrpcd-libraries.txt')
    report = {'status': 'a081_overlay_configured_not_boot_verified', 'kernel': kernel,
              'rootfs': str(ROOT), 'configured_unix_time': int(time.time()),
              'copied_host_paths': COPIED, 'masked_units': sorted(set(MASKED)),
              'hardware_tested': False, 'boot_partition_writes': False,
              'steam_launch': 'runuser -u steamos -- /usr/local/bin/mix-steam desktop'}
    (STATE / 'configuration.json').write_text(json.dumps(report, indent=2) + '\n')
    write('/etc/a081-rootfs-id', 'a081-steamos-arm64\n')
    print(json.dumps({'status': report['status'], 'rootfs': str(ROOT), 'kernel': kernel,
                      'masked_unit_count': len(set(MASKED)), 'hardware_tested': False}))


if __name__ == '__main__':
    main()
