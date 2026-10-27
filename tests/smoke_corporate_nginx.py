"""Real nginx template/TLS/streaming smoke test; no models or corporate services.

Run: python tests/smoke_corporate_nginx.py [nginx-image]
The optional image must include the official nginx entrypoint/template script.
"""
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class Backend(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(dict(self.headers)).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Like the application's unauthenticated route: reject before reading.
        self.send_response(401)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def main():
    image = sys.argv[1] if len(sys.argv) > 1 else 'nginx:stable-alpine'
    name = f'mom-nginx-smoke-{os.getpid()}'
    backend = ThreadingHTTPServer(('0.0.0.0', 0), Backend)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='mom-nginx-smoke-') as directory:
        folder = Path(directory)
        cert, key = folder / 'cert.pem', folder / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        template = folder / 'default.conf.template'
        template.write_text((ROOT / 'deploy/corporate/nginx.conf.template').read_text().replace(
            'http://mom:8000', f'http://mom:{backend.server_port}'))
        try:
            run('docker', 'run', '-d', '--user', '0', '--name', name, '--add-host', 'mom:host-gateway',
                '-p', '127.0.0.1::80', '-p', '127.0.0.1::443',
                '-e', 'SERVER_NAME=localhost', '-e', 'NGINX_ENVSUBST_FILTER=^SERVER_NAME$',
                '-v', f'{template}:/etc/nginx/templates/default.conf.template:ro',
                '-v', f'{cert}:/etc/nginx/ssl/nginx.crt:ro',
                '-v', f'{key}:/etc/nginx/ssl/nginx.key:ro',
                '--entrypoint', '/docker-entrypoint.sh', image, 'nginx', '-g', 'daemon off;')
            http_port = int(run('docker', 'port', name, '80/tcp').rsplit(':', 1)[1])
            https_port = int(run('docker', 'port', name, '443/tcp').rsplit(':', 1)[1])
            for _ in range(50):
                try:
                    with socket.create_connection(('localhost', https_port), timeout=1):
                        break
                except OSError:
                    time.sleep(.2)
            run('docker', 'exec', name, 'nginx', '-t', stderr=subprocess.STDOUT)
            context = ssl.create_default_context(cafile=str(cert))
            connection = http.client.HTTPConnection('localhost', http_port, timeout=5)
            connection.request('GET', '/test?x=1')
            response = connection.getresponse()
            assert response.status == 301
            assert response.getheader('Location') == 'https://localhost/test?x=1'
            connection.close()

            connection = http.client.HTTPSConnection('localhost', https_port, context=context, timeout=5)
            connection.request('GET', '/headers', headers={'X-Forwarded-For': 'spoofed', 'X-Forwarded-Proto': 'http'})
            response = connection.getresponse()
            assert response.status == 200
            headers = json.loads(response.read())
            assert headers['X-Forwarded-Proto'] == 'https'
            assert headers['X-Forwarded-For'] != 'spoofed'
            connection.close()

            # Only headers are sent: buffering would wait for the missing 1.5 GiB
            # body, and the old 1200M limit would return 413 instead of backend 401.
            connection = http.client.HTTPSConnection('localhost', https_port, context=context, timeout=5)
            connection.putrequest('POST', '/api/meetings')
            connection.putheader('Content-Type', 'multipart/form-data; boundary=smoke')
            connection.putheader('Content-Length', str(1536 * 1024 * 1024))
            connection.endheaders()
            assert connection.getresponse().status == 401
            connection.close()
            assert not run('docker', 'exec', name, 'sh', '-c', 'find /var/cache/nginx/client_temp -type f 2>/dev/null || true')
            print('PASS: nginx config, TLS verification, redirect, trusted headers, early upload rejection, no spooling')
        except BaseException:
            subprocess.run(['docker', 'logs', name], check=False)
            raise
        finally:
            subprocess.run(['docker', 'rm', '-f', name], check=False, stdout=subprocess.DEVNULL)
            backend.shutdown()


if __name__ == '__main__':
    main()
