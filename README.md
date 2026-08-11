# OmniPicker CAN / RS485 GUI

这是一个基于 Python + PyQt5 的桌面工具，用于通过 CAN、CAN-FD 或 RS485 控制智元 OmniPicker 夹爪。

实现依据来自智元 OmniPicker 产品手册中的 RS485 自定义协议与 Modbus RTU 章节。

Modbus RTU 模式下，核心寄存器如下：

- 10: 目标位置，低字节有效，0 表示闭合，255 表示完全张开
- 11: 目标速度，低字节有效
- 12: 目标力矩，低字节有效
- 13: 目标加速度，低字节有效
- 14: 目标减速度，低字节有效
- 15: 运动触发标志，写入非零值后触发运动
- 20: 当前错误码
- 21: 当前状态
- 22: 当前位置
- 23: 当前速度
- 24: 当前扭矩

## 使用前提

1. 确认夹爪固件版本不低于 3.3.5。
2. 确认硬件拨码已切到 RS485 模式。
3. 通过官方 REF-CLI 将以下参数配置正确并保存后重启设备：
   - rs485_mode = 1
   - modbus_mode = True
   - uart_baudrate 与本工具中配置一致
   - uart_stopbits 建议为 1 位停止位
4. Modbus 从站地址复用 can_node_id，本工具中的从站地址需与设备参数一致。
5. 若使用自定义协议兼容模式：
   - 3.3.5 之前固件默认使用 485 自定义协议
   - 3.3.5 及之后固件需要将 modbus_mode = False

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 运行

```bash
python3 main.py
```

## 功能

- 自动枚举本机串口
- 支持 Modbus RTU 与手册中的 RS485 自定义协议两种模式
- 支持 Linux SocketCAN 经典 CAN 与 CAN-FD
- 配置波特率、停止位、超时和 Modbus 从站地址
- 写入 10-15 寄存器触发夹爪运动
- 周期读取 20-24 状态寄存器
- 提供全开、全闭、手动下发参数三种控制方式
- 提供常用参数扫描与 1-247 全地址扫描

## 说明

当前版本默认使用 8N1 串口参数，停止位可选 1、1.5、2。设备若配置为不同停止位，需要与程序保持一致。

自定义协议模式使用文档中的 12 字节帧格式：帧头 41 41，随后为从站地址、控制字段和校验和。该模式下状态帧不包含固件版本寄存器，因此界面中的固件版本显示为空。

自定义协议没有独立的状态读取命令，因此界面会禁用“读取状态”按钮。点击“发送运动”、“全开”或“全闭”后，工具会接收并解析设备紧随控制帧返回的状态；读取失败时日志会输出原始 `TX`/`RX` 十六进制数据，便于区分无回包、转换器回显和帧格式问题。

## CAN 使用

CAN 固件版本需不低于 3.3.0。CAN ID 复用设备的 `can_node_id`。经典 CAN 使用 1 Mbit/s、80% 采样点；CAN-FD 使用仲裁域 1 Mbit/s、80% 采样点和数据域 5 Mbit/s、75% 采样点。

CAN 下行 8 字节数据顺序为 `00 Pos Vel Force Acc Dec 00 00`。注意 RS485 自定义协议手册中的速度与力矩字段顺序不同，不应直接复用串口帧体。

USB-CAN 设备需要先注册成 Linux SocketCAN 接口。经典 CAN 示例：

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 1000000 sample-point 0.8 restart-ms 100
sudo ip link set can0 up
ip -details link show can0
```

CAN-FD 示例（需要适配器支持 CAN-FD）：

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 1000000 sample-point 0.8 dbitrate 5000000 dsample-point 0.75 fd on restart-ms 100
sudo ip link set can0 up
ip -details link show can0
```

启动工具后默认选择 `CAN (SocketCAN)`；如需 CAN-FD，可切换为 `CAN-FD (SocketCAN)`。选择 `can0` 并填写正确的 CAN ID。CAN 与自定义串口协议一样，每次发送控制帧后立即接收一帧状态，不支持独立查询。

带拨码开关的夹爪切换到 CAN/CAN-FD 时，3、4 应为 ON，其他拨码为 OFF。硬件版本 20 的 Pin 3/4 分别为 CAN_H/CAN_L；硬件版本 30 的 Pin 3/4 分别为 CAN_L/CAN_H，请按机身版本确认线序。

## 排障建议

如果 GUI 能成功打开 /dev/ttyUSB0，但所有读写请求都提示 No response received，通常不是程序崩溃，而是设备侧未返回 Modbus RTU 响应。建议按这个顺序排查：

1. 固件版本不低于 3.3.5，否则没有 Modbus RTU 支持。
2. 设备拨码已切到 RS485，而不是 CAN 或 UART TTL。
3. 通过官方 REF-CLI 确认 rs485_mode = 1 且 modbus_mode = True，并保存后重启。
4. 确认 uart_baudrate、uart_stopbits、can_node_id 与本工具一致。
5. 检查 RS485 A/B 线是否接反，必要时对调 A/B 再试。
6. 检查 PC 侧转换器与设备是否共地。
7. 若仍无响应，使用 GUI 的“全地址扫描”覆盖 1-247 地址，排除从站地址未知的情况。

如果“全地址扫描”仍完全无响应，问题基本可判定在设备配置或接线，而不是本工具的寄存器实现。
