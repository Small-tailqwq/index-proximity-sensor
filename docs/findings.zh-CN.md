# 详细发现

[English](findings.md) | 简体中文

测试环境：Valve Index、Windows 11、SteamVR OpenXR 运行时 2.18.4、Beat Saber 1.45.3（BSIPA 4.3.5）。文中地址对应这些版本自带的二进制。

## 1. 游戏日志里的现象

`%USERPROFILE%\AppData\LocalLow\Hyperbolic Magnetism\Beat Saber\Player.log`：

```
[2363] AudioTimeSyncController: state=Stopped->Paused songTime=0
```

加载歌曲期间 OpenXR 会话一直处于 `XR_SESSION_STATE_FOCUSED`，排除了 SteamVR 仪表盘等失焦暂停。

## 2. Beat Saber：在场状态是暂停条件（Main.dll）

- `PauseController.GetDefaultPausedState()` 返回 `IXRSystemState.IsAppFocusCurrentlyLost()`（开启 FPFC 时除外）。
- 这个值在 `HandleLevelDidStart`（开局）和 `HandlePauseMenuManagerDidFinishResumeAnimation`（继续倒计时结束）两处重新读取，为 true 就立即 `Pause()`，这就是无限循环的原因。
- 收到 `XRSystemEventType.HmdUnmounted` 事件也会调用 `Pause()`。
- `UnityXRSystemState.IsAppFocusCurrentlyLost()` = `!_hasInputFocus || !_userPresence`。
- `_userPresence` 在每帧 `Update()` 里通过 `SetUserPresence(OpenXRUtility.IsUserPresent)` 刷新。`SetUserPresence` 是唯一写入点，PresenceFix 就补丁在这里。

### 与 1.44.1 对比（构建 `1.44.1_20239`，Steam 测试版分支 `1.44.1_legacy`）

1.44.1 读取的是同一个值：`UnityXRHelper.UpdateUserPresence()` 每帧调用 `OpenXRUtility.IsUserPresent`。原生 `UnityOpenXR.dll`（Unity OpenXR 1.14.3）的逻辑也相同，只是缓存字节在 `+0x106d`，不是 `+0x1085`。区别在于游戏怎么用这个值：

| | 1.44.1 | 1.45.3 |
|---|---|---|
| `PauseController.GetDefaultPausedState()`（开局、继续倒计时结束时） | `hasInputFocus ? !hasVrFocus : true`，**不含在场状态** | `IsAppFocusCurrentlyLost()` = `!hasInputFocus \|\| !userPresence`，**含在场状态** |
| 在场状态的作用 | 只在“佩戴 → 未佩戴”**变化**时通过 `hmdUnmountedEvent` 调用一次 `Pause()` | 变化时同样暂停，**此外**还作为持续条件，阻止开局和每一次恢复 |
| 一直是“未佩戴” | 没有变化，不暂停 | 无限暂停循环 |
| 读数来回跳 | 偶尔暂停一次，可以继续 | 读到“未佩戴”期间一直暂停 |

两个版本里 `_userPresence` 的初值都是 false，所以从一开始就是 false 的话不会产生变化事件。这就是为什么 1.44.1 虽然也有佩戴检测，降级仍然有效。

已在 1.44.1 上实机确认（同一台头显，没有装插件：BSIPA 已把 PresenceFix 挪进 `Old 1.45.3 Plugins`）：

| 证据 | 1.44.1 | 1.45.3 |
|---|---|---|
| SteamVR `xrclient` 日志里请求的扩展 | `XR_EXT_user_presence, XR_KHR_D3D11_enable, …` | `XR_EXT_local_floor, XR_EXT_user_presence, …` |
| 进歌时的 `Player.log` | `AudioTimeSyncController: state=Stopped->Playing songTime=0` | `state=Stopped->Paused songTime=0` |
| 暂停后继续 | `Paused->Playing` 正常 | 每次倒计时结束都重新暂停 |
| 打歌时把头显拿远 | 继续播放 | — |

也就是说，1.44.1 从 SteamVR 收到的同样是“未佩戴”，只是不拿它来阻止游戏进行。

## 3. Unity OpenXR：值从哪来（UnityOpenXR.dll）

- `OpenXRUtility.IsUserPresent` → P/Invoke `NativeConfig_GetUserPresence`。
- 原生层：未启用 `XR_EXT_user_presence`（会话标志 `+0x8`）时恒返回 true；启用时返回缓存字节 `+0x1085`。
- `+0x1085` 的写入点：
  - 会话状态处理函数（`0x18004aee0`，跳转表 `0x18004b260`，索引 = state − 1）：`READY` → 1，`STOPPING` → 0；
  - `messagepump_PumpMessageLoop` 收到 `XR_TYPE_EVENT_DATA_USER_PRESENCE_CHANGED_EXT`（1000470000）时，经 `0x18004cf00` 写入 `isUserPresent`（偏移 0x18）。
- 也就是说 Unity 默认认为用户在场，只有运行时发来 `isUserPresent = false` 才会变成不在场。

## 4. SteamVR 实际发了什么

戴着头显运行 [`tools/openxr_presence_probe.py`](../tools/openxr_presence_probe.py)：

