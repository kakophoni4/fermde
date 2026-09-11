"""Loopback-only DNS UDP/TCP listener; upstream DNS uses TCP through the TUN.

Run inside a phone's network namespace. No alternate host-network resolver.
"""
import socket
import socketserver
import struct
import threading


def read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise OSError('DNS connection closed')
        result.extend(part)
    return bytes(result)


def resolve(query):
    if len(query) < 12:
        return b''
    for address in ('1.1.1.1', '1.0.0.1'):
        try:
            with socket.create_connection((address, 53), timeout=5) as upstream:
                upstream.sendall(struct.pack('!H', len(query)) + query)
                reply = read_exact(upstream, struct.unpack('!H', read_exact(upstream, 2))[0])
                if len(reply) < 12 or reply[:2] != query[:2]:
                    continue
                return reply
        except OSError:
            continue
    # SERVFAIL, retaining the transaction ID and question.
    flags = struct.unpack('!H', query[2:4])[0]
    return query[:2] + struct.pack('!H', (flags & 0x7900) | 0x8082) + query[4:6] + b'\0' * 6 + query[12:]


slots = threading.BoundedSemaphore(64)


class UDP(socketserver.BaseRequestHandler):
    def handle(self):
        if not slots.acquire(blocking=False):
            return
        try:
            query, sock = self.request
            reply = resolve(query)
            if reply:
                # Traditional UDP clients can retry over TCP when truncated.
                if len(reply) > 512:
                    flags = struct.unpack('!H', reply[2:4])[0] | 0x0200
                    reply = reply[:2] + struct.pack('!H', flags) + b'\0' * 8
                sock.sendto(reply, self.client_address)
        finally:
            slots.release()


class TCP(socketserver.BaseRequestHandler):
    def handle(self):
        if not slots.acquire(blocking=False):
            return
        try:
            self.request.settimeout(12)
            while True:
                query = read_exact(self.request, struct.unpack('!H', read_exact(self.request, 2))[0])
                reply = resolve(query)
                if not reply:
                    return
                self.request.sendall(struct.pack('!H', len(reply)) + reply)
        except OSError:
            pass
        finally:
            slots.release()


if __name__ == '__main__':
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    socketserver.ThreadingUDPServer.daemon_threads = True
    with socketserver.ThreadingUDPServer(('127.0.0.1', 53), UDP) as udp, \
         socketserver.ThreadingTCPServer(('127.0.0.1', 53), TCP) as tcp:
        threading.Thread(target=tcp.serve_forever, daemon=True).start()
        udp.serve_forever()
