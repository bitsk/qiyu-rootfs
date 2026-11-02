#!/usr/bin/env python3
"""Package the dual-root /init in the validated A081 container; no device access.

By default the validated kernel/modules are unchanged. --kernel-artifacts may
select a configuration-only SteamOS build from the exact proximity source.
DTB, recovery DTBO, firmware and boot load addresses always remain unchanged.
The recovery defaults to Debian; next-system=steamos selects one SteamOS boot.
"""
import argparse
import hashlib
import json
import lzma
from pathlib import Path
import shutil
import stat
import struct
import subprocess

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "artifacts/debian-proximity"
BASE_SHA256 = "57f94feb2e5ca02e9e50fda437dca7280f69ad5fb591a1d47f50766d80475e5e"


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def cpio_entries(raw):
    """Read newc in memory, preserving device nodes, ownership and link metadata."""
    entries = []
    offset = 0
    while True:
        if raw[offset:offset + 6] != b"070701":
            raise ValueError("Expected baseline newc initramfs")
        fields = [int(raw[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
        offset += 110
        name = raw[offset:offset + fields[11]]
        if not name.endswith(b"\0") or len(name) != fields[11]:
            raise ValueError("Invalid cpio filename")
        offset = (offset + fields[11] + 3) & ~3
        data = raw[offset:offset + fields[6]]
        if len(data) != fields[6]:
            raise ValueError("Truncated cpio content")
        offset = (offset + fields[6] + 3) & ~3
        entries.append((name, fields, data))
        if name == b"TRAILER!!!\0":
            if any(raw[offset:]):
                raise ValueError("Unexpected archive data after cpio trailer")
            return entries


def pack_cpio(entries):
    raw = bytearray()
    for name, original, data in entries:
        fields = original.copy()
        fields[6] = len(data)
        raw += b"070701" + b"".join(f"{v:08x}".encode() for v in fields) + name
        raw += bytes((-len(raw)) % 4)
        raw += data
        raw += bytes((-len(raw)) % 4)
    raw += bytes((-len(raw)) % 512)
    return bytes(raw)


def unpack(image, destination):
    output = subprocess.check_output([
        "unpack_bootimg", "--boot_img", str(image), "--out", str(destination),
        "--format", "mkbootimg", "-0",
    ])
    return [value.decode() for value in output.rstrip(b"\0").split(b"\0")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/steamos-mix/boot")
    parser.add_argument("--kernel-artifacts", type=Path,
                        help="Explicit Image/modules/manifest directory from build-kernel.sh")
    args = parser.parse_args()
    out = args.output.resolve()
    if args.kernel_artifacts and out == (ROOT / "artifacts/steamos-mix/boot").resolve():
        parser.error("A new kernel requires a distinct --output; preserve the baseline dual-root image")
    out.mkdir(parents=True, exist_ok=True)
    candidate = out / "recovery-steamos.bin"
    manifest_path = out / "manifest.json"
    if candidate.exists() or manifest_path.exists():
        raise SystemExit("Refusing to replace an existing candidate; choose a new --output directory")

    baseline_image = BASE / "recovery-debian.bin"
    baseline_manifest = json.loads((BASE / "manifest.json").read_text())
    if digest(baseline_image) != BASE_SHA256 or baseline_manifest["sha256"] != BASE_SHA256:
        raise ValueError("Validated recovery baseline hash mismatch")
    for name, key in [("Image", "kernel_sha256"), ("kernel.config", "kernel_config_sha256"),
                      ("initramfs.cpio.xz", "ramdisk_sha256"), ("modules.tar.gz", "modules_sha256")]:
        if digest(BASE / name) != baseline_manifest[key]:
            raise ValueError(f"Baseline {name} hash mismatch")

    kernel_artifacts = args.kernel_artifacts.resolve() if args.kernel_artifacts else BASE
    kernel_manifest = baseline_manifest
    if args.kernel_artifacts:
        kernel_manifest = json.loads((kernel_artifacts / "manifest.json").read_text())
        if (kernel_manifest["baseline_sha256"] != BASE_SHA256 or
            kernel_manifest["upstream_commit"] != "c03c1a14507dfaf8b856c2608f9ac216021fc222" or
            kernel_manifest["baseline_patch_sha256"] != json.loads((BASE / "kernel-source.json").read_text())["patch_sha256"] or
            kernel_manifest["source_edits"] or kernel_manifest["board_dts_edits"] or
            not kernel_manifest["all_generated_files_match_proximity"]):
            raise ValueError("Replacement kernel is not the configuration-only proximity candidate")
        for name, key in [("Image", "kernel_sha256"), ("kernel.config", "kernel_config_sha256"),
                          ("modules.tar.gz", "modules_sha256"), ("debian.dtb", "dtb_sha256")]:
            if digest(kernel_artifacts / name) != kernel_manifest[key]:
                raise ValueError(f"Replacement kernel artifact {name} hash mismatch")
        if (kernel_artifacts / "debian.dtb").read_bytes() != (BASE / "debian.dtb").read_bytes():
            raise ValueError("Replacement kernel must retain the exact validated hardware DTB")

    init = ROOT / "steamos-mix/init"
    subprocess.run(["sh", "-n", str(init)], check=True)
    baseline = out / "baseline"
    boot_args = unpack(baseline_image, baseline)
    if dict(zip(boot_args[::2], boot_args[1::2]))["--header_version"] != "2":
        raise ValueError("Expected the validated Android boot header v2")
    for name, source in [("kernel", "Image"), ("dtb", "debian.dtb"), ("ramdisk", "initramfs.cpio.xz")]:
        if (baseline / name).read_bytes() != (BASE / source).read_bytes():
            raise ValueError(f"Baseline boot component {name} differs from saved artifact")

    # Reconstruct the source container first to verify that all boot metadata,
    # offsets and padding from the existing image are retained by these tools.
    repacked = out / "baseline-repacked.img"
    subprocess.run(["mkbootimg", *boot_args, "--output", str(repacked)], check=True)
    source_bytes = baseline_image.read_bytes()
    original_repacked = repacked.read_bytes()
    if len(original_repacked) > len(source_bytes) or original_repacked + bytes(len(source_bytes) - len(original_repacked)) != source_bytes:
        raise ValueError("Source boot container does not reconstruct byte-for-byte")

    before = cpio_entries(lzma.decompress((baseline / "ramdisk").read_bytes()))
    after = []
    changed = []
    for name, fields, data in before:
        if name.rstrip(b"\0").lstrip(b"/") == b"init":
            if not stat.S_ISREG(fields[1]) or not fields[1] & 0o111:
                raise ValueError("Baseline /init is not an executable regular file")
            data = init.read_bytes()
            changed.append("/init")
        after.append((name, fields, data))
    if changed != ["/init"]:
        raise ValueError("Expected exactly one baseline /init")
    ramdisk = out / "initramfs.cpio.xz"
    ramdisk.write_bytes(lzma.compress(pack_cpio(after), format=lzma.FORMAT_XZ,
                                      check=lzma.CHECK_CRC32, preset=9))
    if ramdisk.stat().st_size >= 0xf00000:
        raise ValueError("Initramfs exceeds the validated ramdisk-to-DTB load window")
    checked = cpio_entries(lzma.decompress(ramdisk.read_bytes()))
    if len(checked) != len(before):
        raise ValueError("Initramfs entry count changed")
    for old, new in zip(before, checked):
        if old[0].rstrip(b"\0").lstrip(b"/") == b"init":
            fields = old[1].copy()
            fields[6] = len(init.read_bytes())
            if new != (old[0], fields, init.read_bytes()):
                raise ValueError("New /init failed metadata/content roundtrip")
        elif old != new:
            raise ValueError("An initramfs entry other than /init changed")

    new_args = boot_args.copy()
    new_args[new_args.index("--ramdisk") + 1] = str(ramdisk)
    selected_kernel = kernel_artifacts / "Image"
    new_args[new_args.index("--kernel") + 1] = str(selected_kernel)
    image = out / "recovery-steamos.img"
    subprocess.run(["mkbootimg", *new_args, "--output", str(image)], check=True)
    check = out / "image-check"
    unpack(image, check)
    for name in ["dtb", "recovery_dtbo"]:
        if (check / name).read_bytes() != (baseline / name).read_bytes():
            raise ValueError(f"Candidate {name} changed")
    if (check / "kernel").read_bytes() != selected_kernel.read_bytes():
        raise ValueError("Candidate kernel did not roundtrip")
    if (check / "ramdisk").read_bytes() != ramdisk.read_bytes():
        raise ValueError("Candidate initramfs did not roundtrip")
    data = image.read_bytes()
    original_header, new_header = bytearray(source_bytes[:4096]), bytearray(data[:4096])
    # Only payload lengths, the content digest and subsequent DTBO offset vary.
    variable_fields = [(16, 20), (576, 608), (1636, 1644)]
    if args.kernel_artifacts:
        variable_fields.append((8, 12))
    for start, end in variable_fields:
        original_header[start:end] = new_header[start:end] = bytes(end - start)
    if original_header != new_header:
        raise ValueError("Unexpected boot header change")
    kernel_memory = struct.unpack_from("<Q", selected_kernel.read_bytes(), 16)[0]
    if 0x80000 + kernel_memory > 0x1000000 or len(data) >= 96 * 1024 * 1024:
        raise ValueError("Candidate exceeds the validated kernel/boot-image bounds")
    candidate.write_bytes(data + bytes(len(source_bytes) - len(data)))

    fallback = out / "recovery-debian-fallback.bin"
    modules = out / "modules.tar.gz"
    shutil.copyfile(baseline_image, fallback)
    shutil.copyfile(kernel_artifacts / "modules.tar.gz", modules)
    if digest(fallback) != BASE_SHA256 or digest(modules) != kernel_manifest["modules_sha256"]:
        raise ValueError("Fallback image or matching modules copy mismatch")
    fallback_modules = out / "modules-debian-fallback.tar.gz"
    shutil.copyfile(BASE / "modules.tar.gz", fallback_modules)
    if digest(fallback_modules) != baseline_manifest["modules_sha256"]:
        raise ValueError("Fallback module archive mismatch")
    manifest = {
        "image": str(candidate.relative_to(ROOT)) if candidate.is_relative_to(ROOT) else str(candidate),
        "bytes": candidate.stat().st_size,
        "sha256": digest(candidate),
        "baseline_image": str(baseline_image.relative_to(ROOT)),
        "baseline_sha256": BASE_SHA256,
        "fallback_image": str(fallback),
        "fallback_sha256": digest(fallback),
        "fallback_modules_sha256": digest(fallback_modules),
        "kernel_release": kernel_manifest["kernel_release"],
        "kernel_artifacts": str(kernel_artifacts),
        "kernel_config_sha256": kernel_manifest["kernel_config_sha256"],
        "kernel_memory_bytes": kernel_memory,
        "kernel_sha256": digest(check / "kernel"),
        "dtb_sha256": digest(check / "dtb"),
        "recovery_dtbo_sha256": digest(check / "recovery_dtbo"),
        "modules_sha256": digest(modules),
        "ramdisk_sha256": digest(ramdisk),
        "init_sha256": digest(init),
        "build_script_sha256": digest(Path(__file__)),
        "ramdisk_replaced_entries": changed,
        "ramdisk_other_entries_unchanged": True,
        "kernel_dtb_modules_firmware_unchanged": args.kernel_artifacts is None,
        "kernel_modules_unchanged": args.kernel_artifacts is None,
        "hardware_dtb_firmware_unchanged": True,
        "configuration_only_kernel_change": args.kernel_artifacts is not None,
        "baseline_container_reconstructed_exactly": True,
        "roundtrip_verified": True,
        "boot_metadata_preserved": True,
        "selector": {
            "path": "/osstore/boot/next-system", "values": ["debian", "steamos"],
            "default": "debian", "one_shot": True,
            "consumed_before_steamos_validation": True,
            "steamos_root": "/osstore/systems/steamos/rootfs",
            "steamos_marker": "a081-steamos-arm64",
            "invalid_steamos_falls_back_to_debian": True,
        },
        "usb_serial": "A081-DEBIAN",
        "usb_identity_note": "Transport serial retained; inspect /etc/a081-rootfs-id and /run/a081-handoff for the selected OS",
        "device_accessed": False, "boot_tested": False,
        "baseline_promoted": False,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    names = [candidate, image, fallback, modules, fallback_modules, ramdisk, manifest_path]
    (out / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in names))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