```
system: SteamVR/OpenXR : lighthouse supportsUserPresence=1
session state -> READY
xrBeginSession XrResult=0
user presence event isUserPresent=0
session state -> SYNCHRONIZED / VISIBLE / FOCUSED
```

之后 60 秒内没有收到 `isUserPresent=1`。同期 OpenVR 头显活动等级（[`tools/openvr_presence_probe.py`](../tools/openvr_presence_probe.py)）一直是 `0 Idle`。更早一轮测试中做了摘、戴、遮挡，90 秒内跳变了 11 次，大部分时间仍是 Idle。

## 5. Index 传感器本身

### 信号

| 信号 | 来源 | 佩戴 | 离开 |
|---|---|---|---|
| 原始计数 | `lighthouse_console` axis 数据流：`proximity=<计数> (<计数/65535>) prox_triggered=<0/1>`，约 100 Hz | 较高 | 较低 |
| `prox_triggered` | 同一行，由头显固件计算 | 1 | 0 |
| 按钮 31 | OpenVR `GetControllerState(0)` 的 `k_EButton_ProximitySensor` 位 | 1 | 0 |
| 活动等级 | OpenVR `GetTrackedDeviceActivityLevel(0)`，跟随触发位，延迟约 0.3–1 秒 | 1 / 2 | 0（3 为待机） |
| `isUserPresent` | OpenXR `XR_EXT_user_presence`，Beat Saber 读的就是它 | 1 | 0 |

`lighthouse_console` 只有在执行 `sleep` 命令期间才会持续输出，经管道按约 4 KB 分块到达，延迟约 0.5 秒。`button` 和 `event` 数据流里没有原始计数。

### 标定（单台头显）

| 场景 | 原始计数 | `prox_triggered` |
|---|---|---|
| 放在桌上，前方无遮挡 | 143–178 | 0 |
| 正常佩戴 | 约 265–440，多数 270–330 | 0 |
| 佩戴时压紧或调整位置 | 451–729，持续数秒 | 1 |
| 手指贴住传感器 | 3400–13342 | 1 |

- 阈值约 449–451：451 及以上为 1，448 及以下为 0，迟滞只有 2–3 个计数。
- 后来另一次佩戴读数在 415–455 之间，判定在阈值附近来回跳。
- 传感器本身能工作（手指可达 13000 以上）。问题在于脸部反射只比无遮挡底噪高约 100–250，够不着固定阈值。可能原因有脸与传感器的距离或角度（面垫厚度、佩戴方式、鼻梁和眉骨形状），或红外 LED 随老化变弱。具体是哪一种还没有确认。
- 头发有影响：额前头发伸进头显时计数下降，拨出来后回升（之后一次读数为 455–460，固件判定为佩戴）。这和传感器原理一致：它测的是红外反射强度，不是几何距离。深色头发中的真黑素会吸收近红外，纤维结构还会散射光线，挡在传感器前面就相当于用反射更弱的目标替换了眉心的皮肤。这也说明传感器看的是眉心到额头一带。反射率因人而异（发色、肤色、眉毛、刘海），同一个固件阈值在不同人脸上的余量差别很大。这一条只做了定性观察，没有量化。

### 阈值能不能改？

- 用 `lighthouse_console` 只读导出（`downloadconfig`）的设备配置里没有接近传感器相关字段。
- `driver_lighthouse.dll` 解析设备 JSON 时只读 `proximity.enable`（代码起于 `0x180056efe`，写入 `+0xbe0`）。Index 上它被强制设为 1（`0x1800248ea`，vrserver 日志中为 `Overriding proximityEnable to 1 (Valve Index HMD)`），没有阈值参数。
- `lighthouse_console` 看到的数据包里同时带有原始计数和 `prox_triggered`，说明判定在头显固件里完成。

## 6. 传感器状态的影响范围

- 追踪：lighthouse 光学追踪和 IMU 不依赖接近传感器，测试全程 axis/IMU 数据流稳定在约 100 Hz。
- 性能：SteamVR 不会因为“未佩戴”降低渲染或追踪质量。
- 行为：OpenXR 应用会收到 `isUserPresent`，OpenVR 应用可以读 `/user/head/proximity`，SteamVR 的闲置待机和熄屏计时也取决于活动等级。

## 7. 工具实现备注

- OpenVR 接口序号：`openvr_presence_probe.py` 用 `IVRSystem_022` 的 vtable；`proximity_overlay.py` 用 `IVRSystem_019` / `IVROverlay_019` 的 FnTable，序号取自 SteamVR 自带的 `SteamVR.dll`。两者在读取前都会自检：设备 0 是否为 HMD，按钮 31 是否为 `k_EButton_ProximitySensor`。
- `IVROverlay::SetOverlayRaw` 上传几百次后（每秒 2–3 次，约 3 分钟）开始持续返回 `EVROverlayError 23（RequestFailed）`。所以覆盖层改为渲染 PNG，再用 `SetOverlayFromFile` 加载，两个文件交替使用。压测上传 1500 次，0 次失败。
- `lighthouse_console` 在 `sleep` 期间即使 stdin 关闭也不会退出。覆盖层和插件都把它放进 `KILL_ON_JOB_CLOSE` 作业对象，保证不会残留。
- 两个 `lighthouse_console` 实例可以同时读取数据流。

## 待解决

- 判断读数偏低是传感器老化还是佩戴问题，例如用同一工具测量其他 Index。
