# tools —— PC 端工具

## dap_config.py —— 配网工具(给同事用的那个)

无线调试器的图形化配网界面。插 USB、选 WiFi、填密码、看结果,不用记命令。

### 怎么用

双击 `run_config.bat`,或者:

```bat
python dap_config.py
```

会自动枚举串口、逐个发 `@PING`、连上第一个应答的设备。

### 界面里有什么

| 页 | 内容 |
| --- | --- |
| **配网** | 扫描 WiFi(SSID / 信号条 / dBm / 加密 / 信道,点表头排序)、填密码、连接。连上后直接给出三条调试路径的具体命令 |
| **已保存网络** | 板子里存的凭据列表(密码打码)、删除、恢复出厂设置 |
| **调试接入** | Keil / OpenOCD / usbip 三条路各自的启动开关,地址统一可配(默认 mDNS 名 `dap.local`) |
| **日志** | 板端日志与协议帧分开显示,可手动发命令 |

顶部还有模式切换(STA / AP)、重启板子、固件版本。

### 调试接入页(dap_backends.py)

三条路各自独立开关,**不要同时开** —— usbip 与 Keil/OpenOCD 会抢同一个 DAP 连接。

| 后端 | 形态 | 说明 |
| --- | --- | --- |
| Keil µVision | `elaphureLink.Wpf.exe` | 无命令行模式,只能起进程。启动时自动把地址写进它的 `.config` |
| OpenOCD / VSCode | 完全无头 | `CREATE_NO_WINDOW` 起进程,日志直接进日志页,用户看不到黑框 |
| USB 桥接 | 一次动作 | 需要管理员权限,点「挂载」弹一次 UAC。**AT32 烧录的前提** |
| AT32 烧录 | 完全无头 | 用 atopenocd(雅特力 fork),必须先完成 USB 桥接 |

#### 两条 OpenOCD 不能互相替代

flash 驱动是**编译进 openocd.exe 的 C 代码**,不是 cfg 脚本,所以:

| 构建 | AT32 flash 驱动 | elaphurelink | 能烧 AT32 |
| --- | --- | --- | --- |
| `openocd-elaphurelink` | ❌ | ✅ | ❌ |
| `atopenocd`(雅特力) | ✅ | ❌ | ✅ 经 usbip |

拿 elaphurelink 版加载 AT32 的 cfg 会直接报 `flash driver 'at32f403axx' not found` ——
cfg 能借用,驱动借不了。AT32 烧录的完整顺序是:装 usbip 驱动 → 挂载 → 启动 AT32。

#### 挂载前会先查网段

`usbip` 挂载前会先探 3240/1234 端口。不通就直接报"PC 和设备不在同一网段",
而不是让 usbip 自己超时。**用 TCP 不用 ping**:Windows 默认拦 ICMP,ping 不通不代表端口不通。

**一个容易踩的坑**:OpenOCD 只有在真正和目标芯片建立 SWD 会话后才打开 gdb 端口
3333。连不上目标时它照样"运行中",只开 6666/4444。所以界面区分三态:

- `已连上目标 · gdb :3333`(绿)—— 可以开始调试
- `运行中,但还没连上目标芯片(查 SWD 接线)`(黄)—— 进程活着,没握手
- `已就绪,未启动`(灰)

黄色状态持续约 12 秒后会弹出排查清单(SWD 四线 / 目标是否上电 / 芯片选对没 / 复位脚)。

**第三方工具在哪**:`tools_root()` 依次找 `<程序目录>/tools` 和开发机工具集目录,
判据是那里真的有 `openocd.exe`(不是"目录存在" —— 开发仓库自己的 `tools/` 也存在但
只有源码)。交付包把三个工具目录放在 exe 旁边即可。

### 命令行选项

```bat
python dap_config.py --port COM18     指定串口(跳过自动探测)
python dap_config.py --mock           连假设备,没有板子也能试界面
python dap_config.py --list           只枚举+探测串口,不开界面
python dap_config.py --raw "@SCAN"    直接发一条命令看响应
python dap_config.py --selftest       无硬件自测(121 项)
```

