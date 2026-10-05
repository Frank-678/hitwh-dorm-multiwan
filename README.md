# HITwh 寝室校园网多路聚合

在哈尔滨工业大学（威海）寝室的一个有线网口上，通过 OpenWrt `macvlan` 创建多个独立 MAC/DHCP 会话，并用 nftables 与策略路由进行**按连接负载均衡**。当校园网按已认证终端独立限速时，三个已认证 MAC 可以让多线程下载和多设备并发流量达到接近三倍的持续总带宽。

> 本项目只用于本人账号或明确获授权的校园网终端。请遵守学校网络使用规定。项目不会绕过认证，也不会保存账号和密码。

## 已验证环境

- 校区：哈尔滨工业大学（威海）
- 接入：寝室墙上有线网口，DHCP 获取 `10.240.0.0/16` 地址
- 网关：`10.240.255.254`
- 认证：锐捷 ePortal，未认证终端会跳转到 `172.26.156.158/eportal/`
- 限速：约 5 MB/s/已认证终端，多个终端的限速彼此独立
- 路由器：中国移动 RAX3000M NAND 版，OpenWrt 24.10 系固件
- 三路结果：多连接下载的持续总速度可接近 `3 × 5 MB/s`

其他楼宇、年级或时间段的认证策略可能不同。先确认同一墙口允许多个 MAC 获取 DHCP 地址，并确认不同终端的限速彼此独立。

## 所需设备

1. 一台 **RAX3000M** 路由器，闲鱼价格约 140 元。NAND 版已经足够，不需要 eMMC、USB 口或外置交换机。
2. 一台可以修改有线网卡 MAC 地址的电脑，用于依次认证三个 MAC。
3. 三个允许同时在线的校园网终端名额。优先使用本人账号允许的设备数；使用他人账号前必须获得本人授权。

路由器固件需要包含：

- `kmod-macvlan`
- `nftables`/`firewall4`
- `ip-full`
- `curl`
- `jsonfilter`

## 工作原理

```mermaid
flowchart LR
    Internet[校园网网关] --- Port[寝室单个千兆墙口]
    Port --- Eth[eth1 物理 WAN]
    Eth --- W1[wan<br/>认证 MAC 1]
    Eth --- W2[macwan2 / wan2<br/>认证 MAC 2]
    Eth --- W3[macwan3 / wan3<br/>认证 MAC 3]
    W1 --> PBR[nftables 连接标记<br/>独立策略路由表]
    W2 --> PBR
    W3 --> PBR
    PBR --> NAT[OpenWrt NAT]
    NAT --> LAN[寝室有线与 Wi-Fi 设备]
```

默认把新连接轮询分配到在线线路，TCP 与 UDP 分别使用独立计数器，避免 DNS 等 UDP 短连接占用 TCP 的轮询顺序。连接跟踪标记保证同一连接始终从原线路返回。后台每 30 秒检查一次 `204` 响应，认证失效的线路自动退出池，恢复后自动加入。

健康检查保留未变化的策略路由规则和路由表；只有接口地址、网关或设备变化时才调整对应线路。分流规则仅在分流模式、可用出口集合变化或防火墙规则被重置时更新，避免周期刷新破坏正在下载的连接或重置轮询顺序。策略表还保留不可达兜底路由，防止失效线路的既有连接误走默认 WAN。

默认启用路由器自身流量的主备切换：优先使用主 `wan`；主 WAN 认证失效或没有 DHCP 地址时，自动选择通过健康检查的副 WAN 作为默认出口。当前副出口仍在线时继续使用它，失效后另选在线副出口；主 WAN 恢复后自动切回。切换随每轮健康检查执行，客户端仍在全部在线线路之间负载均衡。不会修改线路 MAC、交换接口身份或重启在线 WAN；失效线路上的原有连接仍需应用重连。

安装器给 dnsmasq 配置受管的 `serversfile`，更新器从各线路 DHCP 状态中读取 DNS，并将查询绑定到该线路的源地址。因此主 WAN 没有 DHCP 时，副 WAN 的 DNS 仍可用于健康检查和域名解析。已有自定义 `serversfile` 会保留，此时需自行保证 DNS 不依赖主 WAN。`/tmp/hitwh-mwan.status` 的 `router_interface` 表示路由器当前默认出口，`router_state=unverified` 表示所有检查失败时保留上次仍有地址的出口，而不是认定其在线。

如需关闭路由器自身的主备切换（客户端多 WAN 分流仍保留）：

