"""Controlled origins, reachable only inside the disposable SSH fixture."""
import http.server
import json
import time
import urllib.parse
import hashlib
import base64
import socket
import socketserver
import ssl
import threading

uploaded = [0]
counted = [0]
counted_lock = threading.Lock()

class Origin(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        if self.path == '/counted':
            with counted_lock:
                body = str(counted[0]).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/no-response':
            self.close_connection = True
            return
        if self.path == '/uploaded':
            body = str(uploaded[0]).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith('/inspect'):
            body = json.dumps({'path': self.path, 'host': self.headers.get('Host'), 'port': self.server.server_port, 'headers': dict(self.headers)}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'X-Origin-Hop')
            self.send_header('X-Origin-Hop', 'remove-this')
            self.send_header('Proxy-Authenticate', 'remove-this')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/long-response':
            self.send_response(200)
            self.send_header('Content-Length', '10')
            self.end_headers()
            self.wfile.write(b'first')
            self.wfile.flush()
            time.sleep(11)
            self.wfile.write(b'final')
            return
        if self.path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for event in [b'data: first\n\n', b'data: second\n\n']:
                self.wfile.write(f'{len(event):x}\r\n'.encode() + event + b'\r\n')
                self.wfile.flush()
                time.sleep(1.2)
            self.wfile.write(b'0\r\n\r\n')
            return
        if self.path.startswith('/bytes?'):
            size = int(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['count'][0])
            self.send_response(200)
            self.send_header('Content-Length', str(size))
            self.end_headers()
            block = b'x' * 16384
            try:
                while size:
                    take = min(size, len(block))
                    self.wfile.write(block[:take])
                    size -= take
            except OSError:
                pass
            return
        if self.path == '/truncated':
            self.send_response(200)
            self.send_header('Content-Length', '1024')
            self.end_headers()
            self.wfile.write(b'first-and-only')
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == '/websocket':
            key = self.headers['Sec-WebSocket-Key']
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            # A controlled small-message echo verifies masked client traffic and
            # independent server traffic over the opaque TLS CONNECT tunnel.
            self.wfile.write(b'\x81\x05hello')
            self.wfile.flush()
            first, second = self.rfile.read(2)
            assert first == 0x81 and second & 0x80
            length = second & 0x7f
            assert length < 126
            mask = self.rfile.read(4)
            encoded = self.rfile.read(length)
            decoded = bytes(value ^ mask[i % 4] for i, value in enumerate(encoded))
            self.wfile.write(bytes([0x81, length]) + decoded)
            self.wfile.flush()
            self.close_connection = True
            return
        body = b"proxrs-docker-origin\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        uploaded[0] = 0
        if self.headers.get('Transfer-Encoding') == 'chunked':
            while True:
                size = int(self.rfile.readline().split(b';')[0], 16)
                if size == 0:
                    while self.rfile.readline() != b'\r\n':
                        pass
                    break
                remaining = size
                while remaining:
                    data = self.rfile.read(min(4096, remaining))
                    if not data:
                        return
                    uploaded[0] += len(data)
                    remaining -= len(data)
                assert self.rfile.read(2) == b'\r\n'
        else:
            remaining = int(self.headers.get('Content-Length', '0'))
            while remaining:
                data = self.rfile.read(min(4096, remaining))
                if not data:
                    return
                uploaded[0] += len(data)
                remaining -= len(data)
        if self.path == '/counted':
            with counted_lock:
                counted[0] += 1
            self.send_response(200)
            self.send_header('Content-Length', '1000')
            self.end_headers()
            self.wfile.write(b'first')
            self.wfile.flush()
            time.sleep(15)
            self.close_connection = True
            return
        body = str(uploaded[0]).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
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
           socketserver.ThreadingTCPServer(("127.0.0.1", 9002), Flood),
           http.server.ThreadingHTTPServer(("127.0.0.1", 8081), Origin),
           http.server.ThreadingHTTPServer(("127.0.0.1", 80), Origin),
           IPv6Server(("::1", 8080), Origin)]
for server in servers[1:3]:
    server.socket = context.wrap_socket(server.socket, server_side=True)
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
threading.Event().wait()
