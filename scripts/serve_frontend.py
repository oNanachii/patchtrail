"""Local frontend files plus a loopback-only proxy to a verified backend."""
import argparse
from functools import partial
import http.client
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from urllib.parse import urlsplit

WEB = Path(__file__).resolve().parent.parent / "web"
sys.path.insert(0, str(WEB.parent))
from patchtrail.api import CSP


class FrontendHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        return super().end_headers()

    def log_message(self, format, *args):
        pass

    def _proxy(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 0 <= size <= 4096 or self.headers.get("Transfer-Encoding"):
            self.send_error(413)
            return
        body = self.rfile.read(size) if size else None
        headers = {key: self.headers[key] for key in ("Content-Type", "Authorization", "Idempotency-Key") if key in self.headers}
        connection = http.client.HTTPConnection(self.server.backend.hostname, self.server.backend.port, timeout=10)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                self.send_error(502, "Backend response exceeded the preview limit")
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            self.send_error(502, "Private backend is unavailable; check the SSH tunnel")
        finally:
            connection.close()

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._proxy()
        path = urlsplit(self.path).path
        if path == "/":
            self.path = "/index.html"
        elif path.startswith("/assets/"):
            self.path = self.path[len("/assets"):]
        else:
            self.send_error(404)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._proxy()
        self.send_error(404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:18816")
    parser.add_argument("--port", type=int, default=8874)
    args = parser.parse_args()
    backend = urlsplit(args.api)
    if backend.scheme != "http" or backend.hostname not in {"127.0.0.1", "localhost"} or not backend.port:
        parser.error("Backend must be an explicit loopback HTTP URL")
    handler = partial(FrontendHandler, directory=str(WEB))
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        server.backend = backend
        print(f"Frontend preview: http://127.0.0.1:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
