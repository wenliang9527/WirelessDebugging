# 02_软件

PC 端相关:驱动、IDE 插件、上位机/配置工具。

## 规划(选型确认后细化)

- **连接层**(视路线而定):
  - elaphureLink Keil 插件 / openocd-elaphurelink(免驱动直连)
  - usbip-win(USBIP 兜底模式)
- **配置工具**(自研方向):设备配网、固件 OTA、目标电压/串口配置的 Web 页面或小工具(可参考 elaphure-dap.js 做网页端)
- **使用文档**:各 IDE(Keil / OpenOCD+VSCode / pyOCD)的接入说明

## 待定项

- [ ] 是否需要自研配置/配网上位机
