"""Inspect or change HoYoPlay's existing disableHttp2 setting (Windows, stdlib)."""

import argparse
import csv
from datetime import datetime
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib


def require(condition, message):
    if not condition:
        raise ValueError(message)


def varint(data, position, end):
    value = 0
    for shift in range(0, 70, 7):
        require(position < end, "Truncated MMKV varint")
        byte = data[position]
        position += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError("Invalid MMKV varint")


def inspect(data, meta):
    require(len(data) >= 4 and len(meta) >= 112, "Configuration file is too short")
    size = struct.unpack_from("<I", data)[0]
    crc, version, sequence = struct.unpack_from("<III", meta)
    require(version == 4, "Unsupported MMKV metadata version; refusing to modify")
    require(0 < size <= len(data) - 4, "Invalid MMKV data size")
    require(struct.unpack_from("<I", meta, 28)[0] == size, "Metadata size mismatch")
    require(meta[12:28] == bytes(16), "Encrypted configuration is unsupported")
    require(struct.unpack_from("<Q", meta, 104)[0] == 0, "Unsupported MMKV flags")
    require(zlib.crc32(data[4:4 + size]) == crc, "CRC mismatch; refusing to modify")
    end = 4 + size
    _, position = varint(data, 4, end)  # MMKV's initial map-length placeholder.
    values, locations = {}, {}
    while position < end:
        key_length, position = varint(data, position, end)
        require(0 < key_length <= end - position, "Invalid MMKV key length")
        key = data[position:position + key_length].decode("utf-8")
        position += key_length
        length, position = varint(data, position, end)
        require(length <= end - position, "Invalid MMKV value length")
        values[key] = bytes(data[position:position + length])
        locations[key] = (position, length)  # Last record wins for duplicate keys.
        position += length
    require(position == end, "MMKV parsing did not reach the expected end")
    require(values.get("disableHttp2") in (b"\x00", b"\x01"),
            "Existing disableHttp2 boolean was not found")
    return size, sequence, values, locations


def prepare(data, meta, disabled):
    size, sequence, before, locations = inspect(data, meta)
    offset, length = locations["disableHttp2"]
    require(length == 1, "Unsupported disableHttp2 value length")
    changed_data, changed_meta = bytearray(data), bytearray(meta)
    changed_data[offset] = int(disabled)
    crc = zlib.crc32(changed_data[4:4 + size])
    require(sequence < 0xffffffff, "MMKV sequence overflow")
    struct.pack_into("<I", changed_meta, 0, crc)
    struct.pack_into("<I", changed_meta, 8, sequence + 1)
    struct.pack_into("<II", changed_meta, 32, size, crc)
    _, _, after, _ = inspect(changed_data, changed_meta)
    require({key for key in before if before[key] != after[key]} == {"disableHttp2"},
            "Unexpected settings change")
    require(sum(a != b for a, b in zip(data, changed_data)) == 1,
            "Expected exactly one changed data byte")
    return bytes(changed_data), bytes(changed_meta)


def ensure_closed():
    require(os.name == "nt", "Writing is supported only on Windows")
    result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                            capture_output=True, check=True)
    rows = csv.reader(io.StringIO(result.stdout.decode("utf-8", errors="replace")))
    names = {row[0].lower() for row in rows if row}
    require(not names.intersection({"hyp.exe", "hyphelper.exe"}),
            "Exit HoYoPlay from the system tray before writing; HYP is still running")


def output(value):
    print(json.dumps(value, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=["true", "false"],
                        help="true disables HTTP/2; false restores HTTP/2")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing")
    parser.add_argument("--data-dir", type=Path, help="Directory containing the MMKV file pair")
    parser.add_argument("--backup-root", type=Path,
                        default=Path(__file__).resolve().parent / "backups")
    args = parser.parse_args()
    if args.dry_run and args.set is None:
        parser.error("--dry-run requires --set")
    if args.data_dir is None:
        require(bool(os.environ.get("APPDATA")), "APPDATA is unavailable; use --data-dir")
        args.data_dir = Path(os.environ["APPDATA"]) / "miHoYo/HYP/1_1/data"
    path = args.data_dir / "usersettings.dat"
    crc_path = args.data_dir / "usersettings.dat.crc"
    original, original_meta = path.read_bytes(), crc_path.read_bytes()
    _, sequence, before, _ = inspect(original, original_meta)
    limit = before.get("enableDownloadSpeedLimit")
    output({"data_dir": str(args.data_dir.resolve()), "disableHttp2": before["disableHttp2"] == b"\x01",
            "downloadLimitEnabled": limit == b"\x01" if limit in (b"\x00", b"\x01") else None,
            "crc_valid": True, "sequence": sequence})
    if args.set is None:
        return
    disabled = args.set == "true"
    if before["disableHttp2"] == bytes([int(disabled)]):
        output({"status": "already_set", "written": False})
        return
    data, meta = prepare(original, original_meta, disabled)
    output({"changed_key": "disableHttp2", "new_value": disabled,
            "data_bytes_changed": 1, "dry_run": args.dry_run})
    if args.dry_run:
        return
    ensure_closed()
    require(path.read_bytes() == original and crc_path.read_bytes() == original_meta,
            "Configuration changed during inspection; retry after exiting HoYoPlay")
    args.backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"),
                                 dir=args.backup_root))
    (backup / path.name).write_bytes(original)
    (backup / crc_path.name).write_bytes(original_meta)
    (backup / "change.json").write_text(json.dumps({
        "time": datetime.now().astimezone().isoformat(), "data_dir": str(args.data_dir.resolve()),
        "before_disableHttp2": not disabled, "after_disableHttp2": disabled,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    require((backup / path.name).read_bytes() == original and
            (backup / crc_path.name).read_bytes() == original_meta, "Backup verification failed")
    ensure_closed()
    # Acquire both files before writing either. A write failure restores the original pair.
    with path.open("r+b") as stream, crc_path.open("r+b") as crc_stream:
        require(stream.read() == original and crc_stream.read() == original_meta,
                "Configuration changed before writing; retry")
        try:
            for target, content in [(stream, data), (crc_stream, meta)]:
                target.seek(0)
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            require(path.read_bytes() == data and crc_path.read_bytes() == meta,
                    "Written files do not match expected contents")
            inspect(path.read_bytes(), crc_path.read_bytes())
        except BaseException:
            for target, content in [(stream, original), (crc_stream, original_meta)]:
                target.seek(0)
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            raise
    output({"status": "applied_and_verified", "backup_dir": str(backup.resolve()),
            "restart_required": True})


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
