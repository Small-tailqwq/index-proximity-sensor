"""Read-only probe: open a render-less OpenXR session (XR_MND_headless) and log XR_EXT_user_presence events.

This is the same value Beat Saber 1.45+ reads (UnityOpenXR caches XrEventDataUserPresenceChangedEXT.isUserPresent).
Usage: python -I openxr_presence_probe.py [--seconds N] [--log PATH] [--loader PATH]
The default loader is the openxr_loader.dll shipped with Beat Saber; any OpenXR loader works.
"""
import argparse
import ctypes
import sys
import time
from ctypes import POINTER, Structure, byref, c_char, c_char_p, c_int32, c_uint32, c_uint64, c_void_p
from datetime import datetime

DEFAULT_LOADER = r"C:\Program Files (x86)\Steam\steamapps\common\Beat Saber\Beat Saber_Data\Plugins\x86_64\openxr_loader.dll"

XR_TYPE_INSTANCE_CREATE_INFO = 3
XR_TYPE_SYSTEM_GET_INFO = 4
XR_TYPE_SYSTEM_PROPERTIES = 5
XR_TYPE_SESSION_CREATE_INFO = 8
XR_TYPE_SESSION_BEGIN_INFO = 10
XR_TYPE_EVENT_DATA_BUFFER = 16
XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED = 18
XR_TYPE_EVENT_DATA_USER_PRESENCE_CHANGED_EXT = 1000470000
XR_TYPE_SYSTEM_USER_PRESENCE_PROPERTIES_EXT = 1000470001
XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY = 1
XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO = 2
XR_SESSION_STATE_READY = 2
XR_EVENT_UNAVAILABLE = 4
API_VERSION_1_0_34 = (1 << 48) | 34

STATES = ["UNKNOWN", "IDLE", "READY", "SYNCHRONIZED", "VISIBLE", "FOCUSED", "STOPPING", "LOSS_PENDING", "EXITING"]


class XrApplicationInfo(Structure):
    _fields_ = [("applicationName", c_char * 128), ("applicationVersion", c_uint32),
                ("engineName", c_char * 128), ("engineVersion", c_uint32), ("apiVersion", c_uint64)]


class XrInstanceCreateInfo(Structure):
    _fields_ = [("type", c_int32), ("next", c_void_p), ("createFlags", c_uint64),
                ("applicationInfo", XrApplicationInfo),
                ("enabledApiLayerCount", c_uint32), ("enabledApiLayerNames", POINTER(c_char_p)),
                ("enabledExtensionCount", c_uint32), ("enabledExtensionNames", POINTER(c_char_p))]


class XrSystemGetInfo(Structure):
    _fields_ = [("type", c_int32), ("next", c_void_p), ("formFactor", c_int32)]


class XrSystemUserPresencePropertiesEXT(Structure):
    _fields_ = [("type", c_int32), ("next", c_void_p), ("supportsUserPresence", c_uint32)]


class XrSystemProperties(Structure):
    _fields_ = [("type", c_int32), ("next", c_void_p), ("systemId", c_uint64), ("vendorId", c_uint32),
                ("systemName", c_char * 256),
                ("maxSwapchainImageHeight", c_uint32), ("maxSwapchainImageWidth", c_uint32), ("maxLayerCount", c_uint32),
                ("orientationTracking", c_uint32), ("positionTracking", c_uint32)]


class XrSessionCreateInfo(Structure):
    _fields_ = [("type", c_int32), ("next", c_void_p), ("createFlags", c_uint64), ("systemId", c_uint64)]


class XrSessionBeginInfo(Structure):
    _fields_ = [("type", c_int32), ("next", c_void_p), ("primaryViewConfigurationType", c_int32)]


