#!/usr/bin/env python3
"""Simple KF29 WiFi car controller GUI.

Confirmed facts: UDP 7099, packet 03 33 steering drive 00 88,
and RTSP video at rtsp://192.168.1.1:7070/webcam.
The fifth packet byte is an action/macro field, not confirmed camera tilt.
"""
import socket
import threading
import time
import json
import os
import tkinter as tk
from tkinter import ttk
try:
    import cv2
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    cv2 = None

DEFAULT_CONFIG = {
    "network": {"ip": "192.168.1.1", "control_port": 7099,
                 "rtsp_url": "rtsp://192.168.1.1:7070/webcam",
                 "rtsp_transport": "udp"},
    "packets": {"unlock": "0101", "control_prefix": "0333",
                "control_suffix": "88", "neutral_steering": 128,
                "neutral_drive": 128, "tilt_up_action": 16,
                "tilt_down_action": 32},
    "control": {"steering_min": 64, "steering_max": 192,
                 "drive_min": 0, "drive_max": 255,
                 "forward_value": 255, "reverse_value": 0,
                 "left_value": 64, "right_value": 192,
                 "dead_band": 0, "keyboard_step": 16,
                 "ramp_step": 5},
    "rates_ms": {"control_loop": 50, "unlock_interval": 2000,
                  "tilt_pulse": 25, "video_reconnect": 500}
}

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    try:
        with open(os.path.join(os.path.dirname(__file__), "config.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        for section in cfg:
            if isinstance(raw.get(section), dict):
                cfg[section] = {**cfg[section], **raw[section]}
    except (OSError, ValueError):
        pass
    return cfg

CONFIG = load_config()
CAR_IP = CONFIG["network"]["ip"]
CONTROL_PORT = int(CONFIG["network"]["control_port"])
RTSP_URL = CONFIG["network"]["rtsp_url"]
RTSP_TRANSPORT = CONFIG["network"].get("rtsp_transport", "udp")
PKT = CONFIG["packets"]
RATE = CONFIG["rates_ms"]
CTRL = CONFIG["control"]
NEUTRAL_STEERING = int(PKT["neutral_steering"])
NEUTRAL_DRIVE = int(PKT["neutral_drive"])
STEERING_MIN = int(CTRL["steering_min"])
STEERING_MAX = int(CTRL["steering_max"])
DRIVE_MIN = int(CTRL["drive_min"])
DRIVE_MAX = int(CTRL["drive_max"])
DEAD_BAND = int(CTRL["dead_band"])
TILT_UP = int(PKT["tilt_up_action"])
TILT_DOWN = int(PKT["tilt_down_action"])


class CarGui:
    def __init__(self, root):
        self.root = root
        root.title("KF29 WiFi Car")
        root.geometry("1100x820")
        root.minsize(980, 720)
        root.configure(bg="#15171c")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#15171c")
        style.configure("TLabel", background="#15171c", foreground="#e8eaf0", font=("Segoe UI", 10))
        style.configure("TLabelframe", background="#15171c", foreground="#a78bfa", bordercolor="#343844")
        style.configure("TLabelframe.Label", background="#15171c", foreground="#c4b5fd", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", background="#292d38", foreground="#f4f4f5",
                        bordercolor="#454b5b", focusthickness=2, focuscolor="#8b5cf6",
                        padding=(8, 5), font=("Segoe UI", 9, "bold"), width=11)
        style.map("TButton", background=[("active", "#4c1d95"), ("pressed", "#6d28d9")])
        style.configure("Title.TLabel", background="#15171c", foreground="#f4f4f5",
                        font=("Segoe UI", 18, "bold"), padding=(12, 8))
        style.configure("Subtitle.TLabel", background="#15171c", foreground="#a1a1aa",
                        font=("Segoe UI", 9), padding=(12, 0, 12, 8))
        style.configure("Status.TLabel", background="#20232b", foreground="#c4b5fd", padding=8,
                        font=("Consolas", 9))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.steering = NEUTRAL_STEERING
        self.drive = NEUTRAL_DRIVE
        self.target_steering = NEUTRAL_STEERING
        self.target_drive = NEUTRAL_DRIVE
        self.forward_pulse_ms = 100
        self.reverse_pulse_ms = 100
        self.active_pulse = None
        self.pulse_job = None
        self.pulse_mode = tk.BooleanVar(value=False)
        self.pulse_interval = tk.IntVar(value=100)
        self.next_pulse = 0.0
        self.pulse_release = 0.0
        self.tilt_byte = None
        self.brightness = 0
        self.last = "Idle"

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="KF29  /  WIFI CONTROL DECK", style="Title.TLabel", anchor="w").pack(side="left")
        ttk.Label(header, text="Live FPV · precision drive · camera tilt", style="Subtitle.TLabel", anchor="e").pack(side="right", padx=12)
        video_frame = tk.Frame(root, bg="#0d0f13", highlightthickness=1, highlightbackground="#343844")
        video_frame.pack(fill="both", expand=True, padx=0, pady=0)
        self.video = tk.Label(video_frame, text=("VIDEO DISPLAY\n" + RTSP_URL),
                              bg="#0d0f13", fg="#a1a1aa", height=15,
                              font=("Segoe UI", 11))
        self.video.pack(fill="both", expand=True)

        controls = ttk.Frame(root)
        controls.pack(fill="x", padx=12, pady=2)
        joystick_frame = ttk.LabelFrame(controls, text="Drive")
        joystick_frame.pack(side="left", padx=(0, 8))
        button_frame = ttk.LabelFrame(controls, text="Controls")
        button_frame.pack(side="right", fill="x", expand=True)
        ttk.Label(joystick_frame, text="Joystick").pack(pady=(3, 0))
        self.joy = tk.Canvas(joystick_frame, width=180, height=150, bg="#0d0f13",
                             highlightthickness=1, highlightbackground="#454b5b")
        self.joy.pack(padx=7, pady=3)
        self.joy.create_line(90, 12, 90, 138, fill="#343844")
        self.joy.create_line(12, 75, 168, 75, fill="#343844")
        self.joy_knob = self.joy.create_oval(76, 61, 104, 89, fill="#8b5cf6", outline="#c4b5fd")
        self.joy.bind("<Button-1>", self.joy_move)
        self.joy.bind("<B1-Motion>", self.joy_move)
        self.joy.bind("<ButtonRelease-1>", lambda e: self.center_wheels())
        ttk.Label(button_frame, text="WASD / arrow keys also work", justify="center").pack(pady=(2, 1))
        row = ttk.Frame(button_frame); row.pack()
        ttk.Button(row, text="▲ Forward", command=lambda: self.set_drive(int(CTRL["forward_value"]))).grid(row=0, column=0, padx=4, pady=3)
        ttk.Button(row, text="■ Stop", command=self.neutral).grid(row=1, column=1, padx=4, pady=3)
        ttk.Button(row, text="▼ Reverse", command=lambda: self.set_drive(int(CTRL["reverse_value"]))).grid(row=0, column=2, padx=4, pady=3)
        ttk.Button(row, text="◀ Left", command=lambda: self.set_steering(int(CTRL["left_value"]))).grid(row=1, column=0, padx=4, pady=3)
        ttk.Button(row, text="Right ▶", command=lambda: self.set_steering(int(CTRL["right_value"]))).grid(row=1, column=2, padx=4, pady=3)
        ttk.Button(row, text="⟲ Pulse left", command=lambda: self.pulse_steering(int(CTRL["left_value"]))).grid(row=2, column=0, padx=4, pady=3)
        ttk.Button(row, text="Center wheels", command=self.center_wheels).grid(row=2, column=1, padx=4, pady=3)
        ttk.Button(row, text="Pulse right ⟳", command=lambda: self.pulse_steering(int(CTRL["right_value"]))).grid(row=2, column=2, padx=4, pady=3)
        ttk.Button(row, text="Tilt up", command=lambda: self.pulse_tilt(TILT_UP)).grid(row=0, column=3, padx=(14, 4), pady=3)
        ttk.Button(row, text="Tilt down", command=lambda: self.pulse_tilt(TILT_DOWN)).grid(row=1, column=3, padx=(14, 4), pady=3)
        pulse_options = ttk.Frame(button_frame)
        pulse_options.pack(pady=(2, 3))
        ttk.Checkbutton(pulse_options, text="Pulse mode", variable=self.pulse_mode,
                        command=self.update_pulse_mode).pack(side="left", padx=6)
        ttk.Label(pulse_options, text="Base interval (ms)").pack(side="left", padx=(10, 3))
        ttk.Spinbox(pulse_options, from_=50, to=1000, increment=25,
                    textvariable=self.pulse_interval, width=6).pack(side="left")
        ttk.Label(pulse_options, text="Display brightness").pack(side="left", padx=(14, 3))
        self.brightness_scale = ttk.Scale(pulse_options, from_=-40, to=100,
                                          length=130, command=self.set_brightness)
        self.brightness_scale.set(0)
        self.brightness_scale.pack(side="left")
        diag = ttk.LabelFrame(root, text="Macros / Servo probe  ")
        diag.pack(fill="x", padx=12, pady=2)
        for i in range(6):
            ttk.Button(diag, text=f"Probe 0x{i:02x}",
                       command=lambda v=i: self.probe_servo(v)).grid(row=0, column=i, padx=2, pady=3)
        ttk.Button(diag, text="Neutral / stop", command=self.neutral).grid(row=0, column=6, padx=4, pady=3)
        self.status = ttk.Label(root, text="Starting...", style="Status.TLabel", anchor="w")
        self.status.pack(fill="x", padx=12, pady=(4, 10))

        root.bind("<KeyPress>", self.key_down)
        root.bind("<KeyRelease>", self.key_up)
        root.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.io_loop, daemon=True).start()
        if cv2 is not None:
            threading.Thread(target=self.video_loop, daemon=True).start()
        else:
            self.video.configure(text="OpenCV is not installed")

    def packet(self):
        packet = bytes.fromhex(PKT["control_prefix"]) + bytes((self.steering, self.drive, 0x00)) + bytes.fromhex(PKT["control_suffix"])
        if len(packet) != 6:
            raise ValueError(f"control packet must be 6 bytes, got {len(packet)}")
        return packet

    def set_drive(self, value):
        self.drive = value
        self.target_drive = value
        self.last = f"Drive 0x{value:02x}"

    def set_steering(self, value):
        self.steering = value
        self.target_steering = value
        self.last = f"Steering 0x{value:02x}"

    def joy_move(self, event):
        x = max(14, min(166, event.x))
        y = max(14, min(136, event.y))
        self.move_joystick_knob(x, y)
        self.target_steering = int(NEUTRAL_STEERING + (x - 90) * (STEERING_MAX - STEERING_MIN) / 2 / 76)
        self.target_drive = int(NEUTRAL_DRIVE + (75 - y) * (DRIVE_MAX - DRIVE_MIN) / 2 / 61)
        self.target_steering = max(STEERING_MIN, min(STEERING_MAX, self.target_steering))
        self.target_drive = max(DRIVE_MIN, min(DRIVE_MAX, self.target_drive))
        if abs(self.target_steering - NEUTRAL_STEERING) <= DEAD_BAND: self.target_steering = NEUTRAL_STEERING
        if abs(self.target_drive - NEUTRAL_DRIVE) <= DEAD_BAND: self.target_drive = NEUTRAL_DRIVE
        self.last = f"Joystick target steering=0x{self.target_steering:02x} drive=0x{self.target_drive:02x}"

    def move_joystick_knob(self, x, y):
        self.joy.coords(self.joy_knob, x - 14, y - 14, x + 14, y + 14)

    def nudge_joystick(self, key):
        step = int(CTRL["keyboard_step"])
        if key == "w": self.target_drive = min(DRIVE_MAX, self.target_drive + step)
        elif key == "s": self.target_drive = max(DRIVE_MIN, self.target_drive - step)
        elif key == "a": self.target_steering = max(STEERING_MIN, self.target_steering - step)
        elif key == "d": self.target_steering = min(STEERING_MAX, self.target_steering + step)
        x = int(90 + (self.target_steering - NEUTRAL_STEERING) * 76 / max(1, (STEERING_MAX - STEERING_MIN) / 2))
        y = int(75 - (self.target_drive - NEUTRAL_DRIVE) * 61 / max(1, (DRIVE_MAX - DRIVE_MIN) / 2))
        self.move_joystick_knob(max(14, min(166, x)), max(14, min(136, y)))
        self.last = f"Keyboard nudge {key.upper()} target steering=0x{self.target_steering:02x} drive=0x{self.target_drive:02x}"

    def update_pulse_mode(self):
        self.next_pulse = 0.0
        self.pulse_release = 0.0
        if not self.pulse_mode.get():
            self.drive = 0x80
            self.last = "Pulse mode off / neutral"
        else:
            self.last = "Pulse mode on"

    def set_brightness(self, value):
        self.brightness = int(float(value))

    def pulse_steering(self, value, duration_ms=140):
        self.steering = value
        self.drive = 0x80
        self.last = f"Pulse steering 0x{value:02x}"
        self.root.after(duration_ms, self.center_wheels)

    def pulse_tilt(self, value, duration_ms=None):
        duration_ms = int(RATE["tilt_pulse"] if duration_ms is None else duration_ms)
        self.tilt_byte = value
        self.steering = self.drive = 0x80
        self.last = f"Camera tilt command 0x{value:02x}"
        self.root.after(duration_ms, self.stop_tilt)

    def stop_tilt(self):
        self.tilt_byte = None
        self.last = "Camera tilt stopped"

    def pulse_drive(self, value):
        if value == 0xFF:
            duration = self.forward_pulse_ms
            self.forward_pulse_ms = min(duration + 100, 1000)
            direction = "forward"
        else:
            duration = self.reverse_pulse_ms
            self.reverse_pulse_ms = min(duration + 100, 1000)
            direction = "backward"
        self.steering = 0x80
        self.drive = value
        self.last = f"Pulse {direction} {duration} ms"
        self.root.after(duration, self.neutral_after_pulse)

    def toggle_pulse_drive(self, value):
        if self.active_pulse == value:
            self.stop_pulse_drive()
            return
        self.stop_pulse_drive()
        self.active_pulse = value
        self.forward_pulse_ms = self.reverse_pulse_ms = 75
        self.repeat_pulse_drive()

    def repeat_pulse_drive(self):
        if self.active_pulse is None:
            return
        value = self.active_pulse
        if value == 0xFF:
            duration = self.forward_pulse_ms
            self.forward_pulse_ms = min(duration + 25, 750)
            direction = "forward"
        else:
            duration = self.reverse_pulse_ms
            self.reverse_pulse_ms = min(duration + 25, 750)
            direction = "backward"
        self.steering = 0x80
        self.drive = value
        self.last = f"Repeating {direction} pulse {duration} ms"
        self.pulse_job = self.root.after(duration, self.pulse_gap)

    def pulse_gap(self):
        self.steering = self.drive = 0x80
        if self.active_pulse is not None:
            self.pulse_job = self.root.after(100, self.repeat_pulse_drive)

    def stop_pulse_drive(self):
        self.active_pulse = None
        if self.pulse_job is not None:
            self.root.after_cancel(self.pulse_job)
            self.pulse_job = None
        self.steering = self.drive = 0x80
        self.last = "Repeating pulse stopped / neutral"

    def neutral_after_pulse(self):
        self.steering = self.drive = 0x80
        self.last = "Pulse complete / neutral"

    def neutral(self):
        self.stop_pulse_drive()
        self.steering = self.drive = 0x80
        self.target_steering = self.target_drive = 0x80
        self.tilt_byte = None
        if hasattr(self, "joy"):
            self.joy.coords(self.joy_knob, 76, 61, 104, 89)
        self.last = "Stop / neutral"

    def center_wheels(self):
        self.stop_pulse_drive()
        self.steering = 0x80
        self.drive = 0x80
        self.target_steering = 0x80
        self.target_drive = 0x80
        self.forward_pulse_ms = 100
        self.reverse_pulse_ms = 100
        if hasattr(self, "joy"):
            self.joy.coords(self.joy_knob, 76, 61, 104, 89)
        self.last = "Wheels centered / neutral"

    def probe_servo(self, value):
        # Keep both driving axes neutral; only vary the otherwise-unused byte.
        self.steering = self.drive = 0x80
        self.diagnostic_byte = value
        self.last = f"Servo probe 03 33 80 80 {value:02x} 88"

    def key_down(self, event):
        if event.keysym.lower() == "space":
            self.center_wheels()
            return
        if event.keysym.lower() in ("w", "a", "s", "d"):
            self.nudge_joystick(event.keysym.lower())
            return
        if event.char in "123456789":
            self.steering = self.drive = 0x80
            self.diagnostic_byte = int(event.char)
            self.last = f"Macro {event.char}"
            return
        keys = {"Up": ("drive", int(CTRL["forward_value"])), "Down": ("drive", int(CTRL["reverse_value"])),
                "Left": ("steer", int(CTRL["left_value"])), "Right": ("steer", int(CTRL["right_value"])),
                "w": ("drive", int(CTRL["forward_value"])), "s": ("drive", int(CTRL["reverse_value"])),
                "a": ("steer", int(CTRL["left_value"])), "d": ("steer", int(CTRL["right_value"]))}
        if event.keysym in keys:
            kind, value = keys[event.keysym]
            if kind == "drive": self.set_drive(value)
            else: self.set_steering(value)

    def key_up(self, event):
        if event.keysym in ("Up", "Down"):
            self.drive = 0x80
            self.target_drive = 0x80
            self.move_joystick_knob(90, 75)
        elif event.keysym in ("Left", "Right"):
            self.steering = 0x80
            self.target_steering = 0x80
            self.move_joystick_knob(90, 75)

    def io_loop(self):
        while not self.stop.is_set():
            try:
                now = time.monotonic()
                if now >= getattr(self, "next_unlock", 0.0):
                    self.sock.sendto(bytes.fromhex(PKT["unlock"]), (CAR_IP, CONTROL_PORT))
                    self.next_unlock = now + int(RATE["unlock_interval"]) / 1000.0
                ramp = max(1, int(CTRL["ramp_step"]))
                self.steering += max(-ramp, min(ramp, self.target_steering - self.steering))
                self.drive += max(-ramp, min(ramp, self.target_drive - self.drive))
                now = time.monotonic()
                drive = self.drive
                if self.pulse_mode.get() and drive != NEUTRAL_DRIVE:
                    magnitude = abs(drive - NEUTRAL_DRIVE)
                    base = max(50, min(1000, int(self.pulse_interval.get())))
                    # Treat the configured value as the fastest pulse rate.
                    # Smaller joystick deflections stretch the interval
                    # smoothly instead of creating an unusably fast stream.
                    fraction = magnitude / 127.0
                    interval = max(base, int(base + (1.0 - fraction) * base * 2.0))
                    if now < self.pulse_release:
                        drive = NEUTRAL_DRIVE
                    elif now >= self.next_pulse:
                        self.next_pulse = now + interval / 1000.0
                        self.pulse_release = now + min(45, interval / 3) / 1000.0
                    else:
                        drive = NEUTRAL_DRIVE
                action = self.tilt_byte if self.tilt_byte is not None else 0x00
                packet = bytes.fromhex(PKT["control_prefix"]) + bytes((self.steering, drive, action)) + bytes.fromhex(PKT["control_suffix"])
                if hasattr(self, "diagnostic_byte"):
                    packet = bytes.fromhex(PKT["control_prefix"]) + bytes((NEUTRAL_STEERING, NEUTRAL_DRIVE, self.diagnostic_byte)) + bytes.fromhex(PKT["control_suffix"])
                    del self.diagnostic_byte
                if len(packet) != 6:
                    raise ValueError(f"control packet must be 6 bytes, got {len(packet)}")
                self.sock.sendto(packet, (CAR_IP, CONTROL_PORT))
                self.sock.settimeout(0.05)
                data, addr = self.sock.recvfrom(256)
                msg = f"{self.last} | {addr[0]}:{addr[1]} | {data.hex()}"
            except (socket.timeout, OSError):
                msg = f"{self.last} | no response"
            self.root.after(0, self.status.configure, {"text": msg})
            self.stop.wait(max(0.005, int(RATE["control_loop"]) / 1000.0))

    def video_loop(self):
        # The KF29 advertises RTP/JPEG and the captured app session uses UDP.
        # Explicitly select the transport so FFmpeg does not default to TCP.
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{RTSP_TRANSPORT}"
        cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
        while not self.stop.is_set():
            ok, frame = cap.read()
            if not ok:
                self.root.after(0, self.video.configure,
                                {"text": "Waiting for RTSP video..."})
                self.stop.wait(int(RATE["video_reconnect"]) / 1000.0)
                cap.release()
                cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
                continue
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if self.brightness:
                frame = cv2.convertScaleAbs(frame, alpha=1.0, beta=self.brightness)
            image = Image.fromarray(frame)
            photo = ImageTk.PhotoImage(image=image)
            self.root.after(0, self.show_frame, photo)
        cap.release()

    def show_frame(self, photo):
        width = max(1, self.video.winfo_width())
        height = max(1, self.video.winfo_height())
        image = ImageTk.getimage(photo)
        image = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (width, height), "#0d0f13")
        canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
        photo = ImageTk.PhotoImage(image=canvas)
        self.video.configure(image=photo, text="")
        self.video.image = photo

    def close(self):
        self.neutral()
        try:
            self.sock.sendto(self.packet(), (CAR_IP, CONTROL_PORT))
        except OSError:
            pass
        self.stop.set()
        self.sock.close()
        self.root.destroy()


if __name__ == "__main__":
    app = tk.Tk()
    CarGui(app)
    app.mainloop()
