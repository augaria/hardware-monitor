# <img src="central_server/static/favicon.png" width="32" height="32" alt="icon"> Hardware Monitor

[English](README.md) | [中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](Dockerfile)

一套面向局域网 Linux 主机的轻量级硬件监控系统，采用推送（push）模式。每台被监控的机器运行一个体积很小的 Agent，按设定周期采集硬件指标并主动上报到中心服务器；中心服务器在内存中聚合数据，提供实时的 Web 仪表板，并在指标越线或机器掉线时通过通知通道发出告警。

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="Hardware Monitor 仪表板" width="640">
  <br>
  <em>实时仪表板：混合展示服务器和 NAS，阵列、磁盘和温度按严重程度自动着色。</em>
</p>

## 特性

- **推送模型** —— 由 Agent 主动上报，服务器不向客户端轮询。新增一台机器只需一条安装命令。
- **无状态服务器** —— 所有状态保存在内存中，可随时重启，最多丢失一个上报周期内的数据。
- **依赖极少** —— Agent 只依赖 `psutil`（NVIDIA GPU 可选 `pynvml`），HTTP 走标准库 `urllib`。
- **NAS 友好** —— 原生支持 mdadm 阵列、LVM、bcache、ZFS、LUKS；已在 Synology DSM、OpenMediaVault、TrueNAS 上验证。
- **可配置告警** —— 各项指标都可以配置阈值，并支持单机覆盖；带有冷却时间，避免告警风暴。
- **可插拔通知通道** —— 通过 [notifier](https://github.com/augaria/notifier) 库内建支持邮件和微信。
- **实时仪表板** —— 深色主题响应式网格，每 5 秒刷新一次，按严重程度自动着色。

## 架构

```
被监控机器（N 台）                  中心服务器（1 台）
┌──────────────────────┐            ┌──────────────────────────────────────┐
│  hardware-monitor-   │            │  central_server/main.py              │
│  agent               │            │  ┌────────────────────────────────┐  │
│                      │  POST      │  │  内存缓存                       │  │
│  psutil + pynvml     │  /report   │  │  machine → {data, last_seen}   │  │
│  每 N 秒上报一次      │ ─────────► │  └────────────────────────────────┘  │
│                      │            │  GET /api/status   (前端轮询)         │
│  systemd 托管         │            │  POST /report      (Agent 上报)      │
└──────────────────────┘            │                                      │
                                    │  central_server/alerter.py           │
                                    │  - 阈值检查                          │
                                    │  - 单条告警冷却                      │
                                    │  - 通知分发                          │
                                    │                                      │
                                    │  Docker 容器                         │
                                    └──────────────────────────────────────┘
```

## 快速开始

### 1. 部署中心服务器（Docker）

```bash
git clone https://github.com/augaria/hardware-monitor.git
cd hardware-monitor

# 构建镜像
./docker-build.sh

# 配置
cp docker-compose.sample.yml docker-compose.yml
# 编辑 docker-compose.yml —— 填写端口、阈值和通知通道

# 启动
docker compose up -d
```

仪表板访问地址：`http://<服务器 IP>:<PORT>`（默认端口 `5000`）。

### 2. 在每台被监控机器上安装 Agent

**前置条件：** 目标机器需先安装 Miniconda 或 Anaconda。

```bash
# 先把脚本下载下来再执行 —— 安装脚本是交互式的，
# 直接用 `curl ... | sudo bash` 会把脚本本身当成 stdin，read 读不到你的输入。
curl -fLO https://raw.githubusercontent.com/augaria/hardware-monitor/main/scripts/install.sh
sudo bash install.sh
```

安装脚本会询问中心服务器地址、机器名和上报间隔，然后创建 conda 环境、安装 Agent 包，并启动 systemd 服务。

**更新已安装的 Agent**（重新拉取代码，保留原有配置）：

```bash
sudo bash install.sh --update
```

可通过 `systemctl status hardware-monitor-agent` 查看运行状态。

## 采集的指标

**静态信息**（Agent 启动时采集一次，每次上报都包含）：

| 字段 | 来源 |
|---|---|
| `is_vm`、`virt_type` | `systemd-detect-virt` |
| `machine_type` | 检测到 OMV / TrueNAS / DSM / QNAP / Unraid 时为 `nas`，否则为 `server` |
| `cpu_model`、`cpu_cores` | `/proc/cpuinfo`、`psutil.cpu_count(logical=False)` |
| `memory_total_gb` | `psutil.virtual_memory().total` |
| `gpu_model` | `pynvml.nvmlDeviceGetName`（仅 NVIDIA） |

**实时指标**（每个上报周期采集一次）：

| 字段 | 来源 |
|---|---|
| `os_name` | `/etc/os-release` 的 `PRETTY_NAME`（旧版 Synology 退回到 `/etc.defaults/VERSION`）；每周期重读，OS 升级后无需重启 Agent 即可生效 |
| `cpu_usage`、`cpu_temp` | `psutil`（`k10temp` / `coretemp`） |
| `memory_usage`、`memory_used_gb` | `psutil.virtual_memory` |
| `motherboard_temp` | `acpitz` / IT8x / NCT67xx / W83 / 华硕主板芯片 |
| `gpu_usage`、`gpu_memory_usage`、`gpu_temp` | `pynvml`（仅 NVIDIA） |
| `disks` | 单盘 `{name, total_gb, used_pct, temp, state}`，从 `/sys/block` 扫描得到。SATA 温度走 `smartctl -A`；NVMe 温度依次尝试 sysfs hwmon、`psutil`、群晖 `synonvme`、`smartctl`。`state` 取值：`mounted`、`RAID mdN`、`LVM PV`、`SSD cache`、`ZFS pool`、`LUKS (locked)`、`<fstype> (not mounted)`、`unmounted`。 |
| `arrays` | 每个 mdadm 阵列 `{name, level, state, role, total_gb, used_pct, mount, members}`，来自 `/proc/mdstat`。挂载点查找会先沿 `holders/` 向上、再沿每个挂载点的 `slaves/` 向下，最后用容量匹配未归属的 `dm-*` 挂载（用于打通 Synology DSM 隐藏的缓存→数据卷链路）。`role` 为 `data` / `cache` / `swap`。 |

无法采集的字段会上报为 `null`，服务器和仪表板都会优雅地处理空值。

## 配置

中心服务器的全部配置都通过环境变量传递，完整示例见 [docker-compose.sample.yml](docker-compose.sample.yml)。

### 服务器

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `5000` | Flask 监听端口 |
| `OFFLINE_TIMEOUT` | `30` | 多少秒未上报视为掉线 |
| `ALERT_COOLDOWN_MINUTES` | `10` | 同一告警的最小重复间隔（分钟） |
| `HIDE_ARRAYS_BELOW_GB` | `10` | 仪表板自动折叠小于此容量的阵列（以及 SSD 缓存阵列） |

> **建议：** `OFFLINE_TIMEOUT` 设为 Agent `--interval` 的若干倍（例如间隔 60 秒时设为 180–300 秒），可以避免单次上报失败被误判为掉线。

### 告警阈值

留空或不设置即可关闭对应告警。

| 变量 | 单位 | 示例 |
|---|---|---|
| `ALERT_CPU_USAGE` | % | `90` |
| `ALERT_CPU_TEMP` | °C | `85` |
| `ALERT_MEMORY_USAGE` | % | `90` |
| `ALERT_MOTHERBOARD_TEMP` | °C | `75` |
| `ALERT_GPU_USAGE` | % | `95` |
| `ALERT_GPU_TEMP` | °C | `85` |
| `ALERT_GPU_MEMORY` | % | `90` |
| `ALERT_DISK_TEMP` | °C | `60` |

以上是服务器全局默认值。每台 Agent 还可以提供自己的覆盖文件，详见下一节。

### 通知通道

把 `NOTIFIER_CHANNELS` 设为一个 JSON 数组，每个元素是一个通道配置。当前支持 `email` 和 `wechat`。

```json
[
  {
    "type": "email",
    "smtp_server": "smtp.example.com",
    "email": "alerts@example.com",
    "passkey": "app-password",
    "recipients": ["admin@example.com"],
    "min_level": 30
  }
]
```

`min_level`：`10`=DEBUG，`20`=INFO，`30`=WARNING，`40`=ERROR，`50`=CRITICAL。可以同时配置多个通道，把不同级别的告警路由到不同目的地。

## 单机阈值覆盖

不同机器的"正常温度"差别很大 —— 编译服务器 80 °C 很正常，NAS 80 °C 就不正常了。每个 Agent 可以提供自己的阈值文件，中心服务器会**逐项**把它们叠加在 docker-compose 默认值之上 —— 你只需写明要修改的那几项。

**文件格式**（简单的 `KEY=VALUE`，`#` 起注释，键名与服务端环境变量一致）：

```ini
# /etc/hardware-monitor/thresholds.conf
ALERT_CPU_TEMP=80          # 这台机器本来就热，把上限放宽
ALERT_DISK_TEMP=55         # NAS 机械盘 —— 阈值更严
#ALERT_CPU_USAGE=85        # 注释掉 → 使用服务端默认值
```

完整的注释模板见 [agent/thresholds.sample.conf](agent/thresholds.sample.conf)。

**接入方式：**

| 场景 | 命令 |
|---|---|
| 首次安装并启用覆盖 | `sudo bash install.sh --thresholds-file /etc/hardware-monitor/thresholds.conf` |
| 调整某个数值（已经接入） | 编辑 `.conf`，然后 `sudo systemctl restart hardware-monitor-agent` |
| 给一台已经在跑的 Agent 加上覆盖 | `sudo bash install.sh --update --thresholds-file /etc/hardware-monitor/thresholds.conf` |
| 关闭覆盖（恢复服务端默认值） | `sudo bash install.sh --update --thresholds-file ""` |
| 仅重新拉取 Agent，不动覆盖配置 | `sudo bash install.sh --update` |

`--update` 模式是完全非交互的（方便从 cron / Ansible / CI 调用），不会询问阈值相关问题 —— 需要修改时请显式传入 `--thresholds-file`。

## Web 仪表板

仪表板每 5 秒调用一次 `GET /api/status`，每次拉到数据后重建机器卡片网格。

每张卡片包含：

- **顶部** —— 机器名、`SERVER` / `NAS` 类型徽标、在线/离线状态点、最近上报时间。卡片排序：服务器在前、NAS 在后，组内按字母序排列。
- **硬件徽标** —— 操作系统、CPU + 核心数、内存、GPU；虚拟机则只显示一个 `VM <类型>` 徽标。
- **System 行** —— CPU 使用率/温度、内存使用率、主板温度。
- **GPU 行**（如有） —— 使用率、显存使用率、温度。
- **Arrays 表格** —— 每个 mdadm 阵列一行：等级、容量、使用率、角色（`DATA` / `CACHE` / `SWAP`）、成员盘；挂载路径列在表格下方。容量过小或角色为 `cache` 的阵列默认被折叠，可点击 "+ show N hidden" 展开。
- **Disks 表格** —— 名称、容量、状态或使用率、温度。未挂载的盘显示其角色（如 `RAID md2`、`LVM PV`、`ZFS pool`），而不是使用率。

数值按严重程度自动着色（绿 / 黄 / 红）。这套视觉阈值与服务器告警阈值是独立的，定义在 [central_server/static/app.js](central_server/static/app.js)。

## 项目结构

```
hardware-monitor/
├── agent/
│   ├── hardware_monitor_agent/
│   │   └── main.py              # 指标采集 + HTTP 上报循环
│   ├── thresholds.sample.conf   # 单机阈值覆盖文件的注释模板
│   └── pyproject.toml           # 包定义；命令入口：hardware-monitor-agent
├── central_server/
│   ├── main.py                  # Flask 应用：/report、/api/status、掉线检测线程
│   ├── alerter.py               # 阈值检查、冷却逻辑、通知分发
│   ├── templates/
│   │   └── index.html           # 仪表板 HTML 框架
│   └── static/
│       ├── app.js               # 轮询循环 + 卡片 DOM 构建
│       ├── style.css            # 深色主题、响应式网格
│       └── favicon.png
├── scripts/
│   └── install.sh               # Agent 安装脚本（交互 / --update）
├── Dockerfile                   # Python 3.11-slim + Flask + notifier
├── docker-build.sh              # Docker 构建 + 可选推送
├── docker-compose.sample.yml    # 完整配置参考
├── LICENSE
└── README.md
```

## 开源许可

[MIT](LICENSE) © augaria
