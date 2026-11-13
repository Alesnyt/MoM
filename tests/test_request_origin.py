import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import main, request_origin


def browser_request(headers, *, peer='198.51.100.10', method='POST'):
    return Request({'type':'http', 'method':method, 'path':'/api/meetings', 'scheme':'http',
                    'query_string':b'', 'client':(peer,1234),
                    'headers':[(key.lower().encode(),value.encode()) for key,value in headers.items()]})


@pytest.mark.parametrize('headers,expected', [
    ({'origin':'http://mom.example'}, True),
    ({'origin':'http://mom.example:80'}, True),
    ({'origin':'https://mom.example'}, False),
    ({'origin':'http://mom.example:8080'}, False),
    ({'origin':'http://evil.example','sec-fetch-site':'same-site'}, False),
    ({'origin':'null'}, False),
    ({'origin':'http://mom.example.evil.test'}, False),
    ({'origin':'http://user@mom.example'}, False),
    ({'origin':'http://mom.example:bad'}, False),
    ({'origin':'http://mom.example/path'}, False),
    ({'origin':'http://localhost:5173'}, False),
    ({'sec-fetch-site':'cross-site'}, False),
    ({'sec-fetch-site':'same-site'}, False),
    ({'sec-fetch-site':'same-origin'}, True),
    ({'referer':'http://mom.example/meeting?id=1'}, True),
    ({'referer':'http://evil.example/mom.example'}, False),
    ({}, True),  # Non-browser API clients remain supported.
])
def test_mutations_require_matching_browser_origin(headers, expected):
    assert request_origin.allowed(browser_request({'host':'mom.example',**headers})) is expected


def test_forwarded_scheme_only_from_trusted_proxy_and_port_is_preserved():
    request = browser_request({'host':'mom.example:8443','origin':'https://mom.example:8443',
                               'x-forwarded-proto':'https'})
    assert request_origin.allowed(request, trusted_proxy=True)
    assert not request_origin.allowed(request, trusted_proxy=False)
    request = browser_request({'host':'127.0.0.1:8000', 'origin':'http://localhost:5173', 'sec-fetch-site':'cross-site'})
    assert request_origin.allowed(request)


@pytest.mark.parametrize('path', ['/api/meetings', '/api/meetings/transcript', '/api/admin/context/import',
                                  '/api/admin/glossaries/preview', '/api/session/login'])
def test_cross_origin_forms_rejected_before_body_parsing(path, monkeypatch):
    async def forbidden(*a, **kw):
        pytest.fail('Untrusted request body was read')
    monkeypatch.setattr(Request,'stream',forbidden)
    response = TestClient(main.app).post(path, files={'file':('record.wav',b'not-read')},
                                         headers={'Origin':'https://untrusted.example','Sec-Fetch-Site':'cross-site'})
    assert response.status_code == 403
    assert response.headers['x-content-type-options'] == 'nosniff'


def test_same_origin_forms_still_require_authentication_and_reads_are_unchanged(monkeypatch):
    client = TestClient(main.app)
    assert client.post('/api/meetings',headers={'Origin':'http://testserver'}).status_code == 401
    monkeypatch.setattr(main,'_health',lambda **kw: {'ok':True})
    assert client.get('/api/health',headers={'Origin':'https://untrusted.example'}).status_code == 200
