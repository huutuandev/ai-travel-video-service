import http.client
import ipaddress
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError

from app.core.config import settings
from app.core.exceptions import MediaError, RetryableExecutionError
from app.core.media_urls import validate_image_url
from app.storage.local import atomic_copy, upload_path

FORMATS = {
    "jpeg": ("image/jpeg", "image2"),
    "png": ("image/png", "png_pipe"),
    "webp": ("image/webp", "webp_pipe"),
    "mov": ("video/mp4", "mov"),
    "wav": ("audio/wav", "wav"),
    "mp3": ("audio/mpeg", "mp3"),
    "ogg": ("audio/ogg", "ogg"),
}
MOV_MIMES = {"video/mp4", "video/quicktime", "audio/mp4", "audio/x-m4a"}
IMAGE_TYPES = {"jpeg", "png", "webp"}


def identify(path: Path, content_type: str | None = None, *, decode_image=True) -> str:
    with path.open("rb") as stream:
        header = stream.read(32)
    if header.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif header.startswith(b"RIFF") and header[8:12] in (b"WEBP", b"WAVE"):
        kind = "webp" if header[8:12] == b"WEBP" else "wav"
    elif header[4:8] in (b"ftyp", b"moov", b"mdat", b"wide"):
        kind = "mov"
    elif header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
    ):
        kind = "mp3"
    elif header.startswith(b"OggS"):
        kind = "ogg"
    else:
        raise MediaError("INVALID_ASSET", "Unsupported or invalid media content")

    declared = (content_type or "").split(";", 1)[0].strip().lower()
    allowed = set(MOV_MIMES if kind == "mov" else (FORMATS[kind][0],))
    if kind == "wav":
        allowed.update(("audio/x-wav", "audio/wave"))
    if declared and declared not in allowed | {"application/octet-stream"}:
        raise MediaError("INVALID_ASSET", "Content-Type does not match media content")

    if kind in IMAGE_TYPES and decode_image:
        try:
            with Image.open(path) as image:
                if image.width * image.height > 24_000_000 or getattr(image, "is_animated", False):
                    raise MediaError("INVALID_ASSET", "Image size or animation is unsupported")
                image.verify()
            with Image.open(path) as image:
                image.load()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise MediaError("INVALID_ASSET", "Image cannot be decoded") from exc
    return kind


def bounded_copy(source, target: Path, max_bytes: int):
    total = 0
    with target.open("wb") as output:
        while chunk := source.read(65536):
            total += len(chunk)
            if total > max_bytes:
                raise MediaError("ASSET_TOO_LARGE", "Media exceeds the configured size limit")
            output.write(chunk)


def public_addresses(host: str) -> list[str]:
    addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    )))
    for value in addresses:
        ip = ipaddress.ip_address(value)
        if (
            not ip.is_global or ip.is_multicast or ip.is_reserved
            or getattr(ip, "ipv4_mapped", None)
        ):
            raise MediaError("UNSAFE_ASSET_URL", "Asset hostname resolves to a prohibited address")
        if isinstance(ip, ipaddress.IPv6Address) and (
            ip not in ipaddress.ip_network("2000::/3") or ip.sixtofour or ip.teredo
        ):
            raise MediaError("UNSAFE_ASSET_URL", "Asset hostname resolves to a prohibited address")
    if not addresses:
        raise MediaError("INVALID_ASSET", "Asset hostname has no addresses")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str):
        super().__init__(host, port=443, timeout=8, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        raw = socket.create_connection((self.ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def download(ref, target: Path, max_bytes: int) -> str:
    validate_image_url(ref)
    url = urlsplit(str(ref))
    try:
        addresses = public_addresses(url.hostname)
        # Pin the validated address throughout the TLS request. No redirect or proxy is followed.
        last_error = None
        for address in addresses[:4]:
            connection = PinnedHTTPSConnection(url.hostname, address)
            try:
                route = url.path or "/"
                if url.query:
                    route += "?" + url.query
                connection.request("GET", route, headers={
                    "Accept-Encoding": "identity", "User-Agent": "AI-Travel-Video-Service/0.1",
                })
                with connection.getresponse() as response:
                    if response.status == 429 or response.status >= 500:
                        raise RetryableExecutionError("Asset origin temporarily unavailable")
                    if response.status != 200:
                        raise MediaError("INVALID_ASSET", "Asset origin rejected the request")
                    length = response.getheader("Content-Length")
                    if length and length.isdecimal() and int(length) > max_bytes:
                        raise MediaError("ASSET_TOO_LARGE", "Media exceeds the size limit")
                    bounded_copy(response, target, max_bytes)
                    return identify(target, response.getheader("Content-Type"))
            except ssl.SSLCertVerificationError as exc:
                raise MediaError("UNSAFE_ASSET_URL", "Asset TLS verification failed") from exc
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                connection.close()
        raise RetryableExecutionError("Unable to reach asset origin") from last_error
    except socket.gaierror as exc:
        if exc.errno == socket.EAI_AGAIN:
            raise RetryableExecutionError("Asset DNS temporarily unavailable") from exc
        raise MediaError("INVALID_ASSET", "Asset hostname cannot be resolved") from exc


def save_upload(tenant: UUID, source: Path, content_type: str | None) -> UUID:
    asset_id = uuid4()
    identify(source, content_type, decode_image=False)
    atomic_copy(source, upload_path(tenant, asset_id))
    return asset_id


class JobAssets:
    def __init__(self, directory: Path, tenant_id: UUID):
        self.directory = directory
        self.tenant_id = tenant_id
        self.cached = {}
        self.total_bytes = 0

    def fetch(self, reference):
        if reference in self.cached:
            return self.cached[reference]
        configuration = settings()
        limit = min(
            configuration.max_asset_bytes,
            configuration.max_job_asset_bytes - self.total_bytes,
        )
        destination = self.directory / f"asset-{len(self.cached)}"
        if isinstance(reference, UUID):
            source = upload_path(self.tenant_id, reference)
            try:
                with source.open("rb") as stream:
                    bounded_copy(stream, destination, limit)
            except FileNotFoundError as exc:
                raise MediaError("ASSET_NOT_FOUND", "Uploaded asset was not found") from exc
            kind = identify(destination)
        else:
            kind = download(reference, destination, limit)
        self.total_bytes += destination.stat().st_size
        self.cached[reference] = (destination, kind)
        return destination, kind

