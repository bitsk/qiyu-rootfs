#!/system/bin/sh
# Run once as Android root after the verified foreign-stage archive is extracted.
set -eu
r=/mnt/osstore/systems/debian/rootfs
[ -x "$r/debootstrap/debootstrap" ]
printf 'https://deb.debian.org/debian\n' > "$r/debootstrap/mirror"
printf 'deb https://deb.debian.org/debian trixie main\n' > "$r/etc/apt/sources.list"
# The host cannot create device nodes; finish this foreign-stage setup natively.
for spec in 'null 1 3' 'zero 1 5' 'full 1 7' 'random 1 8' 'urandom 1 9' 'tty 5 0' 'console 5 1'; do
    set -- $spec
    [ -e "$r/dev/$1" ] || mknod "$r/dev/$1" c "$2" "$3"
    chmod 666 "$r/dev/$1"
done
ln -sfn /proc/self/fd "$r/dev/fd"
ln -sfn /proc/self/fd/0 "$r/dev/stdin"
ln -sfn /proc/self/fd/1 "$r/dev/stdout"
ln -sfn /proc/self/fd/2 "$r/dev/stderr"
ln -sfn pts/ptmx "$r/dev/ptmx"
mkdir -p "$r/proc" "$r/sys" "$r/dev/pts" "$r/run"
mount -t proc proc "$r/proc"
mount -t sysfs -o ro sysfs "$r/sys"
mount -t devpts -o newinstance,ptmxmode=0666 devpts "$r/dev/pts"
mount -t tmpfs -o mode=0755 tmpfs "$r/run"
cleanup() {
    umount "$r/run" 2>/dev/null || true
    umount "$r/dev/pts" 2>/dev/null || true
    umount "$r/sys" 2>/dev/null || true
    umount "$r/proc" 2>/dev/null || true
}
trap cleanup EXIT
printf '#!/bin/sh\nexit 101\n' > "$r/usr/sbin/policy-rc.d"
chmod 755 "$r/usr/sbin/policy-rc.d"
chroot "$r" /usr/bin/env PATH=/usr/sbin:/usr/bin:/sbin:/bin DEBIAN_FRONTEND=noninteractive /debootstrap/debootstrap --second-stage
chroot "$r" /usr/bin/dpkg --audit
rm -f "$r/usr/sbin/policy-rc.d"
sync
echo DEBIAN_SECOND_STAGE_COMPLETE
