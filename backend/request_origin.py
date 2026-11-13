"""Reject cross-origin browser mutations before reading their request bodies."""
from urllib.parse import urlsplit

_UNSAFE = {'POST', 'PUT', 'PATCH', 'DELETE'}
_DEV_ORIGINS = {('http', host, port) for host in ('localhost', '127.0.0.1') for port in (5173, 8000)}


def _origin(value: str, *, referer: bool = False):
    try:
        url = urlsplit(value)
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
            return None
        if not referer and (url.path not in {'', '/'} or url.query or url.fragment):
            return None
        return url.scheme, url.hostname.lower(), url.port if url.port is not None else (443 if url.scheme == 'https' else 80)
    except ValueError:
        return None


def allowed(request, *, trusted_proxy: bool = False) -> bool:
    if request.method not in _UNSAFE or not request.url.path.startswith('/api/'):
        return True
    supplied = request.headers.get('origin')
    referer = request.headers.get('referer') if supplied is None else None
    site = request.headers.get('sec-fetch-site', '').lower()
    if supplied is None and referer is None:
        # CLI/integration clients don't send browser metadata. Browser requests
        # from sibling subdomains are not trusted merely for being same-site.
        return site not in {'cross-site', 'same-site'}
    source = _origin(supplied if supplied is not None else referer, referer=referer is not None)
    scheme = request.url.scheme
    if trusted_proxy:
        forwarded = request.headers.get('x-forwarded-proto', '').split(',')[0].strip().lower()
        if forwarded in {'http', 'https'}:
            scheme = forwarded
    target = _origin(f'{scheme}://{request.headers.get("host", "")}')
    if source is None or target is None:
        return False
    if source in _DEV_ORIGINS and target in _DEV_ORIGINS:
        return True
    return source == target and site != 'cross-site'
