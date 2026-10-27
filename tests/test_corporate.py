import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from backend import auth, config, ldap_auth, main

ROOT = 'DC=corp,DC=example'
EMPLOYEES = 'OU=Employees,' + ROOT


@pytest.fixture(autouse=True)
def isolated_login_scope(monkeypatch):
    monkeypatch.delenv('LDAP_AUTH_BASE_DNS', raising=False)
    monkeypatch.delenv('LDAP_ALLOWED_OU', raising=False)
    monkeypatch.setattr(config, 'LDAP_BASE_DN', ROOT)


class Entry:
    def __init__(self, dn):
        self.entry_dn = dn
    def __contains__(self, key):
        return True
    def __getitem__(self, key):
        return SimpleNamespace(value='alice@corp.example')


class Directory:
    def __init__(self, dns, description='success'):
        self.dns = dns
        self.description = description
        self.calls = []
    def search(self, **kwargs):
        self.calls.append(kwargs)
        self.entries = [Entry(dn) for dn in self.dns if dn.endswith(kwargs['search_base'])][:kwargs['size_limit']]
        self.result = {'description': self.description}
        return bool(self.entries)


def test_default_login_keeps_original_base_and_non_users_accounts():
    connection = Directory(['CN=Alice,' + EMPLOYEES])
    assert ldap_auth._search(connection, 'alice@corp.example')[0][0] == 'CN=Alice,' + EMPLOYEES
    assert connection.calls[0]['search_base'] == ROOT


def test_scope_applied_before_result_limit_excludes_contractors(monkeypatch):
    monkeypatch.setenv('LDAP_AUTH_BASE_DNS', json.dumps([EMPLOYEES]))
    connection = Directory(['CN=Alice,OU=Users,OU=Contractors,' + ROOT,
                            'CN=Alice,OU=Archive,' + ROOT, 'CN=Alice,' + EMPLOYEES])
    assert ldap_auth._search(connection, 'alice@corp.example') == [('CN=Alice,' + EMPLOYEES, 'alice@corp.example')]
    assert connection.calls[0]['search_base'] == EMPLOYEES


def test_cn_users_supported_and_overlapping_scopes_deduplicated(monkeypatch):
    monkeypatch.setenv('LDAP_AUTH_BASE_DNS', json.dumps(['CN=Users,' + ROOT, 'OU=Team,' + EMPLOYEES, EMPLOYEES, EMPLOYEES]))
    assert ldap_auth.authentication_bases() == ['CN=Users,' + ROOT, EMPLOYEES]


@pytest.mark.parametrize('value', ['[]', 'null', '"Users"', '[123]', '["Users"]', '["DC=other"]', 'not-json'])
def test_invalid_scopes_fail_closed(monkeypatch, value):
    monkeypatch.setenv('LDAP_AUTH_BASE_DNS', value)
    with pytest.raises(ldap_auth.LdapError, match='LDAP_AUTH_BASE_DNS'):
        ldap_auth.authentication_bases()


def test_old_short_ou_setting_requires_explicit_migration(monkeypatch):
    monkeypatch.setenv('LDAP_ALLOWED_OU', 'Users')
    with pytest.raises(ldap_auth.LdapError, match='Замените'):
        ldap_auth.authentication_bases()


def test_two_allowed_accounts_remain_ambiguous(monkeypatch):
    other = 'OU=SPB,' + ROOT
    monkeypatch.setenv('LDAP_AUTH_BASE_DNS', json.dumps([EMPLOYEES, other]))
    connection = Directory(['CN=Alice,' + EMPLOYEES, 'CN=Alice,' + other])
    assert len(ldap_auth._search(connection, 'alice@corp.example')) == 2


@pytest.mark.parametrize('description', ['sizeLimitExceeded', 'timeLimitExceeded', 'noSuchObject', 'insufficientAccessRights'])
def test_incomplete_directory_answer_cannot_authenticate(description):
    connection = Directory(['CN=Alice,' + EMPLOYEES], description)
    with pytest.raises(ldap_auth.LdapError):
        ldap_auth._search(connection, 'alice@corp.example')


def test_ldap_open_checks_socket_instead_of_none_return():
    class Connection:
        closed = True
        def open(self):
            self.closed = False
        def start_tls(self):
            return True
    ldap_auth._open(Connection(), starttls=True)
    with pytest.raises(ldap_auth.LdapError):
        ldap_auth._open(SimpleNamespace(open=lambda: None, closed=True), starttls=False)


def proxy_request(peer, client_ip='10.1.2.3'):
    return Request({'type': 'http', 'scheme': 'http', 'server': ('mom', 8000),
                    'client': (peer, 40000), 'path': '/api/session/login',
                    'headers': [(b'x-forwarded-proto', b'https'), (b'x-forwarded-for', client_ip.encode())]})


def test_trusted_proxy_secure_cookie_and_distinct_rate_limit_keys(monkeypatch):
    monkeypatch.setattr(config, 'TRUSTED_PROXY_IPS', {'172.30.88.3'})
    keys = []
    monkeypatch.setattr(auth, 'rate_allow', lambda key: keys.append(key) or True)
    for ip in ['10.1.2.3', '10.1.2.4']:
        request = proxy_request('172.30.88.3', ip)
        assert main._cookie_kw(request)['secure'] is True
        main._rate_or_429(request, 'user')
    assert keys == ['user:10.1.2.3', 'user:10.1.2.4']


def test_untrusted_client_cannot_spoof_proxy_headers(monkeypatch):
    monkeypatch.setattr(config, 'TRUSTED_PROXY_IPS', {'172.30.88.3'})
    request = proxy_request('10.1.2.8')
    assert main._cookie_kw(request)['secure'] is False
    assert main._client_ip(request) == '10.1.2.8'
