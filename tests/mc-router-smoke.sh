#!/bin/bash
# Verify that mc-router answers the Minecraft status protocol on its published port.
#
# WHAT THIS IS FOR. The router is the only published service in the stack, so
# "the server container is healthy" is not the same as "a client can reach a
# server". This speaks the first two packets of the Minecraft Java protocol
# (handshake, then status request) to the published port and requires the
# JSON status response. It passes whether the backend is awake (the real
# status is proxied) or asleep (the configured asleep MOTD is served); both
# prove the router is up, routed, and answering. A connection that the router
# refuses outright - unmapped hostname, backend down with no asleep MOTD -
# gets no response and fails the test.
#
# Run from the repository root with the stack up:
#   ./tests/mc-router-smoke.sh
#
# The hostname and port default to what the compose file publishes; override
# to probe a different deployment.
set -uo pipefail

HOST="${MINECRAFT_SERVER_ROUTER_HOST:-localhost}"
PORT="${MINECRAFT_SERVER_SERVER_PORT:-25565}"

python3 - "$HOST" "$PORT" <<'PY'
import json
import socket
import struct
import sys


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def read_varint(buf, pos):
    val = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, pos
        shift += 7


def mc_string(s):
    enc = s.encode("utf-8")
    return varint(len(enc)) + enc


host, port = sys.argv[1], int(sys.argv[2])

try:
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
except OSError as e:
    sys.exit(f"cannot connect to 127.0.0.1:{port}: {e} - is the router up and publishing it?")

try:
    # Handshake: state 0 -> 2. Protocol 47 is the 1.20-1.21 era; the router
    # forwards it untouched and the server answers regardless.
    payload = varint(47) + mc_string(host) + struct.pack(">H", port) + varint(2)
    sock.sendall(varint(1 + len(payload)) + varint(0x00) + payload)
    # Status request: packet 1, no payload.
    sock.sendall(varint(1) + varint(0x01))

    data = b""
    status = None
    while status is None:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        try:
            total, pos = read_varint(data, 0)
            if len(data) < pos + total:
                continue  # packet incomplete, read more
            pid, pos = read_varint(data, pos)
            if pid != 0x02:
                sys.exit(f"expected status packet 2, got {pid}")
            slen, pos = read_varint(data, pos)
            if len(data) < pos + slen:
                continue
            status = json.loads(data[pos:pos + slen].decode("utf-8"))
        except IndexError:
            continue  # varint runs past the buffer, read more
        except ValueError:
            sys.exit("status response is not valid JSON")
finally:
    sock.close()

if status is None:
    sys.exit(f"no status response from the router for hostname {host!r} - is it mapped?")
print(f"router answered on {host}:{port}:")
print(json.dumps(status, indent=2)[:500])
PY
