"""Controlled origins, reachable only inside the disposable SSH fixture."""
import http.server
import socket
import socketserver
import ssl
import threading

class Origin(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"proxrs-docker-origin\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while data := self.request.recv(16384):
            self.request.sendall(data)
        self.request.sendall(b"after-half-close")

class Flood(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(1)
        try:
            block = b"x" * 16384
            for _ in range(32768):
                self.request.sendall(block)
        except OSError:
            pass

class IPv6Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6

# A full listen queue makes subsequent SSH destination connects stall without
# relying on public IPs, firewall changes, or unrelated network services.
stalled = socket.socket()
stalled.bind(("127.0.0.1", 9001))
stalled.listen(1)
queued = [socket.create_connection(("127.0.0.1", 9001)) for _ in range(2)]

context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/run/origin.pem", "/run/origin.key")
servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 8080), Origin),
           http.server.ThreadingHTTPServer(("127.0.0.1", 8443), Origin),
           IPv6Server(("::1", 8443), Origin),
           socketserver.ThreadingTCPServer(("127.0.0.1", 9000), Echo),
           socketserver.ThreadingTCPServer(("127.0.0.1", 9002), Flood)]
for server in servers[1:3]:
    server.socket = context.wrap_socket(server.socket, server_side=True)
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
threading.Event().wait()
