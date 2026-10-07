#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RC WiFi car data decoder ("WIFI UFO" toy, cooingdv / KY-UFO protocol family).

Findings from notes.txt + public teardowns of this toy family:
    Device IP : 192.168.1.1
    UDP ports : 5007, 7070
    RTSP     : rtsp://192.168.1.1:7070/webcam
    Unlock   : send the 2-byte magic "01 01" to UDP 7070 -> the toy enables
               its data/video stream and starts sending datagrams.

This tool:
  * repeats the unlock magic on UDP 5007/7070 (survives timeouts/reboots)
  * listens on those ports and decodes every datagram:
      - control/telemetry packets  (cooingdv "66 .. 99" format)
      - RTP video, incl. RFC 2435 JPEG (payload type 26) reassembly
      - MJPEG-over-UDP ("66 01 01" header with frame offset/length fields)
      - raw JPEG / H.264 NAL / RTSP / plain text
  * fingerprints unknown packets (fixed bytes, counters, value ranges)
    so you can reverse-engineer fields interactively
  * optionally does a real RTSP DESCRIBE/SETUP/PLAY handshake and decodes
    whatever RTP arrives on the negotiated client port

Pure standard library - run with any Python 3.7+.

Examples:
    python decode.py                unlock 5007/7070, listen + decode
    python decode.py --probe        scan ports and test the RTSP endpoint
    python decode.py --rtsp         also run the full RTSP handshake
    python decode.py --port 7099    add/override a UDP port to sniff
    python decode.py --magic 0101   override the unlock magic (hex)
    python decode.py --save-jpg     write reassembled JPEG frames to --out
