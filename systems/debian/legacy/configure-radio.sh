#!/bin/sh
# Run natively inside the installed Debian, after installing the supplied packages.
set -eu
[ "$(cat /etc/a081-rootfs-id)" = a081-debian-trixie-arm64 ]
install -d -m 0755 /etc/systemd/network /etc/wpa_supplicant /etc/udev/rules.d
install -m 0644 /osstore/updates/debian-radio-r1/10-a081-wlan.link /etc/systemd/network/
install -m 0644 /osstore/updates/debian-radio-r1/20-a081-wlan.network /etc/systemd/network/
install -m 0644 /osstore/updates/debian-radio-r1/80-a081-wlan.rules /etc/udev/rules.d/
install -m 0600 /osstore/updates/debian-radio-r1/private/wpa.conf /etc/wpa_supplicant/wpa_supplicant-wlan0.conf
ln -sfn /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
systemctl enable systemd-networkd.service systemd-resolved.service systemd-timesyncd.service bluetooth.service
systemctl disable wpa_supplicant@wlan0.service
systemctl disable systemd-networkd-wait-online.service
systemctl daemon-reload
udevadm control --reload
systemctl restart systemd-networkd.service systemd-resolved.service systemd-timesyncd.service bluetooth.service
if [ -d /sys/class/net/wlan0 ]; then systemctl start wpa_supplicant@wlan0.service; fi
# The .link file applies on the next boot, when the new radio modules load.
echo A081_DEBIAN_RADIO_CONFIGURED
