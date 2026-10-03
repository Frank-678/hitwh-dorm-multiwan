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

这通常表示认证没有生效。把路由器从墙口拔下，电脑直接接墙口并设置为该 MAC，然后访问 `http://neverssl.com`，完成认证并开启无感知登录。

认证结束前不要让电脑与路由器同时使用相同 MAC。完成后接回路由器并执行：

```sh
hitwh-mwan refresh
hitwh-mwan list
```

## 网关 Ping 不通

`10.240.255.254` 可能过滤 ICMP。Ping 失败不能单独证明线路不可用。项目使用 HTTPS `204` 响应判断是否真正通过认证。

可以检查 ARP 邻居：

```sh
ip neigh show
```

## 只有部分任务加速

本项目按连接分流。一个长期 TCP 连接只会走一条 WAN。尝试支持多线程的下载器，或同时运行多个下载任务。重新开始任务会建立新连接，但不保证每个任务恰好落在不同出口。

## 下载开始很快，随后回落

这通常是上游突发限速或下载器统计缓存。使用下面的命令观察墙口真实流量：

```sh
hitwh-mwan speed 30
```

短时峰值不能提高长期平均速率。

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
