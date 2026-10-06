# 故障排查

## `state=no-dhcp`

该虚拟 WAN 没有获得地址。

```sh
ifup wan2
ubus call network.interface.wan2 status
logread -e wan2 | tail -n 50
```

检查：

- MAC 格式是否正确；
- 路由器 WAN 是否确实接在墙口；
- 是否有另一台电脑同时使用相同 MAC；
- 墙口是否限制了可见 MAC 数量；
- 固件是否包含 `kmod-macvlan`。

## 有 DHCP 地址但 `state=inactive`

这通常表示认证没有生效。先在 LuCI 的“多路合并管理器”添加该 WAN 的凭据，或通过 SSH 执行 `hitwh-mwan auth set wan2`，再单独 `hitwh-mwan refresh wan2`。保存凭据或后台掉线检测本身不会登录。

需要验证码或门户使用尚未支持的加密/服务选择时，可把路由器从墙口拔下，电脑直接接墙口并设置为该 MAC，然后访问 `http://neverssl.com`，完成认证。

认证结束前不要让电脑与路由器同时使用相同 MAC。完成后接回路由器并执行：

```sh
hitwh-mwan refresh
hitwh-mwan list
```

`hitwh-mwan refresh` 先检查全部线路；只对没有 DHCP 地址的离线受管线路执行 `ifdown`/`ifup`。有地址的线路直接使用各自凭据尝试认证，不中断在线 WAN。每条线路最多登录一次，失败后等待下一次人工刷新。

常见 `AUTH_RESULT`：`missing_credentials` 为未配置；`mac_mismatch` 需重新保存凭据确认 MAC；`insecure_storage` 需检查 root 属主及目录 `0700`、文件 `0600`；`authentication_failed` 需核对账号密码；`portal_not_detected` 表示没有发现配置的可信门户；`captcha_required`、`encryption_required`、`service_required` 需手动登录；`verification_failed` 表示登录后目标 WAN 仍未通过 204 检查。不要通过公开日志或 Issue 提供密码和原始门户响应。

## 主 WAN 掉线，刷新无效

先执行 `hitwh-mwan list`。主 WAN 有地址但 `state=inactive` 通常是校园网认证失效。配置主 WAN 的凭据后手动刷新，核对固定失败原因；旧组件需离线安装新版 IPK。修改主 WAN MAC 后必须重新保存凭据确认绑定。新安装的物理 WAN 默认不在池中，应通过 GUI 创建虚拟线路。

`add random` 失败若显示 `retained offline`，说明新接口和固定 MAC 已保留。不要重复添加来重试认证；修改该 WAN 的凭据并手动刷新。容量/随机源/创建失败则没有保留新线路，已尝试的 MAC 仍登记历史。

新版默认启用 `router_failover=1`：主 WAN 离线时，客户端新连接使用其他在线出口，路由器自身默认出口也会切到健康副 WAN。主 WAN 恢复后自动切回，无需把副 WAN 的 MAC 复制到物理 WAN。查看状态：

```sh
hitwh-mwan list
ip -4 route get 1.1.1.1
```

`router_interface:wan2` 表示当前使用 wan2 作为本机默认出口；`ip route show table main` 仍可能显示原主 WAN，这不代表切换失败，实际由策略规则决定。`router_dns_configured:1` 表示已接入各线路的源地址绑定 DNS；值为 `0` 时检查是否未运行新版安装器，或 dnsmasq 已使用自定义 `serversfile`。后者需要自行保证 DNS 可以在没有主 WAN 时工作。

所有检查失败时不会提升未知线路：`router_state=unverified` 仅表示保留了上次仍有地址的出口；`unavailable` 表示没有可保留的出口。失效线路上的旧连接需要重新建立。

## 网关 Ping 不通

`10.240.255.254` 可能过滤 ICMP。Ping 失败不能单独证明线路不可用。项目使用配置的 `204` 检查页判断是否真正通过认证，默认使用 HTTP。

可以检查 ARP 邻居：

```sh
ip neigh show
```

## 只有部分任务加速

本项目按连接分流。一个长期 TCP 连接只会走一条 WAN。尝试支持多线程的下载器，或同时运行多个下载任务。重新开始任务会建立新连接，但不保证每个任务恰好落在不同出口。

## 下载开始很快，随后回落

需要同时检查下载连接和各出口流量，不能仅凭速度曲线认定是上游限速。使用下面的命令观察墙口真实流量：

```sh
hitwh-mwan speed 30
```

短时峰值不能提高长期平均速率。

如果每隔约 30 秒副出口连接停滞，最后只剩主 WAN 持续下载，先检查是否仍运行旧版更新脚本。旧版会在每轮健康检查时删除策略规则、清空路由表，即使地址和设备没有变化，也可能破坏既有 NAT 连接。新版保留未变化的路由与规则，并且不会在出口集合不变时重置 nftables 计数。

2026-10-04 的异环下载现场对照中，旧脚本运行时五个副出口停滞；暂停周期更新后，同一 CDN 的固定出口连接持续约 90 秒，每个副出口约 6 MB/s；手动执行旧脚本则立即使正常连接的连接跟踪记录消失。这是更新脚本问题的证据，不能推广为所有下载回落都由同一原因造成。

部署修复后，两条副出口连接分别在约 178.5 秒内完整下载 1 GiB，平均各约 6 MB/s；期间经过 6 轮自动健康检查和 2 次手动刷新，连接继续传输，`ip monitor route rule` 未记录路由或策略规则变化。这验证了刷新稳定性，不代表所有启动器都能持续吃满六路。

修复后仍需排查下载器的独立连接数、连接复用、连接速度差异，以及校验、解压和任务尾部。默认的独立 TCP/UDP 轮询可减少随机分配扎堆，但不保证字节流量均分；旧随机模式的对照及切换方式见 [分流算法验证](load-balancing.md)。具体下载器方案见 [`specific`](../specific/README.md)。

## OpenClash/PassWall 与本项目冲突

代理插件也可能修改连接标记和策略路由。先停用代理插件验证多 WAN：

```sh
/etc/init.d/openclash stop 2>/dev/null || true
/etc/init.d/passwall stop 2>/dev/null || true
hitwh-mwan refresh
```

如果停用后恢复，需为代理插件配置兼容的绕过规则。不要让两个系统重复使用 `0x101`–`0x110` 一带的连接标记。

## 固件软件源失效

第三方 OpenWrt/Kwrt 固件可能把软件源指向不存在的版本路径。不要从不同内核版本的官方源强行安装 `kmod-macvlan`，否则内核模块 ABI 不匹配。建议更换包含所需模块的完整固件，或使用与当前内核完全匹配的软件源。

## 恢复配置

安装、添加和删除线路时会把网络配置备份到：

```text
/root/hitwh-mwan-backups/
```

如果 LuCI 无法访问，可通过路由器串口或恢复模式把最近的 `network-*` 文件复制回 `/etc/config/network`，再重启网络。