"""
import argparse
import binascii
import os
import re
import socket
import struct
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

IP = "192.168.1.1"
UDP_PORTS = (7099,)
ENABLE_MAGIC = bytes((0x01, 0x01))
RTSP_PATH = "/webcam"

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


# --------------------------------------------------------------------------
# colour helpers (ANSI, auto-disabled on unsupported terminals)
# --------------------------------------------------------------------------
class C:
    def __init__(self, on=True):
        self.on = on
        if on:  # enable VT mode on the Windows console
            try:
                import ctypes
                k = ctypes.windll.kernel32
                h = k.GetStdHandle(-11)
                m = ctypes.c_uint32(0)
                if k.GetConsoleMode(h, ctypes.byref(m)):
                    k.SetConsoleMode(h, m.value | 0x0004)
            except Exception:
                pass

    def paint(self, s, code):
        return f"\x1b[{code}m{s}\x1b[0m" if self.on else s

    def dim(self, s): return self.paint(s, "2")
    def red(self, s): return self.paint(s, "31")
    def grn(self, s): return self.paint(s, "32")
    def ylw(self, s): return self.paint(s, "33")
    def blu(self, s): return self.paint(s, "34")
    def mag(self, s): return self.paint(s, "35")
    def cyn(self, s): return self.paint(s, "36")
    def bold(self, s): return self.paint(s, "1")


# --------------------------------------------------------------------------
# decoding primitives
# --------------------------------------------------------------------------
def hexdump(data):
    out = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{i:04x}  {hexpart:<47}  {asc}")
    return "\n".join(out)


def decode_control(data):
    """cooingdv control/telemetry packets.

    Variant A (8 bytes): 66 <roll> <pitch> <throttle> <yaw> <cmd> <xor> 99
                         xor = roll^pitch^throttle^yaw (some fw: ^cmd)
    Variant B (21 bytes): 03 66 14 <roll> <pitch> <throttle> <yaw> <cmd>
                         <speed> 00*10 <xor> 99
    """
    # Confirmed KF29 / com.cooingdv.wificar car packet from PCAPdroid:
    # 03 33 <steering> <drive> 00 88.  Values observed at idle are 80/80.
    if len(data) == 6 and data[:2] == b"\x03\x33" and data[4:] == b"\x00\x88":
        return (f"CAR[6] steering={data[2]:#04x} drive={data[3]:#04x} "
                f"tail={data[4:].hex()}")
    # Confirmed periodic car response to the app's control socket.
    if len(data) == 15 and data[0] == 0x05 and not any(data[1:]):
        return "CAR[15] status/ack (05 + 14 zero bytes)"
    if len(data) == 8 and data[0] == 0x66 and data[-1] == 0x99:
        r, p, t, y, cmd, x = data[1:7]
        c1 = r ^ p ^ t ^ y
        ok = "OK" if x in (c1, c1 ^ cmd) else "??"
        return (f"CTRL[8] roll={r:#04x} pitch={p:#04x} thr={t:#04x} "
                f"yaw={y:#04x} cmd={cmd:#04x} xor={x:#04x} ({ok})")
    if (len(data) == 21 and data[0] == 0x03 and data[1] == 0x66
            and data[2] == 0x14 and data[-1] == 0x99):
        r, p, t, y, cmd, sp = data[3:9]
        x = data[-2]
        c1 = r ^ p ^ t ^ y
        ok = "OK" if x in (c1, c1 ^ cmd) else "??"
        return (f"CTRL[21] roll={r:#04x} pitch={p:#04x} thr={t:#04x} "
                f"yaw={y:#04x} cmd={cmd:#04x} speed={sp:#04x} xor={x:#04x} ({ok})")
    return None


def decode_rtp(data):
    """RFC 3550 header. Also RFC 2435 JPEG for PT 26 (used by these toys)."""
    if len(data) < 12:
        return None
    if data[0] >> 6 != 2:            # RTP version must be 2
        return None
    v, p, x, cc = data[0] >> 6, (data[0] >> 5) & 1, (data[0] >> 4) & 1, data[0] & 0x0f
    m, pt = (data[1] >> 7) & 1, data[1] & 0x7f
    seq = struct.unpack(">H", data[2:4])[0]
    ts = struct.unpack(">I", data[4:8])[0]
    ssrc = struct.unpack(">I", data[8:12])[0]
    extra = ""
    joff = None
    if pt == 26 and len(data) >= 12 + 8:     # RFC 2435 JPEG payload
        tspec, joff = data[12] >> 4, int.from_bytes(data[13:16], "big")
        jt, jq, wid, hei = data[16], data[17], data[18], data[19]
        extra = (f" JPEG type={jt} q={jq} w={wid} h={hei} "
                 f"frag_off={joff}")
    return (f"RTP v={v} pt={pt}{f' (JPEG)' if pt == 26 else ''} seq={seq} "
            f"ts={ts} ssrc={ssrc:08x} m={m}{extra}")


def decode_mjpeg_hdr(data):
    """MJPEG-over-UDP used by some cooingdv cameras.

    24-byte header, e.g.:
        66 01 01 83 40 4C 00 00 80 02 E0 01 <off_lo> <off_hi> <len_lo> <len_hi> 00...
    frame offset = u16 LE at [12:14], segment length = u16 LE at [14:16]
    """
    if len(data) < 24 or data[:3] != b"\x66\x01\x01":
        return None
    off = int.from_bytes(data[12:14], "little")
    seglen = int.from_bytes(data[14:16], "little")
    if not (0 < seglen <= len(data)):
        return None
    return off, seglen


def decode_raw(data):
    """Purely heuristic checks that don't need a header layout."""
    if len(data) >= 2 and data[:2] == binascii.unhexlify("ffd8"):
        return f"JPEG frame {len(data)} bytes"
    if len(data) >= 4 and (data[:4] == b"\x00\x00\x00\x01" or data[:3] == b"\x00\x00\x01"):
        return f"H.264 NAL unit {len(data)} bytes"
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in data)
    if len(data) and printable / len(data) > 0.80:
        try:
            text = data.decode("ascii", "replace")
        except Exception:
            text = repr(data)
        first = (text.split("\r\n")[0].split("\n")[0][:60])
        if first.startswith(("RTSP/", "OPTIONS", "DESCRIBE", "SETUP", "PLAY",
                             "TEARDOWN", "HTTP/")):
            return f"TEXT/RTSP: {first}"
        return f"TEXT: {first}"
    return None