`--mock` 和 `--raw` 可以组合:`python dap_config.py --mock --raw "@SCAN"`。

### 依赖

Python 3.8+ 的 tkinter 和 pyserial。都是现成的,不需要额外装东西:

```bat
python -c "import tkinter, serial; print(serial.VERSION)"
```

### 关于错误码

固件把 IDF 的 WiFi 断连原因码原样透传,工具负责翻成人话:

| 码 | 含义 | 工具的说法 |
| --- | --- | --- |
| 15 | `4WAY_HANDSHAKE_TIMEOUT` | **密码可能不对** |
| 201 | `NO_AP_FOUND` | 找不到这个 WiFi(SSID 错 / 只有 5GHz / 太远) |
| 202 | `AUTH_FAIL` | 认证被拒(密码错或加密方式不兼容) |
| 204 | `HANDSHAKE_TIMEOUT` | 握手超时(密码 / WPA3 协商) |
| 409 | 需要在 STA 模式 | 先切回 STA,AP 模式下扫不了 |
| 423 | 设备忙 | 上一条命令还在跑 |

> **15 和 202 都要当"密码不对"看。** WPA2 密码错误时 IDF 报的是 15,不是 202 ——
> 这条是实测出来的,纸面上看不出来。

### 一个已知的工具侧设计

固件回显 SSID 时**不做**百分号编码(见 `dap_cmd.c` 的 `"ssid=%s"`),所以 SSID 里
带空格、`&`、`=` 会把 `ssid=...&rssi=...` 这类字段撕开。工具侧没有要求固件改,
而是用锚定后缀的正则(`ssid=(.*)&rssi=(-?\d+)&auth=...`)把 SSID 完整还原。

发往板子方向的参数仍然严格用 `quote(safe="")` 编码,保证不会产生裸 `&` 或裸 `+`。

---

## capture_boot.py —— 抓启动横幅

启动横幅在 `app_main()` 里打印,上电后约 200ms 就过去了,打开串口再看必然错过。
这个脚本先占住串口、脉冲 RTS 让芯片复位、再读 5 秒,并剥掉 ANSI 颜色码。

```bat
python capture_boot.py
```

端口和时长在文件头的 `PORT` / `DURATION` 常量里改。

---

## build_package.py —— 一键打交付包

把 GUI(exe)、第三方工具、固件、文档组装成一个可直接发给同事的目录:

```bat
pip install -r requirements.txt      # 首次:装 pyserial / pyinstaller / pillow
python build_package.py              # 产物在 tools/dist/无线调试器/
python build_package.py --skip-exe   # 只重组工具目录,不重跑 PyInstaller
```

产物结构:

```
无线调试器/
├─ 无线调试器.exe            PyInstaller --onedir(--console 保留,排错要看日志)
├─ tools/
│  ├─ elaphureLink/                     BSD-2(Keil 插件)
│  ├─ openocd-elaphurelink/v0.4.0/      GPL-2(独立进程)
│  ├─ usbip-win/                        GPL-3.0(独立进程)
│  │  ├─ install_driver.bat             驱动安装(界面会让用户来这个目录点)
│  │  ├─ uninstall_driver.bat
│  │  └─ v0.3.6-dev/                    usbip.exe 本体
│  └─ atopenocd/                        GPL-2(雅特力 fork,349 个 target)
├─ firmware/                  固件 bin + 烧录指南
├─ 快速上手.md
└─ 许可与开源声明.txt
```

`smoke_check()` 会校验上述结构,包括 `install_driver.bat` 和每个 GPL 目录下的
`COPYING` 全文(并核对 GPL 版本号)。许可证全文源文件在 `tools/licenses/`。

> `flash_download_tool` 是乐鑫的专有软件,绝不入包。

## _shot.py —— 截图辅助(开发期)

```bat
python _shot.py
```

依赖 `pillow`。

## requirements.txt

| 依赖 | 谁需要 | 说明 |
| --- | --- | --- |
| pyserial>=3.4 | 主程序 | 串口通信;`--selftest` 不需要 |
| pyinstaller>=5.0 | build_package.py | 打 exe |
| pillow>=9.0 | _shot.py | 截图 |
