"""SteamVR overlay: show the HMD proximity (wear-detection) state live, just below your view.

Read-only: it never changes SteamVR settings. Interface table indices come from the SteamVR.dll
that ships with SteamVR (IVRSystem_019 / IVROverlay_019 FnTables).
The raw proximity count comes from the axis stream of SteamVR's own lighthouse_console
(only the serial/axis/dump/sleep commands are sent).

Usage: python -I proximity_overlay.py [--console] [--log PATH] [--seconds N] [--serial LHR-xxxxxxxx] [--lang en|zh]
  --console  no overlay, print to the terminal only (self-test / logging)
Requires Pillow (pip install pillow).
"""
import argparse
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import POINTER, Structure, byref, c_bool, c_char_p, c_float, c_int, c_uint32, c_uint64, c_void_p
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

OPENVR_DLL = r"C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\openvr_api.dll"
LIGHTHOUSE_CONSOLE = r"C:\Program Files (x86)\Steam\steamapps\common\SteamVR\tools\lighthouse\bin\win64\lighthouse_console.exe"
PRESENCEFIX_INI = r"C:\Program Files (x86)\Steam\steamapps\common\Beat Saber\UserData\PresenceFix.ini"
PROX_LINE = re.compile(r"proximity=(\d+) \(([\d.]+)\) prox_triggered=(\d)")
RECEIVER_LINE = re.compile(r"^\s+(LHR-[0-9A-Fa-f]+)\s*$")
VRApplication_Overlay = 2
VRApplication_Background = 3
k_EButton_ProximitySensor = 31
HMD = 0

ACTIVITY = {-1: "Unknown", 0: "Idle", 1: "UserInteraction", 2: "UserInteraction_Timeout", 3: "Standby"}

TEXT = {
    "en": {"font": r"C:\Windows\Fonts\segoeui.ttf", "worn": "WORN", "off": "OFF",
           "firmware": "firmware", "software": "software", "on_range": "trig", "off_range": "idle"},
    "zh": {"font": r"C:\Windows\Fonts\msyh.ttc", "worn": "佩戴", "off": "离开",
           "firmware": "固件", "software": "软件", "on_range": "触发", "off_range": "未触发"},
}


class VRControllerAxis(Structure):
    _fields_ = [("x", c_float), ("y", c_float)]


class VRControllerState(Structure):
    _fields_ = [("unPacketNum", c_uint32), ("ulButtonPressed", c_uint64), ("ulButtonTouched", c_uint64),
                ("rAxis", VRControllerAxis * 5)]


class HmdMatrix34(Structure):
    _fields_ = [("m", c_float * 12)]


def fn_table(vr, version, entries):
    """Fetch function pointers in FnTable order. entries: [(name, restype, argtypes)], list index = table index."""
    err = c_int(0)
    table = vr.VR_GetGenericInterface(f"FnTable:{version}".encode(), byref(err))
    if not table or err.value:
        raise RuntimeError(f"Failed to get FnTable:{version} err={err.value}")
    ptrs = ctypes.cast(table, POINTER(c_void_p))
    funcs = {}
    for index, (name, restype, argtypes) in enumerate(entries):
        if name:
            funcs[name] = ctypes.CFUNCTYPE(restype, *argtypes)(ptrs[index])
    return funcs


