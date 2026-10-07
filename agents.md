# RC Car Access Context

## Target

- Toy: WIFI UFO / cooingdv / KY-UFO protocol family
- Expected device IP: `192.168.1.1`
- Expected UDP ports: `5007`, `7070`
- Candidate control port from prior notes: `7099`
- Expected RTSP endpoint: `rtsp://192.168.1.1:7070/webcam`
- Unlock packet: UDP payload `01 01`
- Confirmed product identification: Shantou Chenghai Honghuida Toys Factory, model `KF29`.
- Confirmed Wi-Fi SSID: `WIFI-UFO-0c4aad`.  (different per car to allow multi car play)
- Confirmed app: Google Play `wifi car`, package `com.cooingdv.wificar`.

## Existing tooling

`decode.py` is a standard-library Python decoder that:

- sends `01 01` periodically to UDP 5007 and 7070;
- decodes the known 8-byte and 21-byte control formats;
- fingerprints unknown datagrams;
- detects RTP/JPEG, MJPEG-over-UDP, raw JPEG/H.264, and RTSP text;
- optionally probes RTSP and saves reassembled JPEG frames.

Useful commands:

```powershell
# Use the bundled Python runtime if no system Python is installed.
& 'C:\Users\onedr\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' decode.py --probe --no-color
& 'C:\Users\onedr\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' decode.py --no-color --save-jpg --rtsp
```

## Latest observation (2026-09-24, Africa/Johannesburg)

Probe command:

```text
python bundled-runtime decode.py --probe --no-color
```

Result:

- TCP `5007`, `7060`, `7070`, `7099`, `8080`, `8030`, and `8888`: closed.
- UDP unlock on `5007` and `7070`: no response within the 2-second probe timeout.
- RTSP connection to `192.168.1.1:7070`: Windows `WinError 10013` (socket access forbidden by local permissions).

After the car was restarted:

- Host interface had `192.168.1.100` on the car subnet.
- `192.168.1.1` answered ICMP ping in 2–5 ms and had ARP entry `c4-28-70-ad-4a-0c`.
- Live listeners sent `01 01` every 2 seconds and listened on UDP `5007`, `7070`, `7099`, `8080`, and `8800`; no datagrams were received.
- TCP `7070` still did not accept a connection; Python RTSP attempts continued to report Windows `10013`.

## KF29-specific research

- Public KF29 manuals describe a 1:28 Wi-Fi FPV car with both a 2.4 GHz physical remote and smartphone app control.
- The manuals say to obtain the dedicated app from the packaging/manual QR code; the app name and network protocol are not specified.
- Reported Wi-Fi names are generic/variant-dependent (`FPV...` or `WIFI-UFO-...`), so the prior cooingdv/UFO identification is not confirmed for this car.
- The current `01 01` unlock, RTSP `/webcam`, and `66 ... 99` control assumptions remain hypotheses. Do not rely on them without a packet capture or app-specific evidence.
- The official app listing says this app controls a climbing car over Wi-Fi, receives live images, and supports joystick, gravity, trajectory, and speech control. This confirms the app is car-specific rather than the drone app.
- Best next reverse-engineering path: run the official app on an Android device while capturing traffic on the `WIFI-UFO-0c4aad` network, then extract the actual camera/control ports and startup packets.

## Confirmed from `PCAPdroid_24_Sept_22_50_19.pcap`

- App client observed as `10.215.173.1`; car is `192.168.1.1`.
- Control/handshake UDP destination is `192.168.1.1:7099`.
- App sends `01 01` repeatedly to UDP 7099.
- App control packets are 6 bytes: `03 33 <steering> <drive> 00 88`.
- Idle sample: `03 33 80 80 00 88`.
- Car sends repeated 15-byte responses to the app: `05 00 00 00 00 00 00 00 00 00 00 00 00 00 00`.
- RTSP is confirmed on TCP `192.168.1.1:7070`, path `/webcam`; SDP advertises RTP/JPEG payload type 26.
- RTSP negotiation used client UDP ports `10524-10525` and car/server UDP ports `52612-52613` in this capture.
- The decoder now recognizes the confirmed 6-byte car controls and 15-byte status response, and defaults to listening on UDP 7099.

## Camera tilt capture (2026-09-30)

From `PCAPdroid_30_Sept_16_48_54.pcap`, with neutral steering/drive:

- Tilt command A: `03 33 80 80 20 88` (six packets observed).
- Tilt command B: `03 33 80 80 10 88` (three packets observed).
- Physical testing confirmed the GUI mapping: `0x10` is Tilt up and `0x20` is Tilt down.

## Interpretation / next steps

Basic Wi-Fi/IP connectivity is now confirmed, but the expected application protocol has not produced traffic. The likely remaining possibilities are a required mobile-app handshake, a different port/packet format for this car firmware, or local policy affecting TCP/UDP behavior. Before changing the decoder:

1. Connect the computer to the car's Wi-Fi access point and verify the adapter has an address on the `192.168.1.0/24` network.
2. Check that `192.168.1.1` is reachable and that Windows Firewall/VPN/security software is not blocking Python UDP/TCP sockets.
3. Re-run `--probe`; if the device responds, run the listener with `--save-jpg --rtsp` and keep the generated `logs/raw_*.log`.
4. If the car uses another subnet, pass its address with `--ip` and test candidate UDP ports with repeated `--port` options.
5. Do not treat lack of a UDP reply as proof that unlock failed: the current code explicitly allows silent control ports and waits for inbound datagrams.

## Files

- `decode.py`: decoder, listener, RTSP probe, and packet fingerprinting.
- `notes.txt`: original target/protocol notes.
- `logs/`: generated runtime logs and saved frames.
