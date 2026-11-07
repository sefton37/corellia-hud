#!/usr/bin/env python3
"""A fake Bedrock server-list responder (UDP unconnected ping) for tests and the e2e run.

Importable (FakeBedrock) and runnable (`python3 -m tests.support.fake_bedrock --port N --players 2`).
Modes: ok | silent (never answers) | badmagic | truncated | nonutf8 | lying (length field larger than the
data) | huge (a 64 KB datagram). `hits` counts the datagrams received; the CLI also writes the count to
--count-file after every datagram so the e2e can read it.
"""
import argparse
import socket
import struct
import threading

MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
REAL_MOTD = "MCPE;Dedicated Server;2169;1.26.45;{players};{maxp};10000000000000000001;{level};Survival;1;19132;19133;"


def make_pong(motd, magic=MAGIC, declared=None):
    body = motd if isinstance(motd, bytes) else motd.encode()
    length = len(body) if declared is None else declared
    return b"\x1c" + struct.pack(">Q", 1) + struct.pack(">Q", 7) + magic + struct.pack(">H", length) + body


class FakeBedrock:
    def __init__(self, mode="ok", players=0, maxp=10, level="Testworld", port=0, count_file=None):
        self.mode, self.players, self.maxp, self.level = mode, players, maxp, level
        self.hits = 0
        self.count_file = count_file
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = None

    def reply(self):
        motd = REAL_MOTD.format(players=self.players, maxp=self.maxp, level=self.level)
        if self.mode == "badmagic":
            return make_pong(motd, magic=b"\x00" * 16)
        if self.mode == "truncated":
            return make_pong(motd)[:20]
        if self.mode == "nonutf8":
            return make_pong(b"MCPE;\xff\xfe\xfd;2169;1.26.45;0;10;1;Testworld;")
        if self.mode == "lying":
            return make_pong(motd, declared=len(motd) + 500)
        if self.mode == "huge":
            return make_pong(motd + "x" * 65000, declared=65000)
        return make_pong(motd)

    def serve(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.hits += 1
            if self.count_file:
                with open(self.count_file, "w") as f:
                    f.write(str(self.hits))
            if self.mode != "silent" and data[:1] == b"\x01":
                try:
                    self.sock.sendto(self.reply(), addr)
                except OSError:
                    pass

    def start(self):
        self._thread = threading.Thread(target=self.serve, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self.sock.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--players", type=int, default=0)
    ap.add_argument("--count-file")
    a = ap.parse_args()
    fake = FakeBedrock(a.mode, a.players, port=a.port, count_file=a.count_file)
    print(f"fake-bedrock listening 127.0.0.1:{fake.port}", flush=True)
    fake.serve()


if __name__ == "__main__":
    main()
