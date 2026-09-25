"""Repair release text dates and rehash only torrent pieces containing the NFO.

Run with the pinned BDRip Python (torf). Movie bytes and MD5 files never change.
"""

import hashlib
import re
from pathlib import Path


def dated_text(content, value):
    from datetime import date

    date.fromisoformat(value)
    pattern = rb"(?im)^(.*?RELEASE DATE[. ]*:[ \t]*(?:\[/font\][ \t]*)?)(\d{4}-\d{2}-\d{2}|Unknown)"
    updated, count = re.subn(pattern, lambda m: m[1] + value.encode("ascii"), content)
    if count != 1:
        raise ValueError(f"Expected one RELEASE DATE field, found {count}")
    return updated


def piece_bytes(files, piece_size, index, replacements):
    start, end = index * piece_size, (index + 1) * piece_size
    data = bytearray()
    offset = 0
    for path, length in files:
        left, right = max(start, offset), min(end, offset + length)
        if left < right:
            if path in replacements:
                block = replacements[path][left - offset : right - offset]
            else:
                if path.stat().st_size != length:
                    raise ValueError(f"Torrent payload size mismatch: {path.name}")
                with path.open("rb") as stream:
                    stream.seek(left - offset)
                    block = stream.read(right - left)
            if len(block) != right - left:
                raise ValueError("Incomplete torrent piece")
            data.extend(block)
        offset += length
    return bytes(data)


def revised_torrent(torrent_path, package, replacements, *, donors=None):
    from torf import Torrent

    torrent = Torrent.read(torrent_path)
    info = torrent.metainfo["info"]
    files, affected, offset = [], set(), 0
    for item in info["files"]:
        relative = Path(*item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe torrent payload path")
        path = package / relative
        length = item["length"]
        if path in replacements:
            if len(replacements[path]) != length:
                raise ValueError("Date correction must preserve NFO byte length")
            affected.update(
                range(offset // torrent.piece_size, (offset + length - 1) // torrent.piece_size + 1)
            )
        resolved = (donors or {}).get(path, path)
        files.append((resolved, length))
        offset += length
    if not affected:
        raise ValueError("The changed NFO is missing from the torrent")
    pieces = bytearray(info["pieces"])
    for index in sorted(affected):
        old = hashlib.sha1(piece_bytes(files, torrent.piece_size, index, {})).digest()
        if old != pieces[index * 20 : (index + 1) * 20]:
            raise ValueError("Original torrent piece does not match its payload; refusing to patch")
        new = hashlib.sha1(piece_bytes(files, torrent.piece_size, index, replacements)).digest()
        pieces[index * 20 : (index + 1) * 20] = new
    info["pieces"] = bytes(pieces)
    torrent.validate()
    return torrent, sorted(affected)
