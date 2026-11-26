import ipaddress

from pydantic import HttpUrl


def validate_image_url(url: HttpUrl) -> HttpUrl:
    host = (url.host or "").strip("[]").rstrip(".").lower()
    if url.scheme != "https" or url.username or url.password or url.port != 443:
        raise ValueError("Images must use HTTPS on port 443 without credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith((".localhost", ".local", ".internal", ".test")):
            raise ValueError("Image URL must use a public hostname")
    else:
        if not address.is_global or address.is_multicast:
            raise ValueError("Image URL must use a public address")
    return url