```sh
uci set hitwh_mwan.main.router_failover='0'
uci commit hitwh_mwan
hitwh-mwan list
```

改为 `1` 可恢复。旧 UCI 配置未设置该选项时也默认启用；升级需运行新版安装器以接入副 WAN DNS。

这不是逐包链路绑定。单个 TCP 连接仍然只能使用一条线路；百度网盘、Steam、BT、启动器、多设备并发等多连接场景最容易获得叠加效果。

轮询改善新连接数量的分布，不保证各出口字节数或速度完全相等。连接速度、存活时间、HTTP/2 复用和 CDN 调度仍会影响带宽利用率。算法对照与性能边界见 [分流算法验证](docs/load-balancing.md)。

如需使用原来的随机分流，可在路由器上切换：

```sh
uci set hitwh_mwan.main.balance_mode='random'
uci commit hitwh_mwan
hitwh-mwan list
```

将值改为 `round_robin` 可恢复轮询。已有连接继续使用原出口，新算法只影响之后建立的连接；`/tmp/hitwh-mwan.status` 显示当前 `balance_mode`。升级保留旧 UCI 配置时，未设置该选项也默认采用轮询。

## 第一步：准备并认证三个 MAC

可使用三个本地管理、单播 MAC，例如：

```text
02:11:22:33:44:51
02:11:22:33:44:52
02:11:22:33:44:53
```

不要直接照抄示例；请修改后几组十六进制数字，避免与同楼设备冲突。首字节使用 `02` 可以保证它是本地管理的单播地址。

依次完成三次认证：

1. 拔掉路由器 WAN，将电脑直接接到寝室墙口。
2. 把电脑有线网卡改为第一个 MAC。
3. 禁用并重新启用网卡，确保重新申请 DHCP 地址。
4. 打开 `http://neverssl.com`，等待跳转至 HITwh 锐捷认证页。
5. 使用本人或获授权账号登录，并开启“无感知认证/无感知登录”。
6. 确认该 MAC 可以直接访问 HTTPS 网站。
7. 换成第二、第三个 MAC，重复上述操作。

认证某个 MAC 时，路由器不能同时使用这个 MAC，否则会产生二层地址冲突。不要保存或分享带完整查询参数的认证 URL。

Windows 的“网络地址/Network Address”属性通常要求填写不带冒号的形式，例如 `021122334451`。全部认证完成后，把电脑网卡恢复为原始 MAC。

## 第二步：安装

路由器接回墙口，电脑连接路由器 LAN，然后 SSH 登录：

```sh
ssh root@192.168.100.1
```

在线安装：

```sh
curl -fsSL https://raw.githubusercontent.com/ponder-j/hitwh-dorm-multiwan/main/install.sh -o /tmp/install.sh
sh /tmp/install.sh
```

也可以克隆仓库后，把整个目录复制到路由器并运行本地 `install.sh`。

安装器会：

- 备份 `/etc/config/network` 与 `/etc/config/firewall`
- 安装管理、健康检查和开机启动脚本
- 建立独立 nftables 表与策略路由规则
- 关闭可能绕过连接标记的软硬件流量分载
- 将自定义文件加入 OpenWrt 升级保留列表

## 第三步：加入三条线路

将第一个已认证 MAC 设置为主 WAN：

```sh
hitwh-mwan set-main 02:11:22:33:44:51
```

加入另外两个已认证 MAC：

```sh
hitwh-mwan add 02:11:22:33:44:52
hitwh-mwan add 02:11:22:33:44:53
```

查看结果：

```sh
hitwh-mwan list
```

预期输出类似：

```text
active_count:3
active_interfaces: wan wan2 wan3
wan  ... state=active mark=0x101
wan2 ... state=active mark=0x102
wan3 ... state=active mark=0x103
```

`state=inactive` 通常表示该 MAC 尚未完成无感知认证；`state=no-dhcp` 表示墙口没有为它返回 DHCP 租约。

## 后续扩展与管理

添加一个新的已认证 MAC：

```sh
hitwh-mwan add AA:BB:CC:DD:EE:FF
```

脚本会自动选择下一个 `wanN`，创建 macvlan、申请 DHCP、加入防火墙和均衡池。默认最多支持 16 条线路，包括主 WAN。

