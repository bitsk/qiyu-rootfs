#!/system/bin/sh
set -eu
r=/mnt/osstore/systems/debian/rootfs
[ -x "$r/usr/lib/systemd/systemd" ]
[ ! -e "$r/etc/a081-rootfs-id" ]
[ ! -d "$r/debootstrap" ]
printf 'a081-debian\n' > "$r/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 a081-debian\n::1 localhost ip6-localhost ip6-loopback\n' > "$r/etc/hosts"
printf 'Asia/Hong_Kong\n' > "$r/etc/timezone"
ln -sfn /usr/share/zoneinfo/Asia/Hong_Kong "$r/etc/localtime"
printf 'LANG=C.UTF-8\n' > "$r/etc/default/locale"
printf '# Root and /osstore are mounted by the A081 initramfs.\n' > "$r/etc/fstab"
printf 'deb https://deb.debian.org/debian trixie main\ndeb https://deb.debian.org/debian trixie-updates main\ndeb https://security.debian.org/debian-security trixie-security main\' > "$r/etc/apt/sources.list"
printf 'nameserver 1.1.1.1\nnameserver 9.9.9.9\n' > "$r/etc/resolv.conf"
mkdir -p "$r/etc/systemd/system/multi-user.target.wants" "$r/etc/systemd/journald.conf.d" "$r/usr/local/sbin" "$r/osstore"
cp /data/local/tmp/a081-usb-console.service "$r/etc/systemd/system/"
cp /data/local/tmp/a081-first-boot-return.service "$r/etc/systemd/system/"
ln -sfn ../a081-usb-console.service "$r/etc/systemd/system/multi-user.target.wants/a081-usb-console.service"
ln -sfn ../a081-first-boot-return.service "$r/etc/systemd/system/multi-user.target.wants/a081-first-boot-return.service"
ln -sfn /usr/lib/systemd/system/multi-user.target "$r/etc/systemd/system/default.target"
printf '[Journal]\nStorage=persistent\nSystemMaxUse=64M\n' > "$r/etc/systemd/journald.conf.d/a081.conf"
printf '#!/bin/sh\nexec systemctl reboot\n' > "$r/usr/local/sbin/boot-android"
chmod 755 "$r/usr/local/sbin/boot-android"
printf 'Debian 13 arm64 on Qiyu MIX A081. USB console grants local root access.\nRun boot-android to return to stock Android.\n' > "$r/etc/motd"
printf 'a081-debian-trixie-arm64\n' > "$r/etc/a081-rootfs-id"
: > "$r/etc/machine-id"
# Network login stays disabled until an interface and credentials are configured.
chroot "$r" /usr/bin/env PATH=/usr/sbin:/usr/bin:/sbin:/bin SYSTEMD_OFFLINE=1 systemctl disable ssh.service ssh.socket
chroot "$r" /usr/bin/dpkg --audit
chroot "$r" /usr/bin/dpkg-query -W > /mnt/osstore/boot/debian-packages-installed.txt
dd if=/dev/block/by-name/recovery_b of=/mnt/osstore/boot/recovery-stock-b.bin bs=4194304 count=25 conv=fsync
test "$(sha256sum /mnt/osstore/boot/recovery-stock-b.bin | cut -d ' ' -f1)" = 26f7008561a66fe78ef19aeaf31f149b53672850eba60dee03e4565c7cadb1d1
: > /mnt/osstore/boot/debian-trial
sync
echo DEBIAN_ROOTFS_CONFIGURED