class Fingerprint:
    """Tracks the last N packets per (port, length) and reports which byte
    positions are constant (headers/footers) versus variable (fields)."""

    FINGER_LIMIT = 64

    def __init__(self):
        self._buckets = defaultdict(list)

    def add(self, port, data):
        key = (port, len(data))
        b = self._buckets[key]
        b.append(bytes(data))
        if len(b) > self.FINGER_LIMIT:
            b.pop(0)

    def report(self, port, out=None):
        out = out if out is not None else []
        for (p, n), b in self._buckets.items():
            if p != port or len(b) < 4:
                continue
            m = max(len(x) for x in b)
            lines = []
            for i in range(m):
                vals = [x[i] for x in b if i < len(x)]
                uniq = sorted(set(vals))
                if len(uniq) == 1:
                    lines.append(f"  [{i:3d}] FIXED 0x{uniq[0]:02x}")
                else:
                    monotone = sum(1 for j in range(1, len(vals))
                                   if vals[j] > vals[j - 1])
                    how = ""
                    if monotone == len(vals) - 1:
                        how = " [monotonic/counter]"
                    elif len(uniq) <= 8:
                        how = f" [values: {' '.join(f'{v:02x}' for v in uniq)}]"
                    lines.append(f"  [{i:3d}] byte  0x{min(vals):02x}..0x{max(vals):02x} "
                                 f"({len(uniq)} distinct){how}")
            if lines:
                out.append((f"FINGERPRINT {port}/udp len={n} "
                            f"({len(b)} pkts):", lines))
        return out


# --------------------------------------------------------------------------
# JPEG reassembly helpers
# --------------------------------------------------------------------------
class JpegAssembler:
    def __init__(self, save_dir=None):
        self.buf = bytearray()
        self.frame = 0
        self.save_dir = save_dir

    def feed(self, port, offset, seglen, payload):
        if not payload.strip(b"\x00"):
            return None
        if offset == 0:
            self.buf = bytearray()
        self.buf += payload
        done = False
        if self.buf.endswith(b"\xff\xd9"):
            done = True
        if len(self.buf) > 0 and not done:
            idx = self.buf.find(b"\xff\xd9")
            if idx >= 0 and idx >= len(self.buf) - 64:
                done = True
        if self.buf and self.buf[0:2] != b"\xff\xd8":
            self.buf = bytearray(self.buf[self.buf.find(b"\xff\xd8"):]
                                 if b"\xff\xd8" in self.buf else b"")
        if done and self.buf:
            self.frame += 1
            n = self.frame
            if self.save_dir:
                fn = os.path.join(self.save_dir, f"frame_{port}_{n:05d}.jpg")
                with open(fn, "wb") as fh:
                    fh.write(self.buf)
                return f"JPEG frame #{n} saved ({len(self.buf)}B)"
            return f"JPEG frame #{n} ({len(self.buf)}B)"
        return None


