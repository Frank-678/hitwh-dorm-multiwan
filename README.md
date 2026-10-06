# 多路合并管理器

在 OpenWrt 路由器上，把一个有线校园网口提供的多个 MAC/DHCP 会话组成线路池。LuCI 图形界面负责新增、刷新、修改和删除线路；nftables 与策略路由按连接分配出口。账号认证只在用户新增随机线路或手动刷新时执行。

项目最初用于哈尔滨工业大学（威海）的锐捷 ePortal 网络。核心实现使用 OpenWrt 自带的 shell、ucode、rpcd 和 LuCI，路由器不需要安装 Python、Node.js 或 Go。页面的流量历史保存在浏览器内存中。

## 适用条件

符合以下条件的校园网可以评估使用本方案：

- 一个宿舍有线网口允许多个不同 MAC 同时申请独立 IPv4 DHCP 租约。
- 通过本项目支持的锐捷 **ePortal** 协议认证；当前支持无验证码、无需密码加密、无需服务选择的登录流程，以及 JavaScript/HTTP Location 门户跳转。
- 账号允许相应数量的设备同时在线，或每条线路分别使用本人/舍友明确授权的账号。
- 限速按已认证终端独立计算，且宿舍网口的总带宽、路由器的转发能力能够承载叠加后的流量。

使用锐捷认证本身不足以保证兼容：802.1X 客户端认证、验证码、不同加密方式、服务选择、仅允许一个 MAC 或账号统一总限速，都需要另外评估或适配。不同学校需在“接入设置”中确认其可信门户地址。

这是**按连接负载均衡**：多线程下载、多设备和多个 TCP/UDP 会话可以利用多路总带宽，单个 TCP 连接仍使用一条线路。一个账号若有统一总限速，增加 MAC 不会提高该账号的总带宽。

## 已验证的 OpenWrt 环境

| 项目 | 本机环境 |
| --- | --- |
| 路由器 | CMCC RAX3000M NAND |
| 固件 | Kwrt/OpenWrt `24.10-SNAPSHOT` |
| 内核 | `6.6.116` |
| Target / 架构 | `mediatek/filogic` / `aarch64_cortex-a53` |
| Web 管理 | LuCI + nginx；也支持使用 LuCI 的 uhttpd 环境 |
| 认证 | HITwh 锐捷 ePortal，默认 `http://172.26.156.158/eportal` |

IPK 的架构为 `all`，因为包内只有脚本和页面资源；底层依赖仍必须匹配路由器固件。其他设备需有支持 macvlan 的有线 WAN、LuCI、rpcd 的 ucode 插件和足够的剩余空间。

## 第一步：离线安装

路由器已经安装 OpenWrt，并连接宿舍网线。电脑连接路由器 LAN/Wi-Fi，即使校园网尚未认证，也能通过局域网 SSH 和 LuCI 安装配置。

