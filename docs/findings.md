# Findings

English | [简体中文](findings.zh-CN.md)

Test setup: Valve Index, Windows 11, SteamVR OpenXR runtime 2.18.4, Beat Saber 1.45.3 (BSIPA 4.3.5). Addresses refer to the binaries shipped with those versions.

## 1. Symptom in the game log

`%USERPROFILE%\AppData\LocalLow\Hyperbolic Magnetism\Beat Saber\Player.log`:

```
[2363] AudioTimeSyncController: state=Stopped->Paused songTime=0
```

During song load the OpenXR session stays in `XR_SESSION_STATE_FOCUSED`, so this is not a focus-loss pause (dashboard, etc.).

## 2. Beat Saber: presence is a pause condition (Main.dll)

- `PauseController.GetDefaultPausedState()` returns `IXRSystemState.IsAppFocusCurrentlyLost()` (unless FPFC is enabled).
- It is re-evaluated in `HandleLevelDidStart` (song start) and in `HandlePauseMenuManagerDidFinishResumeAnimation` (end of the resume countdown). If it is true, `Pause()` is called immediately. That explains the endless loop.
- An `XRSystemEventType.HmdUnmounted` event also calls `Pause()`.
- `UnityXRSystemState.IsAppFocusCurrentlyLost()` = `!_hasInputFocus || !_userPresence`.
- `_userPresence` is refreshed every frame in `Update()` → `SetUserPresence(OpenXRUtility.IsUserPresent)`. `SetUserPresence` is the only writer, so it is the patch point used by PresenceFix.

## 3. Unity OpenXR: where the value comes from (UnityOpenXR.dll)

- `OpenXRUtility.IsUserPresent` → P/Invoke `NativeConfig_GetUserPresence`.
- Native side: if `XR_EXT_user_presence` is not enabled (session flag at `+0x8`), it always returns true. Otherwise it returns a cached byte at `+0x1085`.
- Writers of `+0x1085`:
  - the session-state handler (`0x18004aee0`, jump table `0x18004b260`, index = state − 1): `READY` → 1, `STOPPING` → 0;
  - `messagepump_PumpMessageLoop`, on `XR_TYPE_EVENT_DATA_USER_PRESENCE_CHANGED_EXT` (1000470000), copies `isUserPresent` (offset 0x18) via `0x18004cf00`.
- So Unity assumes "present" by default. The value only becomes false if the runtime sends `isUserPresent = false`.

## 4. SteamVR: what it actually sends

[`tools/openxr_presence_probe.py`](../tools/openxr_presence_probe.py), with the headset on:

```
system: SteamVR/OpenXR : lighthouse supportsUserPresence=1
session state -> READY
xrBeginSession XrResult=0
user presence event isUserPresent=0
session state -> SYNCHRONIZED / VISIBLE / FOCUSED
```

No `isUserPresent=1` arrived during the following 60 s. Over the same period the OpenVR HMD activity level ([`tools/openvr_presence_probe.py`](../tools/openvr_presence_probe.py)) stayed at `0 Idle`. In an earlier run with removing/re-wearing/covering, it flipped 11 times in 90 s and spent most of the time in Idle.

## 5. The Index sensor itself

### Signals

| Signal | Source | Worn | Not worn |
|---|---|---|---|
| Raw count | `lighthouse_console` axis stream: `proximity=<count> (<count/65535>) prox_triggered=<0/1>`, ~100 Hz | higher | lower |
| `prox_triggered` | Same line; computed by the HMD firmware | 1 | 0 |
| Button 31 | OpenVR `GetControllerState(0)`, bit `k_EButton_ProximitySensor` | 1 | 0 |
| Activity level | OpenVR `GetTrackedDeviceActivityLevel(0)`; follows the trigger bit with ~0.3–1 s delay | 1 / 2 | 0 (3 = standby) |
| `isUserPresent` | OpenXR `XR_EXT_user_presence`; what Beat Saber reads | 1 | 0 |

`lighthouse_console` only streams while a `sleep` command is running, and the output arrives in ~4 KB chunks through a pipe (about 0.5 s latency). The raw count is not available in the `button` or `event` streams.

### Calibration (one headset)

| Situation | Raw count | `prox_triggered` |
|---|---|---|
| On the desk, nothing in front | 143–178 | 0 |
| Worn normally | ~265–440, mostly 270–330 | 0 |
| Worn, pressed closer / adjusted | 451–729 for a few seconds | 1 |
| Finger on the sensor | 3400–13342 | 1 |

- Threshold ≈ 449–451: 451 and up reads 1, 448 and below reads 0. Hysteresis is only 2–3 counts.
- A later session read 415–455 while worn, so the state flickered around the threshold.
- The sensor works (a finger gives 13000+). The problem is that reflection from the face only adds about 100–250 over the idle floor, which is not enough to reach the fixed threshold. Possible causes are face distance or angle (gasket thickness, fit, nose/brow shape) or reduced IR LED output with age. This has not been isolated.
- Hair matters: when the fringe reaches into the headset the count drops, and it recovers after the hair is moved out (a later reading was 455–460, firmware "worn"). This fits how the sensor works. It measures reflected IR intensity, not geometric distance. Dark hair absorbs near-IR (eumelanin) and scatters it, so it replaces the more reflective skin of the brow. It also shows the sensor looks at the area between the eyebrows and the forehead. Reflectance varies by person (hair and skin color, eyebrows, fringe), so the same fixed firmware threshold leaves very different margins for different faces. This was observed qualitatively, not quantified.

### Can the threshold be changed?

- The device config read back with `lighthouse_console` (read-only `downloadconfig`) has no proximity-related fields.
- `driver_lighthouse.dll` only parses `proximity.enable` from the device JSON (code from `0x180056efe`, stored at `+0xbe0`). For the Index it is forced to 1 (`0x1800248ea`; the vrserver log says `Overriding proximityEnable to 1 (Valve Index HMD)`). There is no threshold parameter.
- The packets seen by `lighthouse_console` already contain both the raw count and `prox_triggered`, so the decision is made in the HMD firmware.

## 6. Side effects of the sensor state

- Tracking: lighthouse optical tracking and the IMU do not depend on the proximity sensor; the axis/IMU stream ran at ~100 Hz throughout.
- Performance: SteamVR does not lower rendering or tracking quality when "not worn".
- Behavior: OpenXR apps receive `isUserPresent`, OpenVR apps can read `/user/head/proximity`, and SteamVR's idle/standby and display-off timers depend on the activity level.

## 7. Tool notes

- OpenVR interface indices: `openvr_presence_probe.py` uses the `IVRSystem_022` vtable. `proximity_overlay.py` uses the `IVRSystem_019` / `IVROverlay_019` FnTables, with indices taken from SteamVR's own `SteamVR.dll`. Both self-check that device 0 is an HMD (and that button 31 is `k_EButton_ProximitySensor`) before reading anything.
- `IVROverlay::SetOverlayRaw` started returning `EVROverlayError 23 (RequestFailed)` after a few hundred uploads (~3 minutes at 2–3 uploads/s). The overlay therefore renders to PNG and uses `SetOverlayFromFile`, alternating two files. A stress test of 1500 uploads had no failures.
- `lighthouse_console` does not exit during `sleep` even after stdin is closed. Both the overlay and the plugin put it in a `KILL_ON_JOB_CLOSE` job object so it never lingers.
- Two `lighthouse_console` instances can read the stream at the same time.

## Open questions

- Comparing 1.44.1 and 1.45.x `Main.dll` to confirm presence was not part of the pause condition before.
- Whether the low reading is sensor aging or fit, e.g. by measuring other Index units with the same tool.
