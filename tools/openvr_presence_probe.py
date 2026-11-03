"""Read-only probe: poll the HMD activity level as an OpenVR background app.

On a Valve Index the activity level follows the proximity sensor (worn / not worn).
Usage: python -I openvr_presence_probe.py [--seconds N] [--log PATH]
"""
import argparse
import ctypes
import sys
import time
from datetime import datetime

OPENVR_DLL = r"C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\openvr_api.dll"
VRApplication_Background = 3
IVRSYSTEM_VERSION = b"IVRSystem_022"

# IVRSystem_022 vtable indices (declaration order in openvr.h)
VT_GET_ACTIVITY_LEVEL = 15
VT_GET_DEVICE_CLASS = 19
VT_IS_CONNECTED = 20
VT_GET_BOOL_PROP = 21
VT_GET_STRING_PROP = 27

Prop_ModelNumber_String = 1001
Prop_ManufacturerName_String = 1005
Prop_ContainsProximitySensor_Bool = 1025

ACTIVITY = {
    -1: "Unknown",
    0: "Idle (not worn / still)",
    1: "UserInteraction (worn)",
    2: "UserInteraction_Timeout (worn, no movement for a while)",
    3: "Standby",
    4: "Idle_Timeout",
}


def vfunc(obj, index, restype, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = proto(vtable[index])
    return lambda *args: fn(obj, *args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--log")
    args = parser.parse_args()
    log = open(args.log, "a", encoding="utf-8") if args.log else None

    def out(msg):
        line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + "  " + msg
        print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()

    vr = ctypes.CDLL(OPENVR_DLL)
    vr.VR_InitInternal2.restype = ctypes.c_uint32
    vr.VR_InitInternal2.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_char_p]
    vr.VR_GetGenericInterface.restype = ctypes.c_void_p
    vr.VR_GetGenericInterface.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]

    err = ctypes.c_int(0)
    vr.VR_InitInternal2(ctypes.byref(err), VRApplication_Background, None)
    if err.value:
        out(f"VR_InitInternal2 failed err={err.value} (is SteamVR running?)")
        return 1
    try:
        system = vr.VR_GetGenericInterface(IVRSYSTEM_VERSION, ctypes.byref(err))
        if not system or err.value:
            out(f"Failed to get {IVRSYSTEM_VERSION.decode()} err={err.value}")
            return 1

        device_class = vfunc(system, VT_GET_DEVICE_CLASS, ctypes.c_int, ctypes.c_uint32)
        is_connected = vfunc(system, VT_IS_CONNECTED, ctypes.c_bool, ctypes.c_uint32)
        activity = vfunc(system, VT_GET_ACTIVITY_LEVEL, ctypes.c_int, ctypes.c_uint32)
        get_bool = vfunc(system, VT_GET_BOOL_PROP, ctypes.c_bool,
                         ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
        get_str = vfunc(system, VT_GET_STRING_PROP, ctypes.c_uint32, ctypes.c_uint32,
                        ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_int))

        def prop_str(prop):
            buf = ctypes.create_string_buffer(256)
            perr = ctypes.c_int(0)
            get_str(0, prop, buf, 256, ctypes.byref(perr))
            return buf.value.decode(errors="replace") if not perr.value else f"<err {perr.value}>"

        cls = device_class(0)
        if cls != 1:
            out(f"Device 0 is not an HMD (class={cls}); vtable indices may not match, stopping")
            return 1
        perr = ctypes.c_int(0)
        has_prox = get_bool(0, Prop_ContainsProximitySensor_Bool, ctypes.byref(perr))
        out(f"HMD: {prop_str(Prop_ManufacturerName_String)} {prop_str(Prop_ModelNumber_String)}"
            f" connected={is_connected(0)} proximity_sensor={has_prox if not perr.value else f'<err {perr.value}>'}")

        last = None
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            level = activity(0)
            if level != last:
                out(f"activity -> {level} {ACTIVITY.get(level, '?')}")
                last = level
            time.sleep(0.05)
        out("done")
    finally:
        vr.VR_ShutdownInternal()
        if log:
            log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