class XrEventDataBuffer(Structure):
    _fields_ = [("type", c_int32), ("next", c_void_p), ("varying", ctypes.c_uint8 * 4000)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--log")
    parser.add_argument("--loader", default=DEFAULT_LOADER)
    args = parser.parse_args()
    log = open(args.log, "a", encoding="utf-8") if args.log else None

    def out(msg):
        line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + "  " + msg
        print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()

    xr = ctypes.CDLL(args.loader)
    for name, argtypes in {
        "xrCreateInstance": [POINTER(XrInstanceCreateInfo), POINTER(c_void_p)],
        "xrGetSystem": [c_void_p, POINTER(XrSystemGetInfo), POINTER(c_uint64)],
        "xrGetSystemProperties": [c_void_p, c_uint64, POINTER(XrSystemProperties)],
        "xrCreateSession": [c_void_p, POINTER(XrSessionCreateInfo), POINTER(c_void_p)],
        "xrBeginSession": [c_void_p, POINTER(XrSessionBeginInfo)],
        "xrPollEvent": [c_void_p, POINTER(XrEventDataBuffer)],
        "xrDestroySession": [c_void_p],
        "xrDestroyInstance": [c_void_p],
    }.items():
        fn = getattr(xr, name)
        fn.restype = c_int32
        fn.argtypes = argtypes

    exts = (c_char_p * 2)(b"XR_MND_headless", b"XR_EXT_user_presence")
    ci = XrInstanceCreateInfo(type=XR_TYPE_INSTANCE_CREATE_INFO)
    ci.applicationInfo.applicationName = b"presence-probe"
    ci.applicationInfo.engineName = b"ctypes"
    ci.applicationInfo.apiVersion = API_VERSION_1_0_34
    ci.enabledExtensionCount = 2
    ci.enabledExtensionNames = exts
    instance = c_void_p()
    r = xr.xrCreateInstance(byref(ci), byref(instance))
    if r:
        out(f"xrCreateInstance failed XrResult={r}")
        return 1
    session = c_void_p()
    try:
        system_id = c_uint64()
        r = xr.xrGetSystem(instance, byref(XrSystemGetInfo(type=XR_TYPE_SYSTEM_GET_INFO,
                                                          formFactor=XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY)), byref(system_id))
        if r:
            out(f"xrGetSystem failed XrResult={r}")
            return 1
        presence_props = XrSystemUserPresencePropertiesEXT(type=XR_TYPE_SYSTEM_USER_PRESENCE_PROPERTIES_EXT)
        props = XrSystemProperties(type=XR_TYPE_SYSTEM_PROPERTIES, next=ctypes.cast(byref(presence_props), c_void_p))
        xr.xrGetSystemProperties(instance, system_id, byref(props))
        out(f"system: {props.systemName.decode()} supportsUserPresence={presence_props.supportsUserPresence}")

        r = xr.xrCreateSession(instance, byref(XrSessionCreateInfo(type=XR_TYPE_SESSION_CREATE_INFO,
                                                                  systemId=system_id)), byref(session))
        if r:
            out(f"xrCreateSession failed XrResult={r}")
            return 1

        buf = XrEventDataBuffer()
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            buf.type = XR_TYPE_EVENT_DATA_BUFFER
            buf.next = None
            r = xr.xrPollEvent(instance, byref(buf))
            if r == XR_EVENT_UNAVAILABLE:
                time.sleep(0.02)
                continue
            if r:
                out(f"xrPollEvent XrResult={r}")
                time.sleep(0.1)
                continue
            raw = ctypes.string_at(byref(buf), 40)
            if buf.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED:
                state = int.from_bytes(raw[24:28], "little", signed=True)
                out(f"session state -> {STATES[state] if 0 <= state < len(STATES) else state}")
                if state == XR_SESSION_STATE_READY:
                    r = xr.xrBeginSession(session, byref(XrSessionBeginInfo(
                        type=XR_TYPE_SESSION_BEGIN_INFO,
                        primaryViewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO)))
                    out(f"xrBeginSession XrResult={r}")
            elif buf.type == XR_TYPE_EVENT_DATA_USER_PRESENCE_CHANGED_EXT:
                present = int.from_bytes(raw[24:28], "little")
                out(f"user presence event isUserPresent={present} "
                    f"{'(worn)' if present else '(not worn -> Beat Saber 1.45+ pauses)'}")
            else:
                out(f"other event type={buf.type}")
        out("done")
    finally:
        if session:
            xr.xrDestroySession(session)
        xr.xrDestroyInstance(instance)
        if log:
            log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
