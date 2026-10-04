# 米哈游启动器：关闭 HTTP/2 以利用多出口下载

## 验证记录

- 日期：2026-10-04。
- 启动器：国服米哈游启动器，版本 `1.18.0.380`，下载《原神》。
- 本机：Windows，以太网链路 1 Gbps。
- 路由器：OpenWrt/Kwrt，六个可用 IPv4 出口；自定义 nftables 规则按新连接随机分流，连接标记决定出口。
- 修改前：界面约 **5 MB/s**；一条 CDN TCP 连接在 8 秒内收到约 39.5 MB，约 **4.9 MB/s**，其他出口基本空闲。
- 修改后：重启后界面观察到 **21.49 MB/s**，用户确认已经实现多路并发。该数值是现场瞬时读数，不代表长期平均速度或保证值。

## 原因与适用条件

路由器按连接做负载均衡，无法把一条普通 TCP 连接拆到多个 NAT 出口。HTTP/2 可以把多个下载请求复用到同一条 TCP 连接，因此“下载任务并发”不等于“网络连接并发”。

现场读取到 Sophon 的分块下载任务并发数为 12，但主要下载数据集中在一条 TCP 连接。目标 CDN 支持 HTTP/2。关闭启动器的 HTTP/2 后，下载实现多路并发并显著提速，支持“连接复用限制了多出口分流”的判断；未解密原下载连接逐条确认其 HTTP/2 流。

适用前提是路由器已经能把多个独立连接分配到可用出口，且出口具有可叠加的带宽。本次同一 CDN 的独立连接测试中，五个出口各约 4.4–6.7 MB/s；另一个出口较慢。此方案不为单线路增加带宽。

机制参考：[HTTP/2 标准 RFC 9113](https://www.rfc-editor.org/rfc/rfc9113/)、[OpenWrt 多 WAN 文档](https://openwrt.org/docs/guide-user/network/wan/multiwan/mwan3)。本次路由器使用自定义 nftables 分流，并未使用 mwan3 软件包。

## 本次实际修改

启动器包含隐藏的 `disableHttp2` 用户设置；本次设置界面没有提供对应入口。它不是 `config.ini` 中已验证可用的选项，也不能用普通 JSON 编辑器修改。

配置位于：

```text
%APPDATA%\miHoYo\HYP\1_1\data\usersettings.dat
%APPDATA%\miHoYo\HYP\1_1\data\usersettings.dat.crc
```

文件使用 MMKV 二进制存储。本次格式为未加密、元信息版本 4。修改流程为：

1. 正常退出启动器并确认后台的 `HYP.exe`、`HYPHelper.exe` 已结束。
2. 成对备份数据文件和校验文件。
3. 将已有的 `disableHttp2` 布尔值由 `false` 改为 `true`；数据文件仅改变一个字节。
4. 重算有效数据区的 CRC32，更新元信息中的校验、序列号及已确认数据长度/校验。
5. 重新解析，确认其他设置不变、校验正确，再启动启动器并续传。

下载限速开关原本关闭，本次保持原值。路由器分流、系统网络参数、证书校验、代理配置均沿用原设置。

MMKV 格式依据：[MMKVMetaInfo.hpp](https://github.com/Tencent/MMKV/blob/master/Core/MMKVMetaInfo.hpp)、[MMKV_IO.cpp](https://github.com/Tencent/MMKV/blob/master/Core/MMKV_IO.cpp)。脚本仅接受本次验证过的格式；未来启动器版本改变格式时会拒绝修改。

## 使用脚本

脚本：[http2_setting.py](http2_setting.py)，仅依赖 Python 3 标准库，面向 Windows。下面的 PowerShell 命令从项目根目录执行；使用能运行 Python 3 的 `python` 命令。

### 读取当前设置

```powershell
python .\specific\mihoyo-launcher\http2_setting.py
```

输出 `disableHttp2`、下载限速状态和 CRC 校验结果，不修改文件。`disableHttp2: true` 表示 HTTP/2 已关闭。

### 预演修改

```powershell
python .\specific\mihoyo-launcher\http2_setting.py --set true --dry-run
```

预演在内存中生成修改，验证只有目标设置变化，不写配置或备份。

### 开启优化

先从系统托盘正常退出启动器；关闭主窗口可能只会最小化。然后执行：

```powershell
python .\specific\mihoyo-launcher\http2_setting.py --set true
```

实际修改会在脚本目录的 `backups` 下创建独立备份目录，包含原始文件对和修改记录。脚本发现启动器仍在运行时会拒绝写入，且不会主动终止进程。

完成后正常打开启动器并继续下载。脚本不负责启动应用，也不负责点击续传。

### 恢复 HTTP/2

正常退出启动器后执行：

```powershell
python .\specific\mihoyo-launcher\http2_setting.py --set false
```

再重新启动。此操作仅恢复 HTTP/2 开关，并保留后续对其他设置的修改。

如需完整恢复某次配置，可在启动器退出后将该次备份中的两个文件成对复制回原目录；完整恢复也会恢复那次备份时的其他设置。

### 自定义配置和备份路径

```powershell
python .\specific\mihoyo-launcher\http2_setting.py --data-dir 'D:\config-copy' --set true --dry-run
python .\specific\mihoyo-launcher\http2_setting.py --set true --backup-root 'D:\launcher-backups'
```

`--data-dir` 指向同时包含两个配置文件的目录，适合在副本上检查。默认使用当前 Windows 用户的 `%APPDATA%`；为其他用户操作时须明确指定路径。

## 验证方式与限制

1. 在持续下载阶段记录修改前后速度；排除暂停、校验、解压阶段。
2. 同时观察 CDN 连接与各 WAN 接收流量；以多条连接承担下载流量、多个出口同时收包为成功依据，不仅看连接总数。
3. 等待速度稳定后比较多个时间窗口。当前已有一次现场提速验证，尚未做长期平均速度统计。
4. 如果无改善、出错或只有一条连接持续下载，按上述方式恢复 HTTP/2，再排查 CDN、出口带宽和下载器版本。

关闭 HTTP/2 后仍可能受到 HTTP/1.1 连接池、磁盘处理速度、CDN 或分流分布限制。随机分流会让多条连接落到同一个出口，因此六路峰值约 30 MB/s 并不意味着启动器一定达到 30 MB/s。仅增大任务并发数也不保证增加独立连接。

本次还有一个较慢出口，且路由器的健康检查只检查连通性。这些是后续优化方向，本次未修改出口权重或分流算法。

本目录提供整理后的方案和可重复使用的脚本，不包含现场个人配置备份。运行脚本时产生的 `backups` 目录已加入本目录的 Git 忽略规则。
