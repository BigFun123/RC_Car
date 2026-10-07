import struct, socket, sys

path = sys.argv[1]
with open(path, 'rb') as f:
    data = f.read()
magic = data[:4]
if magic == b'\xd4\xc3\xb2\xa1': endian = '<'
elif magic == b'\xa1\xb2\xc3\xd4': endian = '>'
else: raise SystemExit('unsupported pcap format')
_, _, _, _, _, _, link = struct.unpack(endian + 'IHHIIII', data[:24])
pos = 24
flows = {}
udp = []
tcp = []
while pos + 16 <= len(data):
    sec, usec, incl, orig = struct.unpack(endian + 'IIII', data[pos:pos+16]); pos += 16
    pkt = data[pos:pos+incl]; pos += incl
    if link == 1:
        if len(pkt) < 34: continue
        eth = struct.unpack('>H', pkt[12:14])[0]
        if eth != 0x0800: continue
        ip = pkt[14:]
    elif link == 101:
        if len(pkt) < 20: continue
        ip = pkt
    else:
        continue
    ihl = (ip[0] & 15) * 4
    if len(ip) < ihl + 8: continue
    proto = ip[9]
    src = socket.inet_ntoa(ip[12:16]); dst = socket.inet_ntoa(ip[16:20])
    if proto not in (6,17): continue
    sport, dport = struct.unpack('>HH', ip[ihl:ihl+4])
    key = (proto, src, sport, dst, dport)
    flows[key] = flows.get(key, 0) + 1
    if proto == 17:
        ulen = struct.unpack('>H', ip[ihl+4:ihl+6])[0]
        payload = ip[ihl+8:ihl+ulen]
        udp.append((sec + usec/1e6, src, sport, dst, dport, payload))
    else:
        thl = (ip[ihl+12] >> 4) * 4
        payload = ip[ihl+thl:]
        if payload:
            tcp.append((sec + usec/1e6, src, sport, dst, dport, payload))
print('LINK', link, 'FLOWS', len(flows), 'UDP_DATAGRAMS', len(udp))
print('TCP_PAYLOADS', len(tcp))
print('\nFLOWS')
for k,n in sorted(flows.items(), key=lambda x: -x[1]): print(n, k)
print('\nUDP PAYLOADS')
counts = {}
for t,src,sp,dst,dp,p in udp:
    if dst == '192.168.1.1' or src == '192.168.1.1':
        key = (src, sp, dst, dp, len(p), p.hex())
        counts[key] = counts.get(key, 0) + 1
for k,n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
    print('COUNT', n, k)
print('\nFIRST PACKETS TO/FROM CAR')
shown = 0
for t,src,sp,dst,dp,p in udp:
    if dst == '192.168.1.1' or src == '192.168.1.1':
        print(f'{t:.6f} {src}:{sp} -> {dst}:{dp} len={len(p)} hex={p.hex()}')
        shown += 1
        if shown >= 80: break
print('\nTCP PAYLOADS')
for t,src,sp,dst,dp,p in tcp:
    if dst == '192.168.1.1' or src == '192.168.1.1':
        print(f'{t:.6f} {src}:{sp} -> {dst}:{dp} len={len(p)} hex={p.hex()} text={p[:300]!r}')