在**有互联网的电脑**从 [GitHub Release](https://github.com/ponder-j/hitwh-dorm-multiwan/releases/tag/v1.0.0-8) 下载成品 `luci-app-hitwh-mwan_1.0.0-8_all.ipk` 和 `SHA256SUMS`。也可以在电脑克隆本仓库后构建，Python 只用于电脑打包，无第三方包依赖：

```sh
python3 tools/build_ipk.py
# Windows 也可以用 python tools/build_ipk.py
```

构建产物在 `dist/`。将 IPK 传到路由器的 `/tmp`，然后离线安装：

```sh
scp -O dist/luci-app-hitwh-mwan_1.0.0-8_all.ipk root@192.168.100.1:/tmp/
ssh root@192.168.100.1 "opkg install /tmp/luci-app-hitwh-mwan_1.0.0-8_all.ipk"
```

下载的成品 IPK 可替换命令中的 `dist/...` 路径。示例使用本机 LAN 地址 `192.168.100.1`；其他路由器请替换为其 LAN 地址。`scp -O` 使用 OpenWrt Dropbear 常见的 SCP 传输方式。

传输前在 IPK 和 `SHA256SUMS` 所在目录执行 `sha256sum -c SHA256SUMS`。Windows PowerShell 可用 `Get-FileHash .\luci-app-hitwh-mwan_1.0.0-8_all.ipk -Algorithm SHA256`，与校验文件中的摘要比较。

安装依赖：`luci-base`、`rpcd-mod-ucode`、`ucode`、`ucode-mod-fs`、`ucode-mod-uci`、`curl`、`jsonfilter`、`ip-full`、`kmod-macvlan`、`firewall4`、`coreutils-stat`，以及提供 `flock`、`hexdump`、`setsid`、`sha256sum`、`tar` 的 BusyBox。本机固件已具备这些依赖，安装前会检查必需命令。

如果固件缺少依赖，在联网电脑下载**该固件软件源、该架构**对应的 IPK，一起 SCP 到路由器后安装。`kmod-macvlan` 必须与正在运行的内核版本和 ABI 匹配；厂商 SNAPSHOT 固件应使用其配套源。离线安装时，路由器无法替你从互联网补齐缺失依赖。

安装器会检查组件和剩余空间，备份网络/防火墙配置，启用健康监控及分流规则。它不会创建线路，也不会提交任何账号认证。新安装不把原始物理 WAN 自动加入线路池；升级保留已有配置和凭据。

安装后登录 OpenWrt 的 LuCI，打开 **网络 → 多路合并管理器**。必要时刷新 LuCI 页面。页面、接口和权限校验都在路由器上运行，使用时不需要电脑保持 SSH 会话。

## 第二步：确认校园网接入设置

HITwh 可使用默认设置。其他学校点击“接入设置”：

- **WAN 父设备**：接宿舍网口的实际网络设备，本机为 `eth1`。
- **LAN 网桥**：客户端所在网桥，通常为 `br-lan`。
- **可信认证门户**：学校的 ePortal 基础地址，例如 `http://认证服务器/eportal`，不填写登录 URL 的查询参数。
- **204 检查地址**：用于确认目标线路已经通过认证的地址。

“检测门户地址”只读取底层 WAN 的认证跳转，不提交账号密码。检查检测到的地址确实属于学校后，再保存设置。已有线路时无法更换 WAN 父设备，避免把现有线路迁移到错误网卡。

网关与 IPv4 子网从各线路 DHCP 状态读取，不要求学校使用 `10.240.0.0/16`。如果底层 WAN 没有 DHCP 地址，先在 LuCI 网络接口中确认物理 WAN 使用 DHCP、网线接入和驱动正常。

## 第三步：新增线路

初次安装页面显示 **0 条已配置线路**。点击“新增线路”，选择：

### 随机生成 MAC 并认证

输入本次线路的校园网账号和密码，点击“添加并认证”。流程为：

1. 检查容量和运行环境。
2. 从 `/dev/urandom` 生成本地管理的单播 MAC，避开本机配置、运行时设备、凭据绑定、备份和 MAC 历史中的地址。
3. 创建固定 MAC 的 macvlan/DHCP 线路，保存这条线路的私有凭据。
4. 只为新线路提交一次源地址绑定的 ePortal 登录，通过 204 检查后加入在线线路池。

无需在电脑上修改 MAC 或逐个预先登录。需要更多线路时重复新增，并使用允许同时在线的设备名额或其他获授权账号。

创建失败会回滚本次新配置。创建完成后，DHCP 或认证失败会保留该线路、固定 MAC 和受保护凭据，并显示失败原因；修改凭据后手动刷新该线路即可。失败不会自动换 MAC、踢设备或后台重试登录。

### 使用已认证的 MAC

输入 MAC 后直接创建线路并检查其在线状态，不提交登录。该 MAC 应已经具有有效的校园网会话，且其他设备当前没有同时使用它。后续可以通过“添加凭据”补上账号，以便认证过期时手动恢复。

新建虚拟线路使用 `wan2`、`wan3`…，跳过保留编号 `wan6`。默认可新建 15 条虚拟线路；旧配置若保留物理主 WAN 作为线路，则总共最多 16 条。

## 日常使用

每条线路都有独立操作：

| 操作 | 行为 |
| --- | --- |
| 添加/查看凭据 | 查看、修改或删除该线路的账号密码；密码默认遮盖；保存不触发登录 |
| 刷新 | 只恢复选中的线路，其他账号不参与认证 |
| 修改 MAC | 改变该线路身份；旧凭据绑定失效，需重新保存确认 |
| 删除线路 | 删除线路配置和凭据；保留 MAC 历史以避免复用 |
| 刷新离线线路 | 只处理本轮离线的受管线路；在线线路不登录、不重启 |

单条刷新时，该线路按钮显示“刷新中”，结果保留在线路旁，包括未配置凭据、认证失败或“已在线，无需认证”。采样更新保留按钮节点，不打断鼠标点击或键盘焦点。

“新增随机线路”和凭据弹窗提供“复用已有凭据”下拉菜单，显示已保存账号及来源线路。相同账号、密码合并为一个选项，密码只在选中后传入表单，默认仍遮盖。选择本身不保存、不登录；点击保存或添加后，目标线路得到独立副本并绑定自己的 MAC。以后修改一条线路的凭据不会修改其他线路。

删除旧配置中的物理主线路时，它退出线路池且凭据被删除，物理设备保留作其他 macvlan 的底层接口。

有 DHCP 地址的离线线路直接认证；缺少地址时先恢复 DHCP。每次刷新每条线路最多登录一次。页面打开、浏览器刷新、掉线、监控、hotplug 和路由器启动都不会提交认证。

操作通过 LuCI/rpcd 异步任务执行，有有限超时且没有自动重试队列。关闭页面后，已经明确提交的单次操作仍可能完成；再次操作前查看线路状态。全体刷新预算 270 秒，单个图形操作预算约 300 秒。

## 命令行方案

SSH 登录路由器后，可使用同一套管理逻辑：

```sh
hitwh-mwan add random                 # 交互输入账号密码，创建并认证一条线路
hitwh-mwan add 02:11:22:33:44:55      # 添加已认证的 MAC
hitwh-mwan list                       # 检查/列出线路，不提交认证
hitwh-mwan refresh wan2               # 只恢复 wan2
hitwh-mwan refresh                    # 恢复全部离线受管线路
hitwh-mwan auth set wan2              # 交互保存凭据，密码不回显
hitwh-mwan auth status                # 仅显示配置是否存在，不显示账号密码
hitwh-mwan auth remove wan2           # 删除凭据，不删除线路
hitwh-mwan edit wan2 02:11:22:33:44:66
hitwh-mwan remove wan2                # 删除线路及对应凭据
hitwh-mwan portal                     # 只检测底层 WAN 的门户地址
hitwh-mwan upgrade                    # 手动检查 GitHub，有兼容新版本则校验并安装
hitwh-mwan speed 30                   # 读取流量计数，观察 30 秒
```

`add random --stdin` 是图形界面的 JSON 标准输入入口；请通过交互命令录入密码，避免写进 shell 历史或进程参数。旧版 `set-main <MAC>` 仍可将物理主 WAN 加入线路池。

默认轮询分配新 TCP/UDP 连接，既有连接保留原出口。也可切换随机分流：

```sh
uci set hitwh_mwan.main.balance_mode='random'
uci commit hitwh_mwan
hitwh-mwan list
```

改回 `round_robin` 可恢复轮询。健康线路退出/恢复会调整分流池；默认出口优先使用旧配置中仍在线的主 WAN，否则使用健康副线路。

## 凭据与存储

凭据只在路由器 `/etc/hitwh-mwan/auth.d/<WAN>.json` 创建，目录 root `0700`、文件 root `0600`。它是权限保护的明文存储；root 和完整磁盘备份能够读取。默认不将凭据目录加入 sysupgrade 保留列表。

LuCI 登录和专用 ACL 保护管理接口，凭据读取需要管理写权限。账号密码不会进入仓库、UCI 网络配置、采样 JSON、图表历史、URL、浏览器 localStorage/sessionStorage 或公共日志；子进程通过私有标准输入接收凭据。关闭凭据弹窗清空输入。

建议通过 HTTPS 使用 LuCI。HITwh 当前门户登录为 HTTP，因此路由器到该门户的一段仍无传输加密，文件权限不能解决这一点。项目不会关闭门户 TLS 证书校验。

IPK 只包含必要脚本和页面，打包器限制文件内容总量不超过 512 KiB，安装前要求至少 4 MiB 剩余 overlay，为 opkg 元数据写入留出空间。本机包约 41 KiB 压缩、134 KiB 文件内容；多次测试期间，包含 opkg 元数据和备份的 overlay 增量约 0.8–2.2 MiB，随 UBIFS 回收变化，当前仍余约 17 MiB。依赖包的空间应另计。

网络配置操作保留最近 8 份平面网络快照；包安装备份保留最近 3 份，其他旧备份和 MAC 历史保留在 root 私有目录。图表历史仅占浏览器内存；操作临时文件位于 RAM 的 `/tmp`，数量有上限并按需清理，凭据交接文件读取后立即移除。

## 升级与卸载

安装 `1.0.0-6` 后，可以点击 Dashboard 的“检查更新”，或 SSH 执行 `hitwh-mwan upgrade`。它读取 GitHub 最新正式 Release；有更高版本时自动下载 IPK 与 `SHA256SUMS`，核对 GitHub 附件摘要、文件校验值、包名、版本、架构、OpenWrt 系列和 opkg 依赖，再安装。不会自动轮询更新，也不会调用校园网登录；当前没有更新时只检查版本。

更新使用 HTTPS 和正常证书校验，不需要 GitHub 令牌，也不上传校园网凭据。下载与执行副本放在 root 私有的 RAM 临时目录，完成后清理；真实校验或依赖失败不会强制安装。更新时需要路由器能够访问 GitHub，且至少有 4 MiB 剩余 overlay。安装期间避免断电。

安装任务独立于管理服务运行，正常更新保留网络身份和凭据。成功后按页面提示刷新；rpcd 会在结果写入后重载，可能需要重新登录 LuCI。跨版本兼容以包内的 OpenWrt 系列标记为准，当前支持 `24.10`。GitHub 最新 Release 的含义见 [官方 API 文档](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)。

安装时更新页面文件的修改时间，避免可复现 IPK 的固定时间戳导致浏览器继续使用旧脚本。从早期版本升级后，首次请重载整个 LuCI 页面并按 Ctrl+F5；凭据列表读取失败会显示具体原因，会话过期时重新登录即可。

首次离线安装及无法访问 GitHub 时，仍可 SCP 新 IPK 后执行 `opkg install /tmp/新版本.ipk`。现有配置和本地凭据保留，升级本身不触发认证。

```sh
opkg remove luci-app-hitwh-mwan
```

卸载会停止管理和分流，保留已有网络接口、私有凭据和 MAC 历史，方便重新安装或手动迁移。需要彻底删除某条线路时，先在图形界面或 CLI 显式删除，再卸载软件包。需要清除保留的凭据时，由 root 显式删除 `/etc/hitwh-mwan/auth.d`；不要把该目录或完整系统备份上传到 Issue。

## 开发与验证

```sh
make check
make ipk
```

Windows 可分别运行 Python 测试及 `python tools/build_ipk.py`；POSIX 权限、信号和锁测试在 Linux/WSL 中验证。`dashboard/server.py` 保留为可选的电脑端 SSH 调试入口，发布 IPK 不包含它。

浏览器交互回归见 `dashboard/tests/test_ui.cjs`，CI 使用 Node.js 24、Playwright 1.62.1 和 Chromium 执行 `node --test dashboard/tests/test_ui.cjs`。这些仅为开发测试依赖，IPK 不包含它们。

构建与 CI 检查私有凭据文件和常见秘密字面量，CI 同时扫描可达 Git 历史和 IPK 内的脚本及页面；测试仅使用虚构凭据。发布前可执行 `python3 tests/check_secrets.py --history --ipk dist/*.ipk`。提交前检查可用 `git config core.hooksPath .githooks` 启用；macOS/Linux 需给钩子执行权限。

IPK 使用 [OpenWrt 24.10 官方构建格式](https://github.com/openwrt/openwrt/blob/openwrt-24.10/scripts/ipkg-build)，管理接口采用 [rpcd 的原生 ucode 插件机制](https://lxr.openwrt.org/source/rpcd/examples/ucode/example-plugin.uc)。更多背景见 [工作原理](docs/principles.md)、[分流算法验证](docs/load-balancing.md) 和 [故障排查](docs/troubleshooting.md)。

项目采用 [MIT License](LICENSE)。