```sh
# 查看并重新检查全部线路，不重启接口
hitwh-mwan list

# 检查并重连离线线路；在线线路不会中断
hitwh-mwan refresh

# 删除一条线路，三种写法均可
hitwh-mwan remove wan4
hitwh-mwan remove 4
hitwh-mwan remove AA:BB:CC:DD:EE:FF

# 观察墙口真实接收速度；按 Ctrl+C 停止
hitwh-mwan speed

# 观察 30 秒
hitwh-mwan speed 30
```

## 本地可视化监控

仓库中的 `dashboard` 是一个不依赖第三方 Python 包的本地网页仪表盘。它通过一条持久 SSH 连接，每 2 秒读取一次路由器已有的状态文件、网卡字节计数和系统负载；浏览器关闭、切到后台或点击暂停后会停止采样。路由器不运行 Web 服务，也不会执行测速。

运行前需要安装 Python 3.10 或更高版本，并确保 `ssh` 可用；macOS/Linux 的启动脚本使用 `python3`，Windows 的启动脚本使用 `python`。先运行 `ssh root@192.168.100.1` 确认密钥登录正常。OpenWrt 的 Dropbear 使用 `/etc/dropbear/authorized_keys` 保存 root 的授权公钥。

启动脚本启用 Python 的 UTF-8 模式，确保中文提示在英文系统和重定向输出时也能正常显示。SSH 采样脚本统一使用 UTF-8 与 LF 换行发送，避免 Windows 的 CRLF 导致远端 `sh` 报语法错误。仓库通过 `.gitattributes` 保持文本文件的 LF 换行；GitHub Actions 在 Linux、macOS、Windows 上运行回归测试并检查各自的启动脚本。

macOS 或 Linux：

```sh
./dashboard/start.command
```

Windows PowerShell：

```powershell
.\dashboard\start.ps1
```

也可以直接运行：

```sh
python3 -X utf8 dashboard/server.py --router root@192.168.100.1
```

仪表盘默认只监听本机 `127.0.0.1:8765`，并自动打开浏览器。它使用现有 SSH 密钥或 SSH Agent，不保存路由器密码。页面显示总下载/上传、每条线路的实时速度和占比、在线状态、CPU、内存及连接跟踪使用量。

## specific：具体下载器的多路优化

[`specific`](specific/README.md) 用于记录针对具体下载器的多路下载优化方案。下载器的协议、连接复用和并发策略会影响按连接负载均衡的效果；每个下载器使用独立目录，包含问题原因、适用版本、操作步骤、配套脚本、验证结果和恢复方法。

当前方案：

- [米哈游启动器](specific/mihoyo-launcher/README.md)：关闭隐藏的 HTTP/2 设置，使独立下载连接参与多 WAN 分流；已在六出口环境中验证提速。

这些方案在下载器所在的电脑上单独应用，路由器安装脚本不会自动执行。欢迎将其他下载器的多路优化方式贡献到 `specific`，详见 [贡献指南](CONTRIBUTING.md)。

## 性能边界

- 每条线路约 5 MB/s 时，三路理论持续总量约 15 MB/s。
- 所有虚拟线路共用一个千兆墙口，物理极限为 1 Gbit/s；实际 TCP 有效速度通常低于 125 MB/s。
- RAX3000M 使用双核 Cortex-A53。为了保证策略路由，项目关闭流量分载；线路很多、总流量达到数百 Mbit/s 后，CPU 软件 NAT 可能先成为瓶颈。
- 校园网还可能限制单墙口总带宽、允许的 MAC 数、DHCP 租约数或账号在线设备数。
- 限速器的启动瞬时突发不能变成持续带宽。长期速度仍取决于每个认证终端的稳定限速之和。

更多解释见 [原理与性能边界](docs/principles.md)。遇到问题请看 [故障排查](docs/troubleshooting.md)。

## 卸载

```sh
curl -fsSL https://raw.githubusercontent.com/ponder-j/hitwh-dorm-multiwan/main/uninstall.sh -o /tmp/uninstall.sh
sh /tmp/uninstall.sh
```

卸载器只删除本项目创建的 `wan2`–`wanN` 与脚本，保留主 WAN 的 MAC。配置备份保存在 `/root/hitwh-mwan-backups/`。

## 安全与隐私

- 项目不需要校园网账号、密码，也不会自动提交认证表单。
- 不要把 `/etc/config/network` 直接上传到 Issue，其中可能含固件向导遗留的敏感字段。
- 示例、日志和 Issue 中应遮盖账号、真实 MAC、认证 URL 参数与内网地址。
- 只添加本人或明确获授权使用的认证终端。

## 开发检查

```sh
make check
```

项目采用 [MIT License](LICENSE)。
