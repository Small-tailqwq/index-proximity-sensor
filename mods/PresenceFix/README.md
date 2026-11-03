# PresenceFix

BSIPA + Harmony plugin for Beat Saber 1.45.x. It replaces the OpenXR "user present" value that the game uses as a pause condition.
Beat Saber 1.45.x 的 BSIPA + Harmony 插件，替换游戏用作暂停条件的 OpenXR “用户在场”值。

## Build / 编译

Needs the .NET SDK (any recent version; only its bundled Roslyn compiler is used) and a BSIPA-modded game. No NuGet packages are downloaded.
需要 .NET SDK（任意较新版本，只用到自带的 Roslyn 编译器）和已安装 BSIPA 的游戏，不会下载 NuGet 包。

```powershell
pwsh -File build.ps1            # -> bin/PresenceFix.dll
pwsh -File build.ps1 -Install   # also copy to <game>/Plugins / 同时复制到游戏 Plugins 目录
```

Use `-GameDir` if the game is not in the default Steam library. / 游戏不在默认 Steam 库时加 `-GameDir`。

## Config / 配置

The first launch creates `<game>/UserData/PresenceFix.ini`. Changes are picked up about 2 s after saving.
首次启动生成 `<game>/UserData/PresenceFix.ini`，保存后约 2 秒生效。

| Key | Default | Meaning / 含义 |
|---|---|---|
| `Mode` | `AlwaysWorn` | `AlwaysWorn`: always present / 始终在场；`Threshold`: decide from the raw proximity count / 按原始接近计数判定 |
| `OnThreshold` | 260 | Raw count ≥ this → worn / 计数 ≥ 此值判为佩戴 |
| `OffThreshold` | 220 | Raw count ≤ this for `OffDelaySeconds` → not worn / 计数 ≤ 此值并持续 `OffDelaySeconds` 秒判为离开 |
| `OffDelaySeconds` | 1.0 | Leave debounce / 离开防抖 |
| `Serial` | empty / 空 | HMD serial (`LHR-xxxxxxxx`); empty = first LHR- receiver / 头显序列号，留空自动选择 |

`Threshold` mode starts SteamVR's `lighthouse_console` in the background (read-only commands only) and treats the headset as worn whenever no fresh data is available. Pick thresholds by watching [`tools/proximity_overlay.py`](../../tools/proximity_overlay.py) while you wear and remove the headset; the overlay reads the same ini and shows the software decision.
`Threshold` 模式会在后台启动 SteamVR 的 `lighthouse_console`（只发只读命令）；拿不到新数据时按佩戴处理。阈值可以一边运行 [`tools/proximity_overlay.py`](../../tools/proximity_overlay.py) 一边戴上、摘下来确定，覆盖层会读同一个 ini 并显示软件判定结果。

## Trade-offs / 取舍

- `AlwaysWorn`: the game no longer pauses when you take the headset off. Focus-loss pauses (SteamVR dashboard) still work.
  `AlwaysWorn`：摘下头显时游戏不再自动暂停；SteamVR 仪表盘等失焦暂停照常生效。
- Only Beat Saber is affected; SteamVR and other apps still see the firmware decision.
  只影响 Beat Saber；SteamVR 和其他应用看到的仍是固件判定。
- After a game update BSIPA moves plugins to `Old <version> Plugins`; move it back or rebuild.
  游戏更新后 BSIPA 会把插件移到 `Old <版本> Plugins`，需要移回或重新编译。

Uninstall: delete `<game>/Plugins/PresenceFix.dll`. / 卸载：删除 `<game>/Plugins/PresenceFix.dll`。
