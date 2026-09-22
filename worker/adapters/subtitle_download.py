"""Bounded public-HTTP downloads; websites and archives never choose local paths."""

import http.client
import ipaddress
import socket
import ssl
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

FORMATS = {".srt", ".ass", ".ssa", ".sup"}
MAX_BYTES = 128 * 1024 * 1024


def public_url(url):
    parts = urlsplit(url)
    if (
        len(url) > 4096
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 80, 443)
        or any(ord(c) < 32 for c in url)
    ):
        raise ValueError("Subtitle URL must be a public HTTP(S) address without credentials")
    return parts


def download(url, path, check=lambda: None):
    deadline = time.monotonic() + 120
    for _ in range(6):
        check()
        parts = public_url(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("Subtitle downloads cannot access private or local network addresses")
        # Pin the checked address while retaining the original HTTP Host and TLS SNI.
        address = addresses[0][4][0]
        connection = (
            http.client.HTTPSConnection(
                parts.hostname, port, timeout=15, context=ssl.create_default_context()
            )
            if parts.scheme == "https"
            else http.client.HTTPConnection(parts.hostname, port, timeout=15)
        )
        connection._create_connection = lambda target, timeout, source_address=None: socket.create_connection(
            (address, port), timeout, source_address
        )
        try:
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            connection.request(
                "GET",
                target,
                headers={"User-Agent": "BDRipAgent subtitle discovery", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Subtitle redirect has no destination")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(
                    f"Subtitle download returned HTTP {response.status}; manual access may be needed"
                )
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > MAX_BYTES):
                raise ValueError("Subtitle download exceeds 128 MB")
            total = 0
            with path.open("wb") as output:
                while True:
                    check()
                    if time.monotonic() > deadline:
                        raise TimeoutError("Subtitle download exceeded two minutes")
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("Subtitle download exceeds 128 MB")
                    output.write(chunk)
            if not total:
                raise ValueError("Subtitle download is empty")
            return {"url": url, "content_type": response.getheader("Content-Type", ""), "bytes": total}
        except http.client.HTTPException as error:
            raise ValueError(f"Subtitle server returned an invalid HTTP response: {error}") from error
        finally:
            connection.close()
    raise ValueError("Too many subtitle download redirects")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if Path(urlsplit(href).path).suffix.lower() in FORMATS | {".zip"}:
                self.urls.append(href)


def unpack(path, directory, hint):
    """Return local files and original member names; never extract an archive tree."""
    if zipfile.is_zipfile(path):
        result = []
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 100 or sum(m.file_size for m in members) > 256 * 1024 * 1024:
                raise ValueError("Subtitle archive is too large")
            for member in members:
                name = PurePosixPath(member.filename)
                suffix = name.suffix.lower()
                if member.is_dir() or suffix not in FORMATS:
                    continue
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or "\\" in member.filename
                    or member.flag_bits & 1
                ):
                    raise ValueError("Subtitle archive contains unsafe or encrypted members")
                limit = MAX_BYTES if suffix == ".sup" else 16 * 1024 * 1024
                if member.file_size > limit or member.file_size > max(1, member.compress_size) * 250:
                    raise ValueError("Subtitle archive member exceeds extraction limits")
                if len(result) >= 12:
                    break
                output = directory / f"member-{len(result)}{suffix}"
                output.write_bytes(archive.read(member))
                result.append((output, name.name))
        if not result:
            raise ValueError("Archive contains no supported subtitles")
        return sorted(result, key=lambda item: item[0].suffix == ".sup")
    raw = path.read_bytes()
    suffix = Path(unquote(urlsplit(hint).path)).suffix.lower()
    if raw.startswith(b"PG"):
        suffix = ".sup"
    elif b"[Events]" in raw[:16384] or b"[Script Info]" in raw[:16384]:
        suffix = ".ass"
    elif b"-->" in raw[:16384] or raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        suffix = ".srt" if suffix not in (".ass", ".ssa") else suffix
    else:
        raise ValueError("Download is not SRT, ASS, PGS or a supported ZIP archive")
    output = directory / ("download" + suffix)
    output.write_bytes(raw)
    return [(output, Path(unquote(urlsplit(hint).path)).name or output.name)]


def fetch_candidate(candidate, directory, check):
    directory.mkdir(parents=True, exist_ok=True)
    url = candidate.download_url or candidate.source_url
    path = directory / "response.bin"
    receipt = download(url, path, check)
    if "html" in receipt["content_type"] or path.read_bytes()[:256].lstrip().lower().startswith(
        (b"<!doctype", b"<html")
    ):
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Subtitle page is too large; provide a direct download")
        links = Links()
        links.feed(path.read_text(errors="replace"))
        if not links.urls:
            raise ValueError(
                "No public subtitle download found on this page; login or manual download may be required"
            )
        # Bounded one-page link resolution. The agent still reviews every resulting file.
        files = []
        failures = []
        for index, href in enumerate(list(dict.fromkeys(links.urls))[:3]):
            folder = directory / f"link-{index}"
            folder.mkdir(exist_ok=True)
            target = folder / "response.bin"
            try:
                linked = download(urljoin(receipt["url"], href), target, check)
                files.extend((file, name, linked) for file, name in unpack(target, folder, linked["url"]))
            except (ValueError, OSError) as error:
                check()
                failures.append(str(error))
        if not files:
            raise ValueError("No usable download links on subtitle page: " + "; ".join(failures))
        return files
    return [(file, name, receipt) for file, name in unpack(path, directory, receipt["url"])]
