#!/usr/bin/env python3
"""VLESS-over-WebSocket relay node — Streamlit Community Cloud build.

Stdlib only. Faithful port of heavencloud-argo/index.js (Node) protocol logic:
  * HTTP: decoy page on /, base64 subscription on SUB_PATH (503 until tunnel up)
  * WebSocket upgrade on WS_PATH -> VLESS session (TCP only, like the Node build)
  * cloudflared quick tunnel is managed by app.py; this module only serves locally.
"""
import base64
import hashlib
import os
import secrets
import socket
import threading
from urllib.parse import quote


def _env(name, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


VLESS_UUID = _env("VLESS_UUID", "").lower().replace("-", "")
WS_PATH = _env("WS_PATH", "")
SUB_PATH = _env("SUB_PATH", "/sub")
NODE_NAME = _env("NODE_NAME", "hemeilin")
LISTEN_PORT = int(_env("PORT", _env("SERVER_PORT", "8001")))
CFPORT = "443"

if not VLESS_UUID:
    VLESS_UUID = secrets.token_hex(16)
    print("[vless] generated random UUID (set VLESS_UUID to fix it)", flush=True)
if not WS_PATH:
    WS_PATH = "/api/v1/" + VLESS_UUID[:8]

# Set by app.py once the quick tunnel is up; read by the /sub handler.
tunnel_domain = None

DECOY = (b"<!DOCTYPE html>\n"
         b'<html lang="en"><head><meta charset="utf-8">'
         b'<meta name="viewport" content="width=device-width,initial-scale=1">\n'
         b"<title>Service Status</title></head>\n"
         b'<body style="font-family:system-ui,sans-serif;display:flex;align-items:center;'
         b'justify-content:center;height:100vh;margin:0;background:#0b0f14;color:#9fb3c8">\n'
         b'<div style="text-align:center"><div style="font-size:15px;letter-spacing:.2em">'
         b"SERVICE&nbsp;STATUS</div>\n"
         b'<div style="font-size:42px;color:#3ddc84;margin:12px 0">&#9679;</div>\n'
         b"<div>All systems operational</div></div></body></html>")


def argo_link(domain):
    return ("vless://" + VLESS_UUID + "@" + domain + ":" + CFPORT +
            "?encryption=none&security=tls"
            "&sni=" + domain +
            "&fp=firefox"
            "&type=ws"
            "&host=" + domain +
            "&path=" + quote(WS_PATH, safe="") +
            "#" + quote(NODE_NAME, safe=""))


def subscription_b64(domain):
    return base64.b64encode(argo_link(domain).encode("utf-8")).decode("ascii")


_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSConnection:
    """Minimal server-side WebSocket connection (mirrors Node WsConn)."""

    def __init__(self, sock):
        self.sock = sock
        self.dead = False
        self.onmessage = None
        self.onclose = None
        self._send_lock = threading.Lock()

    def die(self):
        if self.dead:
            return
        self.dead = True
        cb, self.onclose = self.onclose, None
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _frame(self, opcode, data):
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        n = len(data)
        if n < 126:
            head = bytes([0x80 | opcode, n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 126]) + n.to_bytes(2, "big")
        else:
            head = bytes([0x80 | opcode, 127]) + n.to_bytes(8, "big")
        with self._send_lock:
            try:
                self.sock.sendall(head + data)
            except OSError:
                pass

    def send(self, data):
        self._frame(0x2, data)

    def pong(self, data=b""):
        self._frame(0xA, data)

    def close(self):
        if self.dead:
            return
        try:
            self._frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.die()

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def read_loop(self):
        """Blocking read loop; delivers complete messages via onmessage."""
        msg_opcode = None
        msg_parts = []
        while not self.dead:
            head = self._recv_exact(2)
            if head is None:
                self.die()
                return
            b0, b1 = head[0], head[1]
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                ext = self._recv_exact(2)
                if ext is None:
                    self.die()
                    return
                length = int.from_bytes(ext, "big")
            elif length == 127:
                ext = self._recv_exact(8)
                if ext is None:
                    self.die()
                    return
                length = int.from_bytes(ext, "big")
                if length > 16 * 1024 * 1024:
                    self.close()
                    return
            mask = self._recv_exact(4) if masked else None
            if masked and mask is None:
                self.die()
                return
            payload = self._recv_exact(length) if length else b""
            if payload is None:
                self.die()
                return
            if masked:
                payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
            if opcode == 0x8:  # close
                self.close()
                return
            if opcode == 0x9:  # ping
                self.pong(payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):  # text/binary: start of message
                msg_opcode = opcode
                msg_parts = [payload]
            elif opcode == 0x0:  # continuation
                if msg_opcode is None:
                    continue
                msg_parts.append(payload)
            else:
                continue
            if fin:
                full = b"".join(msg_parts)
                msg_opcode = None
                msg_parts = []
                if self.onmessage and not self.dead:
                    try:
                        self.onmessage(full)
                    except Exception:
                        self.close()
                        return


def parse_vless_header(data):
    """Mirror of Node parseVlessHeader. Returns dict or None."""
    if len(data) < 26:
        return None
    version = data[0]
    if data[1:17].hex() != VLESS_UUID:
        return None
    addon_len = data[17]
    i = 18 + addon_len
    if i + 4 > len(data):
        return None
    cmd = data[i]
    i += 1
    port = (data[i] << 8) | data[i + 1]
    i += 2
    atyp = data[i]
    i += 1
    if atyp == 1:  # IPv4
        if i + 4 > len(data):
            return None
        addr = ".".join(str(b) for b in data[i:i + 4])
        i += 4
    elif atyp == 2:  # domain
        ln = data[i]
        i += 1
        if i + ln > len(data):
            return None
        try:
            addr = data[i:i + ln].decode("utf-8")
        except UnicodeDecodeError:
            return None
        i += ln
    elif atyp == 3:  # IPv6
        if i + 16 > len(data):
            return None
        parts = []
        for k in range(8):
            parts.append(format((data[i + 2 * k] << 8) | data[i + 2 * k + 1], "x"))
            # i advances below
        i += 16
        addr = ":".join(parts)
    else:
        return None
    if cmd not in (1, 2):
        return None
    return {"version": version, "addr": addr, "port": port,
            "is_udp": cmd == 2, "data_start": i}


def handle_vless(ws):
    state = {"remote": None, "header_done": False, "closed": False,
             "lock": threading.Lock()}

    def fail():
        with state["lock"]:
            if state["closed"]:
                return
            state["closed"] = True
            remote = state["remote"]
        if remote is not None:
            try:
                remote.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                remote.close()
            except OSError:
                pass
        try:
            ws.close()
        except Exception:
            pass

    def pump_remote(remote):
        try:
            while True:
                try:
                    chunk = remote.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                if state["closed"]:
                    break
                ws.send(chunk)
        finally:
            fail()

    def on_message(payload):
        if state["closed"]:
            return
        if not state["header_done"]:
            h = parse_vless_header(payload)
            if h is None or h["is_udp"]:
                fail()
                return
            try:
                remote = socket.create_connection((h["addr"], h["port"]), timeout=15)
            except OSError:
                fail()
                return
            with state["lock"]:
                if state["closed"]:
                    try:
                        remote.close()
                    except OSError:
                        pass
                    return
                state["remote"] = remote
                state["header_done"] = True
            ws.send(bytes([h["version"], 0]))
            threading.Thread(target=pump_remote, args=(remote,), daemon=True).start()
            rest = payload[h["data_start"]:]
            if rest:
                try:
                    remote.sendall(rest)
                except OSError:
                    fail()
        else:
            remote = state["remote"]
            if remote is None:
                fail()
                return
            try:
                remote.sendall(payload)
            except OSError:
                fail()

    def on_close():
        fail()

    ws.onmessage = on_message
    ws.onclose = on_close
    ws.read_loop()


def _read_http_head(sock):
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
        if len(buf) > 65536:
            return None
    head, _, _ = bytes(buf).partition(b"\r\n\r\n")
    return head


def handle_client(conn):
    try:
        head = _read_http_head(conn)
        if head is None:
            conn.close()
            return
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ")
        if len(parts) < 2:
            conn.close()
            return
        target = parts[1].decode("latin-1")
        path = target.split("?", 1)[0]
        headers = {}
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                headers[k.strip().lower()] = v.strip()

        if path == SUB_PATH:
            domain = tunnel_domain
            if not domain:
                body = b"tunnel not ready yet, try again in a few seconds\n"
                conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                            b"Content-Type: text/plain; charset=utf-8\r\n"
                            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                            b"Connection: close\r\n\r\n" + body)
            else:
                body = (subscription_b64(domain) + "\n").encode("ascii")
                conn.sendall(b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: text/plain; charset=utf-8\r\n"
                            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                            b"Connection: close\r\n\r\n" + body)
            conn.close()
            return

        key = headers.get(b"sec-websocket-key")
        upgrade = headers.get(b"upgrade", b"").lower()
        if path == WS_PATH and key and upgrade == b"websocket":
            accept = base64.b64encode(
                hashlib.sha1(key + _GUID.encode("latin-1")).digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Upgrade: websocket\r\n"
                        b"Connection: Upgrade\r\n"
                        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            handle_vless(WSConnection(conn))
            return

        if path == WS_PATH or key:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
            conn.close()
            return
        conn.sendall(b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(DECOY)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + DECOY)
        conn.close()
    except Exception:
        try:
            conn.close()
        except OSError:
            pass


def run():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LISTEN_PORT))
    srv.listen(128)
    print("[vless] listening on 127.0.0.1:%d  ws_path=%s sub_path=%s" %
          (LISTEN_PORT, WS_PATH, SUB_PATH), flush=True)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle_client, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    run()