# --------------------------------------------------------------------------
# RTSP probe (stdlib client)
# --------------------------------------------------------------------------
def rtsp_probe(ip, port, path, c):
    try:
        rtp_port = 13000 + (os.getpid() % 5000)
    except Exception:
        rtp_port = 16000
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(6)
    url = f"rtsp://{ip}:{port}{path}"
    try:
        sock.connect((ip, port))
    except OSError as e:
        return [f"RTSP connect failed: {e}"]
    lines = []

    def talk(cmd_lines, note):
        data = ("\r\n".join(cmd_lines) + "\r\n\r\n").encode()
        try:
            sock.sendall(data)
            resp = b""
            while b"\r\n\r\n" not in resp:
                ch = sock.recv(65536)
                if not ch:
                    break
                resp += ch
                if len(resp) > 65536:
                    break
        except OSError as e:
            lines.append(f"{note}: error {e}")
            return None
        head, _, body = resp.partition(b"\r\n\r\n")
        lines.append(f"{note}:")
        for ln in head.split(b"\r\n"):
            lines.append(f"   {ln.decode('latin1', 'replace')}")
        if body:
            lines.append(f"   [body {len(body)}B]")
            if b"application/sdp" in head:
                for ln in body.split(b"\r\n"):
                    lines.append(f"      {ln.decode('latin1', 'replace')}")
        return resp

    talk(["OPTIONS " + url + " RTSP/1.0", "CSeq: 1"], "OPTIONS")
    resp = talk(["DESCRIBE " + url + " RTSP/1.0", "CSeq: 2",
                 "Accept: application/sdp"], "DESCRIBE")
    cseq = "CSeq: 3"
    resp2 = talk(["SETUP " + url + "/track0 RTSP/1.0", cseq,
                  f"Transport: RTP/AVP/UDP;unicast;client_port={rtp_port}-{rtp_port + 1}"],
                 "SETUP")
    session = None
    if resp2 and b"Session" in resp2:
        head = resp2.split(b"\r\n\r\n")[0]
        for ln in head.split(b"\r\n"):
            if ln.lower().startswith(b"session:"):
                session = ln.split(b":", 1)[1].strip().decode("latin1", "replace")
    play = ["PLAY " + url + " RTSP/1.0", "CSeq: 4"]
    if session:
        play.append("Session: " + session)
    talk(play, "PLAY")
    sock.close()
    lines.append(f"   -> client RTP port {rtp_port} (listen here for video)")
    return lines


# --------------------------------------------------------------------------
# listeners
# --------------------------------------------------------------------------
def udp_listener(port, ip, magic, dec, fp, jpeg, c, stop, log=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    try:
        # The official app sends from an ephemeral client port to the car's
        # fixed destination port (7099). Bind ephemeral too so replies target
        # this socket exactly like the captured app session.
        sock.bind(("0.0.0.0", 0))
    except OSError as e:
        c.red(f"[!] cannot bind udp {port}: {e}")
        return
    sock.settimeout(0.5)
    last_enable = 0.0
    logged = 0
    while not stop.is_set():
        try:
            if time.time() - last_enable >= 2.0:
                try:
                    sock.sendto(magic, (ip, port))
                    last_enable = time.time()
                except OSError:
                    pass
            data, addr = sock.recvfrom(65536)
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                break
            continue
        logged += 1
        line = dec.ingest(port, data, addr, jpeg, logged)
        fp.add(port, data)
        print(line)
        if log:
            log.write(line)
    sock.close()


class TtyLog:
    _ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, path):
        self.fh = open(path, "a", encoding="utf-8", newline="\n") if path else None

    def write(self, text):
        if self.fh:
            self.fh.write(self._ANSI.sub("", text) + "\n")
            self.fh.flush()


