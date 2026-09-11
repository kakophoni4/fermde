"""Run on the server only, along with the existing access checks."""
import struct
import unittest
from unittest.mock import Mock, patch
import dns_forwarder as dns

QUERY = bytes.fromhex('123401000001000000000000') + b'\x03www\x07example\x03com\0\0\x01\0\x01'


class DNSTests(unittest.TestCase):
    def test_split_tcp_frames(self):
        sock = Mock()
        sock.recv.side_effect = [b'\x00', b'\x12', b'ab', b'c']
        self.assertEqual(dns.read_exact(sock, 2), b'\x00\x12')
        self.assertEqual(dns.read_exact(sock, 3), b'abc')

    def test_upstream_failure_returns_servfail_with_question(self):
        with patch.object(dns.socket, 'create_connection', side_effect=TimeoutError):
            reply = dns.resolve(QUERY)
        self.assertEqual(reply[:2], QUERY[:2])
        self.assertEqual(struct.unpack('!H', reply[2:4])[0] & 15, 2)
        self.assertEqual(reply[12:], QUERY[12:])

    def test_closed_upstream_cannot_hang(self):
        sock = Mock()
        sock.recv.return_value = b''
        with self.assertRaises(OSError):
            dns.read_exact(sock, 2)

    def test_wrong_transaction_is_rejected(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        reply = b'\x99\x99' + QUERY[2:]
        sock.recv.side_effect = [struct.pack('!H',len(reply)), reply]*2
        with patch.object(dns.socket,'create_connection',return_value=sock):
            result = dns.resolve(QUERY)
        self.assertEqual(struct.unpack('!H',result[2:4])[0] & 15,2)