def kill_with_parent(proc):
    """Put the child in a KILL_ON_JOB_CLOSE job so lighthouse_console dies with this process."""
    class BasicLimit(Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", c_uint32), ("SchedulingClass", c_uint32)]

    class ExtendedLimit(Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = c_void_p
    k32.OpenProcess.restype = c_void_p
    job = k32.CreateJobObjectW(None, None)
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(c_void_p(job), 9, byref(info), ctypes.sizeof(info))
    handle = k32.OpenProcess(0x0101, False, proc.pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
    k32.AssignProcessToJobObject(c_void_p(job), c_void_p(handle))
    return job  # kept open for the lifetime of this process


class RawProximity:
    """Read the raw proximity count from lighthouse_console in the background and track the
    value range seen on each side of the firmware trigger."""

    def __init__(self, serial, out):
        self.serial = serial
        self.out = out
        self.value = None
        self.triggered = None
        self.on_range = None   # (min, max) seen while prox_triggered=1
        self.off_range = None  # (min, max) seen while prox_triggered=0
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(
            [LIGHTHOUSE_CONSOLE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert self.proc.stdin and self.proc.stdout
        self.job = kill_with_parent(self.proc)
        threading.Thread(target=self._read, daemon=True).start()

    @staticmethod
    def _widen(rng, v):
        return (v, v) if rng is None else (min(rng[0], v), max(rng[1], v))

    def _send_commands(self, serial):
        assert self.proc.stdin
        # The count is in the axis stream; the console only keeps printing while a sleep command runs
        self.proc.stdin.write((f"serial {serial}\n" if serial else "") + "axis\ndump\nsleep 86400000\n")
        self.proc.stdin.flush()
        self.out(f"reading proximity from {serial or 'default receiver'}")

    def _read(self):
        assert self.proc.stdout
        sent = False
        first_receiver = None
        for line in self.proc.stdout:
            if not sent:
                m = RECEIVER_LINE.match(line)
                if m and first_receiver is None:
                    first_receiver = m.group(1)
                # Send commands once the device list is printed; with no --serial, pick the first
                # LHR- receiver (normally the HMD)
                if "Connected to receiver" in line or line.startswith("lh>"):
                    self._send_commands(self.serial or first_receiver)
                    sent = True
                continue
            m = PROX_LINE.search(line)
            if not m:
                continue
            v, t = int(m.group(1)), int(m.group(3))
            with self.lock:
                self.value, self.triggered = v, t
                if t:
                    self.on_range = self._widen(self.on_range, v)
                else:
                    self.off_range = self._widen(self.off_range, v)

    def snapshot(self):
        with self.lock:
            return self.value, self.triggered, self.on_range, self.off_range

    def close(self):
        # stdin is not read during sleep, so just kill the read-only console
        self.proc.kill()
        self.proc.wait(timeout=3)


def system_entries():
    entries: list[tuple] = [(None, None, None)] * 47
    entries[16] = ("GetTrackedDeviceActivityLevel", c_int, [c_uint32])
    entries[20] = ("GetTrackedDeviceClass", c_int, [c_uint32])
    entries[22] = ("GetBoolTrackedDeviceProperty", c_bool, [c_uint32, c_int, POINTER(c_int)])
    entries[28] = ("GetStringTrackedDeviceProperty", c_uint32,
                   [c_uint32, c_int, c_char_p, c_uint32, POINTER(c_int)])
    entries[34] = ("GetControllerState", c_bool, [c_uint32, POINTER(VRControllerState), c_uint32])
    entries[37] = ("GetButtonIdNameFromEnum", c_char_p, [c_int])
    return entries


def overlay_entries():
    entries: list[tuple] = [(None, None, None)] * 82
    entries[1] = ("CreateOverlay", c_int, [c_char_p, c_char_p, POINTER(c_uint64)])
    entries[2] = ("DestroyOverlay", c_int, [c_uint64])
    entries[20] = ("SetOverlaySortOrder", c_int, [c_uint64, c_uint32])
    entries[22] = ("SetOverlayWidthInMeters", c_int, [c_uint64, c_float])
    entries[35] = ("SetOverlayTransformTrackedDeviceRelative", c_int, [c_uint64, c_uint32, POINTER(HmdMatrix34)])
    entries[41] = ("ShowOverlay", c_int, [c_uint64])
    entries[61] = ("SetOverlayFromFile", c_int, [c_uint64, c_char_p])
    return entries


class SoftThreshold:
    """Read the PresenceFix plugin's UserData/PresenceFix.ini and apply the same hysteresis +
    leave-debounce rule, so thresholds can be tuned against the live reading."""

    def __init__(self, path):
        self.path = path
        self.mode, self.on, self.off, self.delay = "AlwaysWorn", 260, 220, 1.0
        self._mtime = None
        self._next_check = 0.0
        self._worn = True
        self._below_since = None

    def poll(self, now):
        if now < self._next_check:
            return
        self._next_check = now + 1.0
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        with open(self.path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in ";#" or "=" not in line:
                    continue
                key, value = (s.strip() for s in line.split("=", 1))
                try:
                    if key == "Mode":
                        self.mode = "Threshold" if value.lower() == "threshold" else "AlwaysWorn"
                    elif key == "OnThreshold":
                        self.on = int(value)
                    elif key == "OffThreshold":
                        self.off = int(value)
                    elif key == "OffDelaySeconds":
                        self.delay = float(value)
                except ValueError:
                    pass
        self.off = min(self.off, self.on - 1)

    def evaluate(self, value, now):
        if value >= self.on:
            self._worn, self._below_since = True, None
        elif value <= self.off:
            self._below_since = now if self._below_since is None else self._below_since
            if now - self._below_since >= self.delay:
                self._worn = False
        else:
            self._below_since = None
        return self._worn


class Panel:
    W, H = 640, 200

    def __init__(self, text):
        self.text = text
        self.big = ImageFont.truetype(text["font"], 96)
        self.small = ImageFont.truetype(text["font"], 26)

    def render(self, worn, info, raw=None) -> Image.Image:
        t = self.text
        img = Image.new("RGBA", (self.W, self.H), (18, 18, 20, 210))
        draw = ImageDraw.Draw(img)
        color = (64, 200, 110, 255) if worn else (235, 80, 70, 255)
        dim = (200, 200, 205, 255)
        draw.rectangle([0, 0, 18, self.H], fill=color)
        draw.text((48, 18), t["worn"] if worn else t["off"], font=self.big, fill=color)
        draw.text((48, 150), info, font=self.small, fill=dim)
        if raw and raw[0] is not None:
            value, _, on_range, off_range = raw
            draw.text((self.W - 40, 30), str(value), font=self.big, fill=(235, 235, 240, 255), anchor="ra")
            fmt = lambda r: "-" if r is None else f"{r[0]}-{r[1]}"
            draw.text((self.W - 40, 118), f"{t['on_range']} {fmt(on_range)}   {t['off_range']} {fmt(off_range)}",
                      font=self.small, fill=dim, anchor="ra")
        return img


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--console", action="store_true")
    parser.add_argument("--log")
    parser.add_argument("--seconds", type=float, default=0, help="run time; 0 = until Ctrl+C")
    parser.add_argument("--serial", default="", help="HMD serial (LHR-xxxxxxxx); empty = auto-detect")
    parser.add_argument("--no-raw", action="store_true", help="do not start lighthouse_console")
    parser.add_argument("--ini", default=PRESENCEFIX_INI, help="PresenceFix.ini to mirror the software threshold from")
    parser.add_argument("--lang", choices=sorted(TEXT), default="en")
    parser.add_argument("--interval", type=float, default=0.1, help="poll interval in seconds")
    args = parser.parse_args()
    log = open(args.log, "a", encoding="utf-8") if args.log else None
    t = TEXT[args.lang]

    def out(msg):
        line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + "  " + msg
        print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()

    vr = ctypes.CDLL(OPENVR_DLL)
    vr.VR_InitInternal2.restype = c_uint32
    vr.VR_InitInternal2.argtypes = [POINTER(c_int), c_int, c_char_p]
    vr.VR_GetGenericInterface.restype = c_void_p
    vr.VR_GetGenericInterface.argtypes = [c_char_p, POINTER(c_int)]

    err = c_int(0)
    vr.VR_InitInternal2(byref(err), VRApplication_Background if args.console else VRApplication_Overlay, None)
    if err.value:
        out(f"VR_InitInternal2 failed err={err.value} (is SteamVR running?)")
        return 1

    handle = c_uint64(0)
    ov = None
    raw_src = None if args.no_raw else RawProximity(args.serial, out)
    try:
        sysf = fn_table(vr, "IVRSystem_019", system_entries())
        # Self-test: if the table indices were off, these two would not match
        button_name = sysf["GetButtonIdNameFromEnum"](k_EButton_ProximitySensor)
        if sysf["GetTrackedDeviceClass"](HMD) != 1 or button_name != b"k_EButton_ProximitySensor":
            out(f"self-test failed class={sysf['GetTrackedDeviceClass'](HMD)} button31={button_name!r}")
            return 1

        def create_overlay():
            assert ov is not None
            r = ov["CreateOverlay"](b"local.proximity-indicator", b"Proximity", byref(handle))
            if r:
                out(f"CreateOverlay failed EVROverlayError={r}")
                return False
            ov["SetOverlayWidthInMeters"](handle, 0.22)
            ov["SetOverlaySortOrder"](handle, 100)
            # HMD space: 0.9 m ahead, 0.28 m down, tilted slightly up
            mat = HmdMatrix34()
            mat.m[:] = [1, 0, 0, 0,
                        0, 0.966, 0.259, -0.28,
                        0, -0.259, 0.966, -0.9]
            ov["SetOverlayTransformTrackedDeviceRelative"](handle, HMD, byref(mat))
            ov["ShowOverlay"](handle)
            return True

        if not args.console:
            ov = fn_table(vr, "IVROverlay_019", overlay_entries())
            if not create_overlay():
                return 1

        # SetOverlayRaw starts failing with RequestFailed (23) after a few hundred calls, so render
        # to PNG and let the compositor load it; alternate two files to avoid path caching
        png_paths = [os.path.join(tempfile.gettempdir(), f"proximity_overlay_{i}.png") for i in (0, 1)]
        uploads = 0

        panel = Panel(t)
        soft = SoftThreshold(args.ini)
        state = VRControllerState()
        last = None
        last_frame = None
        last_upload = 0.0
        end = time.monotonic() + args.seconds if args.seconds else None
        while end is None or time.monotonic() < end:
            ok = sysf["GetControllerState"](HMD, byref(state), ctypes.sizeof(VRControllerState))
            worn = bool(state.ulButtonPressed >> k_EButton_ProximitySensor & 1) if ok else False
            activity = sysf["GetTrackedDeviceActivityLevel"](HMD)
            raw = raw_src.snapshot() if raw_src else None
            # While lighthouse_console runs, GetControllerState may keep returning false;
            # prefer the firmware trigger bit from the raw stream
            if raw and raw[1] is not None:
                worn = bool(raw[1])
            now = time.monotonic()
            soft.poll(now)
            if soft.mode == "Threshold" and raw and raw[0] is not None:
                shown = soft.evaluate(raw[0], now)
                info = f"{t['firmware']} {t['worn'] if worn else t['off']}   {t['software']} >={soft.on} / <={soft.off}"
            else:
                shown = worn
                info = f"{t['firmware']}   activity={activity} {ACTIVITY.get(activity, '?')}"
            current = (ok, worn, activity, raw, shown, info)
            if current != last:
                raw_text = "" if not raw or raw[0] is None else \
                    f"  raw={raw[0]} prox_triggered={raw[1]} trig_range={raw[2]} idle_range={raw[3]}"
                out(f"proximity={int(worn)} activity={activity} {ACTIVITY.get(activity, '?')}" + raw_text
                    + f"  shown={'worn' if shown else 'off'}" + ("" if ok else "  GetControllerState=false"))
                last = current
            # Upload when the picture changes, plus a refresh every 5 s as a fallback
            frame = (shown, info, raw[0] if raw else None, raw[2] if raw else None, raw[3] if raw else None)
            if ov and (frame != last_frame or time.monotonic() - last_upload > 5.0):
                path = png_paths[uploads % 2]
                panel.render(shown, info, raw).save(path)
                r = ov["SetOverlayFromFile"](handle, path.encode("mbcs"))
                uploads += 1
                if r:
                    out(f"SetOverlayFromFile EVROverlayError={r} (upload #{uploads}), recreating overlay")
                    ov["DestroyOverlay"](handle)
                    create_overlay()
                last_frame = frame
                last_upload = time.monotonic()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if raw_src:
            raw_src.close()
        if ov and handle.value:
            ov["DestroyOverlay"](handle)
        vr.VR_ShutdownInternal()
        out("done")
        if log:
            log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