def main():
    ap = argparse.ArgumentParser(
        description="Decode data from a WIFI UFO RC toy (192.168.1.1).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", default=IP, help=f"device IP (default {IP})")
    ap.add_argument("--port", type=int, action="append", default=[],
                    help="additional UDP port to sniff (repeatable)")
    ap.add_argument("--magic", default="0101",
                    help="unlock magic as hex (default 0101)")
    ap.add_argument("--rtsp", action="store_true",
                    help="also run the full RTSP handshake on 7070")
    ap.add_argument("--probe", action="store_true",
                    help="port-scan + RTSP endpoint check, then exit")
    ap.add_argument("--save-jpg", action="store_true",
                    help="save reassembled JPEG frames under --out")
    ap.add_argument("--out", default=LOG_DIR, help="log output dir")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    c = C(not args.no_color)
    magic = binascii.unhexlify(args.magic)
    ports = list(UDP_PORTS) + [p for p in args.port]
    ports = list(dict.fromkeys(ports))

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jpg_dir = os.path.join(args.out, "frames_" + stamp) if args.save_jpg else None
    if jpg_dir:
        os.makedirs(jpg_dir, exist_ok=True)

    print(c.bold(f"== WIFI UFO decoder ==  target {args.ip}  magic "
                 f"{magic.hex()}  udp {ports}  out {args.out}"))

    if args.probe:
        print(c.cyn("TCP port scan:"))
        for p in (5007, 7060, 7070, 7099, 8080, 8030, 8888):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.8)
            r = s.connect_ex((args.ip, p))
            print(f"   {p}: " + (c.grn("OPEN") if r == 0 else c.dim("closed")))
            s.close()
        pts = c.bold("UDP unlock test:")
        print(pts)
        for p in ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            try:
                s.sendto(magic, (args.ip, p))
                d, _ = s.recvfrom(2048)
                print(f"   udp {p}: sent {magic.hex()}, got reply {len(d)}B - "
                      + c.grn(d.hex()[:80]))
            except OSError:
                print(f"   udp {p}: nothing back after unlock (still could be the ctrl port)")
            finally:
                s.close()
        print()
        print(c.cyn(f"RTSP handshake against {args.ip}:7070{RTSP_PATH}:"))
        for ln in rtsp_probe(args.ip, 7070, RTSP_PATH, c):
            print("  " + ln)
        return

    stop = threading.Event()
    dec = Decoder(c)
    fp = Fingerprint()
    jpeg = JpegAssembler(jpg_dir)
    log = TtyLog(os.path.join(args.out, f"raw_{stamp}.log"))

    threads = []
    for p in ports:
        t = threading.Thread(target=udp_listener,
                             args=(p, args.ip, magic, dec, fp, jpeg, c, stop, log),
                             daemon=True)
        t.start()
        threads.append(t)
        print(c.grn(f"[*] listening {args.ip}:{p}/udp "
                    f"(sending unlock {magic.hex()} every 2s)"))

    if args.rtsp:
        def do_rtsp():
            while not stop.is_set():
                print(c.cyn("RTSP probe:"))
                for ln in rtsp_probe(args.ip, 7070, RTSP_PATH, c):
                    print("   " + ln)
                time.sleep(8)
        threading.Thread(target=do_rtsp, daemon=True).start()

    print(c.dim("<- command_line: Ctrl+C stops and prints a protocol fingerprint ->"))
    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    stop.set()
    for t in threads:
        t.join(timeout=2)

    print()
    print(c.bold("== per-port byte fingerprint (reverse-engineering aid) =="))
    for port in ports:
        rep = fp.report(port, [])
        for title, lines in rep[-3:]:
            print(c.bold(title))
            for ln in lines:
                print("   " + c.dim(ln))
    if jpeg and jpg_dir:
        print(f"frames: {jpeg.frame} saved under {jpg_dir}")
    print(c.dim("all datagrams also written to " + os.path.join(args.out, f"raw_{stamp}.log")))


# --------------------------------------------------------------------------
# ingress router (kept plain so the listener loop stays readable)
# --------------------------------------------------------------------------
class Decoder:
    def __init__(self, c):
        self.c = c

    def ingest(self, port, data, addr, jpeg, logged):
        c = self.c
        head = c.dim(f"[{addr[0]}:{addr[1]} -> {port}/udp #{logged} {len(data)}B]")
        detail = decode_control(data) or decode_rtp(data) or decode_raw(data)
        if detail:
            return f"{head} " + c.blu(detail)
        mj = decode_mjpeg_hdr(data)
        if mj:
            off, seglen = mj
            msg = jpeg.feed(port, off, seglen, data[24:])
            return (f"{head} " + c.ylw(f"MJPEG hdr off={off} seglen={seglen}"
                                       f" total={len(data)}"
                                       + (f" -> {c.grn(msg)}" if msg else "")))
        return f"{head} " + c.red(f"unknown: {data[:24].hex()}{'...' if len(data) > 24 else ''}")


if __name__ == "__main__":
    main()
