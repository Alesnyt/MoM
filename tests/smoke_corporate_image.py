"""Exercise a real corporate image behind TLS using a private test ASR service."""
import http.cookiejar
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "gigaam-v3-local"
TEST_KEY = "corp-smoke-test-key"
TEXT = "Проверка корпоративной расшифровки."


class ASR(BaseHTTPRequestHandler):
    def reply(self, payload):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def authorized(self):
        if self.headers.get("Authorization") == "Bearer " + TEST_KEY:
            return True
        self.send_error(401)
        return False

    def do_GET(self):
        if self.authorized() and self.path == "/v1/models":
            self.reply({"data": [{"id": MODEL, "mode": "audio_transcription"}]})

    def do_POST(self):
        if not self.authorized():
            return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path != "/v1/audio/transcriptions" or MODEL.encode() not in body or b"RIFF" not in body:
            self.send_error(400)
            return
        self.reply({"text": TEXT, "segments": [{"start": 0, "end": 1, "text": TEXT}], "language": "ru"})


def docker(*args):
    return subprocess.check_output(["docker", *map(str, args)], text=True).strip()


def run_smoke(image, proxy_image):
    project = f"mom-test-{os.getpid()}"
    network, app, gpu, nginx = [f"{project}-{s}" for s in ("net", "app", "gpu", "proxy")]
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory).resolve()
        cert, key = folder / "cert.pem", folder / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,DNS:gpu"], check=True, stderr=subprocess.DEVNULL)
        docker("network", "create", network)
        try:
            docker("run", "-d", "--name", gpu, "--network", network, "--network-alias", "gpu", "--entrypoint", "python",
                   "-v", f"{Path(__file__).resolve()}:/smoke.py:ro", "-v", f"{folder}:/tls:ro", image, "/smoke.py", "--mock")

            def launch():
                docker("run", "-d", "--name", app, "--network", network, "--network-alias", "mom",
                       "-e", "SETUP_TOKEN=smoke-setup-token", "-e", "MIN_FREE_MB=0", "-e", f"TRUSTED_PROXY_IPS={proxy_ip}",
                       "-e", "SSL_CERT_FILE=/test-ca.pem", "-v", f"{cert}:/test-ca.pem:ro",
                       "-v", f"{project}-data:/app/data", image)

            docker("run", "-d", "--name", nginx, "--network", network,
                   "-p", "127.0.0.1::443", "-e", "SERVER_NAME=localhost", "-e", "NGINX_ENVSUBST_FILTER=^SERVER_NAME$",
                   "-v", f"{repo}/deploy/corporate/nginx.conf.template:/etc/nginx/templates/default.conf.template:ro",
                   "-v", f"{cert}:/etc/nginx/ssl/nginx.crt:ro", "-v", f"{key}:/etc/nginx/ssl/nginx.key:ro",
                   "--entrypoint", "/bin/sh", proxy_image, "-c",
                   "until [ -f /tmp/start-proxy ]; do sleep 1; done; exec /docker-entrypoint.sh nginx -g 'daemon off;'")
            proxy_ip = docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", nginx)
            launch()
            docker("exec", nginx, "touch", "/tmp/start-proxy")
            port = docker("port", nginx, "443/tcp").rsplit(":", 1)[1]
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                                 urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(cert))))

            def call(path, payload=None, *, data=None, content_type="application/json"):
                if payload is not None:
                    data = json.dumps(payload).encode()
                request = urllib.request.Request(f"https://localhost:{port}{path}", data=data,
                                                 headers={"Content-Type": content_type})
                with opener.open(request, timeout=8) as response:
                    return json.load(response)

            def ready():
                problem = "No HTTP response"
                for _ in range(90):
                    try:
                        health = call("/api/health")
                        if health["ok"]:
                            return
                        problem = str(health.get("worker", {}).get("services", []))
                    except (OSError, urllib.error.URLError) as exc:
                        problem = str(exc)
                    time.sleep(1)
                raise RuntimeError(f"Corporate MoM did not become ready on port {port}: {problem}")

            ready()
            call("/api/auth/setup", {"username": "testadmin", "password": "container-test-password",
                                     "setup_token": "smoke-setup-token"})
            connection = {"base_url": "https://gpu:8080/v1", "api_key": TEST_KEY, "model": MODEL}
            assert call("/api/settings/asr/models", connection)["models"] == [MODEL]
            assert call("/api/settings/asr/test", connection)["ok"]
            call("/api/settings/asr", {**connection, "engine": "remote-api"})
            user = call("/api/admin/users", {"email": "smoke@example.com", "auth_mode": "local", "archive_limit": 5})
            call("/api/session/login", {"email": "smoke@example.com", "password": user["password"]})
            audio = io.BytesIO()
            with wave.open(audio, "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", ""))
                stream.writeframes(bytes(32000))
            form = (b'--sample\r\nContent-Disposition: form-data; name="file"; filename="meeting.wav"\r\n'
                    b'Content-Type: audio/wav\r\n\r\n' + audio.getvalue() + b'\r\n--sample--\r\n')
            mid = call("/api/meetings", data=form, content_type="multipart/form-data; boundary=sample")["id"]
            for _ in range(90):
                meeting = call("/api/meetings/" + mid)
                if meeting["status"] == "review":
                    break
                assert meeting["status"] != "error", meeting.get("error")
                time.sleep(1)
            assert meeting["status"] == "review" and TEXT in meeting["transcript"] and meeting["segments"]
            docker("rm", "-fv", app)
            launch()
            docker("exec", nginx, "nginx", "-s", "reload")  # Refresh the upstream without changing the published port.
            ready()
            assert call("/api/meetings/" + mid)["status"] == "review"
            assert call("/api/settings/asr/models", {**connection, "api_key": ""})["models"] == [MODEL]
            print("PASS: TLS proxy, private GPU API, review without LLM, settings and recording after restart")
        finally:
            for name in (nginx, app, gpu):
                subprocess.run(["docker", "rm", "-fv", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "volume", "rm", project + "-data"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "network", "rm", network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    if sys.argv[1:] == ["--mock"]:
        server = ThreadingHTTPServer(("0.0.0.0", 8080), ASR)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain("/tls/cert.pem", "/tls/key.pem")
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        server.serve_forever()
    else:
        run_smoke(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "nginx:stable-alpine")
