#!/usr/bin/env python3
"""Build a complete, metadata-preserving SteamOS userspace archive without mounts.

Run with dissect.btrfs 1.10 and backports.zstd installed; the zstd CLI compresses
the output. Only a new output directory is written. No device, mount, firmware,
boot, package-manager or service operation is performed.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tarfile
import zipfile
import zlib

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CHUNK = 4 * 1024 * 1024


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def gpt_header(raw, offset, block_size=512):
    values = struct.unpack_from("<8sIIIIQQQQ16sQIII", raw, offset)
    if values[0] != b"EFI PART" or values[1] != 0x10000 or not 92 <= values[2] <= block_size:
        raise ValueError("Unsupported GPT header")
    header = bytearray(raw[offset:offset + values[2]])
    header[16:20] = bytes(4)
    if zlib.crc32(header) != values[3]:
        raise ValueError("GPT header CRC mismatch")
    if values[12] < 128 or values[12] % 8 or not 1 <= values[11] <= 4096:
        raise ValueError("Invalid GPT entry array")
    return values


def extract_partition(archive, output, profile):
    if digest(archive) != profile["archive_sha256"]:
        raise ValueError("Recovery archive checksum mismatch")
    print("Recovery ZIP SHA-256 verified", flush=True)
    with zipfile.ZipFile(archive) as source:
        item = source.getinfo(profile["archive_member"])
        if item.file_size != profile["image_bytes"] or item.is_dir():
            raise ValueError("Unexpected recovery image member")
        with source.open(item) as stream:
            initial = stream.read(CHUNK)
            header = gpt_header(initial, 512)
            if header[5] != 1 or header[6] != item.file_size // 512 - 1:
                raise ValueError("GPT disk bounds mismatch")
            start = header[10] * 512
            length = header[11] * header[12]
            table = initial[start:start + length]
            if len(table) != length or zlib.crc32(table) != header[13]:
                raise ValueError("GPT entry CRC mismatch")
            partitions = []
            for index in range(header[11]):
                entry = table[index * header[12]:(index + 1) * header[12]]
                if not any(entry[:16]):
                    continue
                first, last = struct.unpack_from("<QQ", entry, 32)
                name = entry[56:128].decode("utf-16-le").rstrip("\0")
                if not header[7] <= first <= last <= header[8]:
                    raise ValueError("Partition outside usable GPT range")
                partitions.append(dict(index=index + 1, name=name, first_lba=first,
                                       last_lba=last, offset=first * 512,
                                       bytes=(last - first + 1) * 512))
            ordered = sorted(partitions, key=lambda p: p["first_lba"])
            if any(a["last_lba"] >= b["first_lba"] for a, b in zip(ordered, ordered[1:])):
                raise ValueError("Overlapping GPT partitions")
            selected = [p for p in partitions if p["name"] == profile["partition"]]
            if len(selected) != 1:
                raise ValueError("Recovery must contain exactly one selected root partition")
            partition = selected[0]
            lo, hi = partition["offset"], partition["offset"] + partition["bytes"]
            raw_hash, partition_hash = hashlib.sha256(), hashlib.sha256()
            position, written = 0, 0
            tail = b""
            with output.open("xb") as target:
                chunk = initial
                while chunk:
                    raw_hash.update(chunk)
                    tail = (tail + chunk)[-1024 * 1024:]
                    begin, end = max(lo - position, 0), min(hi - position, len(chunk))
                    if end > begin:
                        data = chunk[begin:end]
                        target.write(data)
                        partition_hash.update(data)
                        written += len(data)
                    position += len(chunk)
                    chunk = stream.read(CHUNK)
            if position != item.file_size or raw_hash.hexdigest() != profile["image_sha256"]:
                raise ValueError("Whole recovery image hash/size mismatch")
            if written != partition["bytes"] or partition_hash.hexdigest() != profile["partition_sha256"]:
                raise ValueError("Root partition hash/size mismatch")
            backup_offset = header[6] * 512 - (position - len(tail))
            backup = gpt_header(tail, backup_offset)
            if backup[5] != header[6] or backup[6] != 1 or backup[7:10] != header[7:10] or backup[11:14] != header[11:14]:
                raise ValueError("Backup GPT does not match primary")
            table_offset = backup[10] * 512 - (position - len(tail))
            if tail[table_offset:table_offset + length] != table:
                raise ValueError("Backup GPT entries differ")
    print("Full image, both GPT copies and rootfs-A SHA-256 verified", flush=True)
    return {"partitions": partitions, "selected": partition,
            "gpt_primary_and_backup_verified": True,
            "zip_crc_verified": True, "image_sha256": raw_hash.hexdigest(),
            "partition_sha256": partition_hash.hexdigest()}


def xattrs(node):
    from dissect.btrfs.c_btrfs import c_btrfs
    attrs = {}
    for _, data in node.subvolume.tree.cursor().iter(
            node.inum, c_btrfs.BTRFS_XATTR_ITEM_KEY, 0, ignore_offset=True):
        offset = 0
        while offset < len(data):
            item = c_btrfs.btrfs_dir_item(data[offset:])
            size = 30 + item.name_len + item.data_len
            if offset + size > len(data):
                raise ValueError("Truncated Btrfs extended attribute")
            name = bytes(item.name).decode("utf-8", "surrogateescape")
            value = bytes(item.data).decode("utf-8", "surrogateescape")
            attrs["SCHILY.xattr." + name] = value
            offset += size
    return attrs


def make_tar(image, output, inventory_path):
    from dissect.btrfs import Btrfs
    counts = {"files": 0, "directories": 0, "symlinks": 0, "hardlinks": 0,
              "special_files": 0, "xattrs": 0, "file_bytes": 0}
    inodes = {}
    with image.open("rb") as stream, output.open("xb") as compressed, inventory_path.open("x") as inventory:
        fs = Btrfs(stream)
        for required in ("/usr/lib/os-release", "/usr/lib/ld-linux-aarch64.so.1",
                         "/opt/steamvr/bin/linuxarm64/vrserver", "/usr/bin/bash"):
            if not fs.get(required).is_file():
                raise ValueError("Missing required SteamOS file: " + required)
        compressor = subprocess.Popen(["zstd", "-T4", "-3", "-q", "-c"],
                                      stdin=subprocess.PIPE, stdout=compressed)
        try:
            with tarfile.open(fileobj=compressor.stdin, mode="w|", format=tarfile.PAX_FORMAT,
                              encoding="utf-8", errors="surrogateescape") as tar:
                def add(node, name):
                    info = tarfile.TarInfo(name)
                    info.mode = stat.S_IMODE(node.mode)
                    info.uid, info.gid = node.uid, node.gid
                    info.mtime = node.mtime_ns // 1000000000
                    info.pax_headers = xattrs(node)
                    counts["xattrs"] += len(info.pax_headers)
                    record = {"path": name, "mode": oct(node.mode), "uid": node.uid,
                              "gid": node.gid, "xattrs": list(info.pax_headers)}
                    key = (node.subvolume.objectid, node.inum)
                    if node.is_dir():
                        info.type = tarfile.DIRTYPE
                        counts["directories"] += 1
                    elif node.is_symlink():
                        info.type, info.linkname = tarfile.SYMTYPE, node.link
                        record["target"] = node.link
                        counts["symlinks"] += 1
                    elif node.is_file():
                        if key in inodes:
                            info.type, info.linkname = tarfile.LNKTYPE, inodes[key]
                            record["target"] = info.linkname
                            counts["hardlinks"] += 1
                        else:
                            inodes[key] = name
                            info.size = node.size
                            counts["files"] += 1
                            counts["file_bytes"] += node.size
                            record["bytes"] = node.size
                    elif node.is_device():
                        info.type = tarfile.CHRTYPE if node.is_character_device() else tarfile.BLKTYPE
                        info.devmajor, info.devminor = os.major(node.inode.rdev), os.minor(node.inode.rdev)
                        counts["special_files"] += 1
                    elif node.is_fifo():
                        info.type = tarfile.FIFOTYPE
                        counts["special_files"] += 1
                    else:
                        raise ValueError("Unsupported filesystem inode: " + name)
                    if node.is_file() and info.type != tarfile.LNKTYPE:
                        with node.open() as contents:
                            tar.addfile(info, contents)
                    else:
                        tar.addfile(info)
                    inventory.write(json.dumps(record, ensure_ascii=True) + "\n")
                    total = sum(counts[k] for k in ("files", "directories", "symlinks", "hardlinks", "special_files"))
                    if total % 10000 == 0:
                        print(f"Archived {total} entries ({counts['file_bytes']} file bytes)", flush=True)
                    if node.is_dir():
                        for child_name, child in sorted(node.iterdir(), key=lambda pair: pair[0]):
                            if child_name in (".", ".."):
                                continue
                            if "/" in child_name or "\0" in child_name:
                                raise ValueError("Unsafe filesystem entry")
                            add(child, child_name if name == "." else name + "/" + child_name)
                add(fs.root, ".")
            compressor.stdin.close()
            if compressor.wait() != 0:
                raise ValueError("zstd compression failed")
        except BaseException:
            compressor.stdin.close()
            compressor.terminate()
            compressor.wait()
            raise
        return {**counts, "filesystem": "btrfs", "filesystem_uuid": str(fs.uuid),
                "filesystem_label": fs.label, "default_subvolume": fs.default_subvolume.objectid}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=HERE / "profile.json")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text())
    archive = (args.archive or REPO / profile["archive"]).resolve()
    if not archive.is_file() or args.output.exists() or args.output.is_symlink():
        parser.error("Require a regular source archive and a new output directory")
    args.output.mkdir(parents=True)
    (args.output / "prepare-source.py").write_bytes(Path(__file__).read_bytes())
    (args.output / "profile-source.json").write_bytes(args.profile.read_bytes())
    receipt = {"schema": 1, "status": "building", "profile": profile,
               "device_actions": [], "hardware_verified": False, "bootable_image": False,
               "scope": "Complete original SteamOS ARM64 userspace for isolated deployment",
               "builder_sha256": digest(Path(__file__).resolve()),
               "builder_source": "prepare-source.py",
               "profile_source": "profile-source.json",
               "dependencies": {name: importlib.metadata.version(name) for name in
                                ("dissect.btrfs", "dissect.cstruct", "dissect.util", "backports.zstd")}}
    manifest = args.output / "manifest.json"
    try:
        image = args.output / "rootfs-A.img"
        receipt["source_verification"] = extract_partition(archive, image, profile)
        root_tar = args.output / "rootfs.tar.zst"
        receipt["filesystem"] = make_tar(image, root_tar, args.output / "files.jsonl")
        subprocess.run(["zstd", "-q", "-t", str(root_tar)], check=True)
        receipt["artifacts"] = {name: {"sha256": digest(args.output / name),
                                      "bytes": (args.output / name).stat().st_size}
                                for name in ("rootfs-A.img", "rootfs.tar.zst", "files.jsonl")}
        receipt["status"] = "prepared"
        receipt["install_note"] = "Extract as root with GNU tar --zstd --numeric-owner --same-owner --same-permissions --xattrs --xattrs-include='*' --xattrs-exclude='btrfs.*' into a new isolated directory; apply MIX boot/service overrides before booting."
    except BaseException as exc:
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        manifest.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "manifest": str(manifest),
                      "archive": receipt["artifacts"]["rootfs.tar.zst"]}, indent=2))


if __name__ == "__main__":
    main()
