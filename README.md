# WirelessDebugging — 无线调试器

基于 WiFi 的 MCU 无线调试探针(SWD/JTAG),目标是做一款 CMSIS-DAP 兼容的无线调试器,摆脱 PC 与目标板之间的 USB 线缆,可替代/补充有线调试器(DAPLink、J-Link 等)。

## 目录结构

| 目录 | 内容 |
| --- | --- |
| `00_preparation/` | 前期准备:需求分析、现有方案调研、方案对比与选型、产品化方案 |
| `01_hardware/` | 硬件:原理图、PCB、BOM、**开发板资料(44 脚定义/引脚对齐)** |
| `02_software/` | 软件:PC 端(驱动、IDE 插件、上位机工具) |
| `03_firmware/` | 固件:设备端固件(ESP-IDF) |
| `04_user/` | 用户手册:配网/固定优先/调试接入/排障表(给同事和买家看) |
| `tools/` | 辅助脚本(非交付物):`capture_boot.py` = 复位并抓取完整启动日志 |

## 当前状态

- [x] 目录结构搭建(文件夹统一英文命名,避免兼容问题)
- [x] 现有方案调研 → [00_preparation/现有方案调研.md](00_preparation/现有方案调研.md)
- [x] 方案选型:路线 A,windowsair 生态二开(2026-10-25)→ [00_preparation/方案对比与选型.md](00_preparation/方案对比与选型.md)
- [x] 需求初步确认 → [00_preparation/需求分析.md](00_preparation/需求分析.md)
- [x] 方案 A 准备工作(2026-10-25)→ [00_preparation/原型验证计划.md](00_preparation/原型验证计划.md),含[烧录指南](03_firmware/固件烧录指南.md)、[接线参考](01_hardware/接线参考.md)、[PC 端环境](02_software/PC端环境搭建.md)
- [x] 原型验证:**核心能力全部打通**。入网(10-25)→ OpenOCD 无线 SWD(10-25)→ Keil+elaphureLink 无线烧录(10-25)→ usbip×ocdup 联动全链路(10-26,506KB 无线烧录+校验+复位一次通过)→ Keil 单步 4/5 达标(10-26)→ 30 分钟挂机六拍全绿零断连(10-27)→ **无线串口桥 v3 上线**(10-27,TCP:1234,验收 5/5)。剩余:A(单步评分)✅、B(挂机)✅ → **C(距离测试)、E(电流三态)待做** → F(S3 达标结论)
- [ ] **产品化阶段(2026-10-27 定位变更)**:目标从"自用"改为**给同事用 / 开源发布 / 小批量售卖**。五条约束:①日常保持现有 STA 模式 ②模式只能通过命令切换(不自动降级)③OTA 下一步 ④配网可视化+自动扫描+能判断 ⑤**配网走 PC 工具经 USB 与板子通讯**(不做板载网页)→ 计划见 [00_preparation/产品化方案-配网与双模网络.md](00_preparation/产品化方案-配网与双模网络.md)
- [x] **P0 工程基线完成(2026-10-28)**:
  - 分支 `feature/productization`(基于 v0.4.0),两个提交 `a09d72c`(v3 基线)+ `fdb730e`(构建修复)
  - **凭据外置**:真实 WiFi 密码移出仓库 → `main/wifi_credentials.h`(已 gitignore),仓库只留 `.example`;开源发布不含真实密码
  - **`build.bat`**:一键编译,绕开 Git Bash 下 idf.py 的五个坑
  - **版本横幅**:启动打印 `=== dap-0.5.0 | mac=AA:BB:CC:DD:EE:FC | idf=v4.4.2 ===",实机验证通过
  - **修掉一个静默构建缺陷**:`wifi_credentials.h` 由 `__has_include` 引入,编译器不记录"文件不存在"为依赖 → 增删该文件后增量编译不重编,表现为"改了配置却没生效"。已用 CMake `OBJECT_DEPENDS` + `build.bat` 状态检测双层修复,两方向均验证
- [x] **硬件参数实测(2026-10-28,`esptool flash_id`)**:ESP32-S3 QFN56 rev v0.2 / **Flash 16MB** / PSRAM 8MB(未启用)/ QIO 3.3V / 40MHz 晶振 / MAC `aa:bb:cc:dd:ee:fc`。**16MB 意味着 OTA 完全可行**,原"2MB 太紧"的顾虑来自 sdkconfig 保守值,已排除
- [x] **开发板资料确认(2026-10-28)**:板型 = **YD-ESP32-S3(ESP32-S3-WROOM-1-N16R8)**,44 脚双 USB-C,USB-UART 为 CH343P。完整 44 脚定义、与本项目的引脚对齐表见 [01_hardware/开发板资料.md](01_hardware/开发板资料.md)。**重要修正**:板载 RGB LED 是 **WS2812 接 GPIO48**(不是上游代码写的 GPIO4),必须用 RMT 驱动 → 新增 P2.5 任务;同时确认串口桥的 GPIO17/18 选择正确、CH343 在 PCB 上可省掉
- [x] **产品化第一阶段收口(2026-11-01 ~ 11-02)**:配网(P0-P4/P2.5)+ 交付形态(A1A2/B1B2/C1C2)+ GUI 方案三侧栏重构 + 体检 Top10 全落地;固件 **0.8.1**(互斥锁 + 栈加固)真机回归 7/7;工具四模块拆分(`dap_proto/dap_mock/dap_gui/dap_selftest`),selftest **277 项**含 mock↔固件交叉核对与变异验证;净版开源收口(凭据外移、三仓库统一 **Apache-2.0**),CI 绿;板子已配网上线(192.168.0.125)。净版 0.8.1 bin 零凭据
- [x] **第二阶段:离线烧录(2026-11-04,代码全量落地,真机验收待板子插回)** → 方案与验收表见 [00_preparation/产品化方案-离线烧录.md](00_preparation/产品化方案-离线烧录.md):
  - **固件 0.9.0**:16MB 自定义分区表(nvs 保位升级,WiFi 配置不丢;ota_0/1 预留)、LittleFS 镜像存储(8 槽位 + NVS 索引,CRC 三方核对)、`@FLIST/@FSTAT/@FDEL/@FSEL/@FNAME/@FPUT/@FLASH/@FPROG`、TCP:1235 分块上传、**板上 SWD 烧录引擎**(直调 DAP 底层,PY32 自写 RAM loader 定槽位交叉编译,flash_algo 家族注册表)、与在线调试线锁互斥、GPIO0 长按 ≥2s 脱机触发、LED 三态反馈
  - **工具**:「烧录文件」页(上传 WiFi 主/USB 兜底、切当前、一键烧录 + 进度轮询)、selftest 277 项(变异三杀验证)、交付包同步重打
