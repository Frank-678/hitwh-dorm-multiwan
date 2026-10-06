#!/usr/bin/env python3
"""Local-only dashboard for the HITwh multi-WAN router.

The browser talks only to this process. While at least one dashboard tab is
visible, one persistent SSH session reads lightweight counters from OpenWrt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shlex
import subprocess
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
REFRESH_TIMEOUT = 300
AUTH_MESSAGES = {
    "missing_credentials": "未配置凭据", "no_dhcp": "尚未获得 DHCP 地址",
    "mac_mismatch": "MAC 已变更，请重新保存凭据", "insecure_storage": "凭据文件权限不安全",
    "captcha_required": "需要验证码，请手动登录", "encryption_required": "门户要求密码加密，请手动登录",
    "service_required": "门户要求选择服务，请手动登录", "network_error": "网络请求失败",
    "portal_not_detected": "未发现可信认证门户", "authentication_failed": "认证失败，请检查凭据",
    "verification_failed": "登录后外网检查仍未通过", "address_changed": "地址已改变，请重新刷新",
    "helper_unavailable": "请升级路由器认证组件", "invalid_credentials": "账号或密码无效",
    "invalid_interface": "不是受管 WAN", "root_required": "需要 root 权限",
    "storage_failed": "凭据存储失败", "mac_unavailable": "无法读取线路 MAC",
    "invalid_response": "门户响应无效", "invalid_portal": "门户配置无效",
}

RECONNECT_SCRIPT = r'''#!/bin/sh
if command -v hitwh-mwan >/dev/null 2>&1; then
	exec hitwh-mwan refresh
fi

refresh_status() {
	if command -v campus-wan >/dev/null 2>&1; then
		campus-wan refresh
	elif [ -x /usr/sbin/campus-mwan-update ]; then
		/usr/sbin/campus-mwan-update
	else
		echo "No supported multi-WAN refresh command was found." >&2
		return 127
	fi
}

refresh_status >/dev/null || exit $?
STATUS=""
for CANDIDATE in /tmp/hitwh-mwan.status /tmp/campus-mwan.status; do
	[ -r "$CANDIDATE" ] && { STATUS="$CANDIDATE"; break; }
done
[ -n "$STATUS" ] || { echo "Multi-WAN status file is unavailable." >&2; exit 1; }

OFFLINE=""
for IFACE in wan $(
	N=2
	while [ "$N" -le 17 ]; do
		if [ "$N" -ne 6 ] && uci -q get "network.wan$N" >/dev/null; then
			echo "wan$N"
		fi
		N=$((N + 1))
	done
); do
	grep -q "^$IFACE .* state=active" "$STATUS" || OFFLINE="$OFFLINE $IFACE"
done

set -- $OFFLINE
ATTEMPTED=$#
if [ "$ATTEMPTED" -eq 0 ]; then
	echo "RESULT 0 0 0"
	exit 0
fi

for IFACE in "$@"; do ifdown "$IFACE" >/dev/null 2>&1 || true; done
sleep 1
for IFACE in "$@"; do ifup "$IFACE" >/dev/null 2>&1 || true; done

WAIT=0
while [ "$WAIT" -lt 20 ]; do
	PENDING=0
	for IFACE in "$@"; do
		IP="$(ubus call "network.interface.$IFACE" status 2>/dev/null |
			jsonfilter -e '@["ipv4-address"][0].address' 2>/dev/null)"
		[ -n "$IP" ] || PENDING=$((PENDING + 1))
	done
	[ "$PENDING" -eq 0 ] && break
	sleep 1
	WAIT=$((WAIT + 1))
done

refresh_status >/dev/null || exit $?
REMAINING=0
for IFACE in "$@"; do
	grep -q "^$IFACE .* state=active" "$STATUS" || REMAINING=$((REMAINING + 1))
done
RECOVERED=$((ATTEMPTED - REMAINING))
echo "RESULT $ATTEMPTED $RECOVERED $REMAINING"
[ "$REMAINING" -eq 0 ]
'''

REMOTE_SCRIPT = r'''#!/bin/sh
INTERVAL="$1"
[ -n "$INTERVAL" ] || INTERVAL=2
PARENT="$(uci -q get hitwh_mwan.main.parent_device 2>/dev/null)"
[ -n "$PARENT" ] || PARENT=eth1

while :; do
	STATUS=""
	for CANDIDATE in /tmp/hitwh-mwan.status /tmp/campus-mwan.status; do
		if [ -r "$CANDIDATE" ]; then STATUS="$CANDIDATE"; break; fi
	done
	printf '@@BEGIN\t%s\n' "$(date +%s)"
	awk '/^cpu / { printf "CPU"; for (i=2; i<=NF; i++) printf "\t%s", $i; printf "\n"; exit }' /proc/stat
	awk '{ printf "LOAD\t%s\t%s\t%s\n", $1, $2, $3 }' /proc/loadavg
	awk '
		/^MemTotal:/ { total=$2 }
		/^MemAvailable:/ { available=$2 }
		END { printf "MEM\t%d\t%d\n", total, available }
	' /proc/meminfo
	COUNT="$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null)"
	MAX="$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null)"
	printf 'CONN\t%s\t%s\n' "${COUNT:-0}" "${MAX:-0}"
	# Keep the fractional, monotonic uptime for accurate rate calculations.
	# The wall-clock timestamp above is intentionally only used for labels.
	awk '{ printf "UPTIME\t%s\n", $1 }' /proc/uptime

	if [ -n "$STATUS" ]; then
		while IFS= read -r LINE; do
			case "$LINE" in
				wan\ *|wan[0-9]*\ *)
					printf 'PATH\t%s\n' "$LINE"
					IFACE="${LINE%% *}"
					SAVED=0
					[ ! -L "/etc/hitwh-mwan/auth.d/$IFACE.json" ] && [ -f "/etc/hitwh-mwan/auth.d/$IFACE.json" ] && SAVED=1
					printf 'AUTHCFG\t%s\t%s\n' "$IFACE" "$SAVED" ;;
			esac
		done <"$STATUS"

		for IFACE in $(awk '/^wan([0-9]*)? / { print $1 }' "$STATUS"); do
			if [ "$IFACE" = wan ]; then
				DEV="$PARENT"
			else
				DEV="mac$IFACE"
			fi
			RX="$(cat "/sys/class/net/$DEV/statistics/rx_bytes" 2>/dev/null)"
			TX="$(cat "/sys/class/net/$DEV/statistics/tx_bytes" 2>/dev/null)"
			printf 'DEV\t%s\t%s\t%s\t%s\n' "$IFACE" "$DEV" "${RX:-0}" "${TX:-0}"
		done
	fi
	printf '@@END\n'
	sleep "$INTERVAL"
done
'''


def parse_frame(lines: Iterable[str]) -> dict[str, Any]:
    """Parse one tab-separated frame emitted by REMOTE_SCRIPT."""
    result: dict[str, Any] = {
        "timestamp": 0,
        "cpu": [],
        "load": [0.0, 0.0, 0.0],
        "memory": {"total_kib": 0, "available_kib": 0},
        "conntrack": {"count": 0, "max": 0},
        "uptime": 0,
        "sample_clock": 0.0,
        "paths": {},
        "devices": {},
    }
    for raw in lines:
        line = raw.rstrip("\n")
        parts = line.split("\t")
        if not parts:
            continue
        kind = parts[0]
        try:
            if kind == "@@BEGIN" and len(parts) >= 2:
                result["timestamp"] = int(parts[1])
            elif kind == "CPU":
                result["cpu"] = [int(value) for value in parts[1:]]
            elif kind == "LOAD" and len(parts) >= 4:
                result["load"] = [float(value) for value in parts[1:4]]
            elif kind == "MEM" and len(parts) >= 3:
                result["memory"] = {
                    "total_kib": int(parts[1]),
                    "available_kib": int(parts[2]),
                }
            elif kind == "CONN" and len(parts) >= 3:
                result["conntrack"] = {"count": int(parts[1]), "max": int(parts[2])}
            elif kind == "UPTIME" and len(parts) >= 2:
                result["sample_clock"] = float(parts[1])
                result["uptime"] = int(result["sample_clock"])
            elif kind == "PATH" and len(parts) >= 2:
                fields = parts[1].split()
                iface = fields[0]
                metadata = {"interface": iface}
                for token in fields[1:]:
                    if "=" in token:
                        key, value = token.split("=", 1)
                        metadata[key] = value
                result["paths"][iface] = metadata
            elif kind == "DEV" and len(parts) >= 5:
                result["devices"][parts[1]] = {
                    "device": parts[2],
                    "rx_bytes": int(parts[3]),
                    "tx_bytes": int(parts[4]),
                }
            elif kind == "AUTHCFG" and len(parts) >= 3:
                result["paths"].setdefault(parts[1], {"interface": parts[1]})["credentials_saved"] = parts[2] == "1"
        except (TypeError, ValueError):
            continue
    return result


def _counter_delta(current: int, previous: int) -> int:
    return max(0, current - previous) if current >= previous else 0


def _cpu_percent(current: list[int], previous: list[int]) -> float:
    if not current or not previous or len(current) != len(previous):
        return 0.0
    deltas = [_counter_delta(now, old) for now, old in zip(current, previous)]
    total = sum(deltas)
    if total <= 0:
        return 0.0
    idle = deltas[3] if len(deltas) > 3 else 0
    iowait = deltas[4] if len(deltas) > 4 else 0
    return round(100.0 * (total - idle - iowait) / total, 1)


@dataclass
class RateCalculator:
    previous: dict[str, Any] | None = None
    totals: dict[str, dict[str, int]] = field(default_factory=dict)

    def transform(self, raw: dict[str, Any]) -> dict[str, Any]:
        timestamp = int(raw.get("timestamp") or time.time())
        previous = self.previous
        sample_clock = float(raw.get("sample_clock") or timestamp)
        if previous:
            previous_clock = float(previous.get("sample_clock") or previous.get("timestamp", timestamp))
            dt = max(0.25, sample_clock - previous_clock)
        else:
            dt = 0.0
        previous_devices = previous.get("devices", {}) if previous else {}

        deltas: dict[str, dict[str, int]] = {}
        for iface, current in raw.get("devices", {}).items():
            old = previous_devices.get(iface, current)
            deltas[iface] = {
                "rx": _counter_delta(current["rx_bytes"], old.get("rx_bytes", current["rx_bytes"])),
                "tx": _counter_delta(current["tx_bytes"], old.get("tx_bytes", current["tx_bytes"])),
            }

        main_iface = "wan" if "wan" in deltas else next(iter(deltas), "")
        virtual_ifaces = [iface for iface in deltas if iface != main_iface]
        physical = deltas.get(main_iface, {"rx": 0, "tx": 0})
        virtual_rx = sum(deltas[name]["rx"] for name in virtual_ifaces)
        virtual_tx = sum(deltas[name]["tx"] for name in virtual_ifaces)

        parent_includes_rx = physical["rx"] >= virtual_rx
        parent_includes_tx = physical["tx"] >= virtual_tx
        main_rx = max(0, physical["rx"] - virtual_rx) if parent_includes_rx else physical["rx"]
        main_tx = max(0, physical["tx"] - virtual_tx) if parent_includes_tx else physical["tx"]
        total_rx = physical["rx"] if parent_includes_rx else physical["rx"] + virtual_rx
        total_tx = physical["tx"] if parent_includes_tx else physical["tx"] + virtual_tx

        paths = []
        metadata = raw.get("paths", {})
        for iface in sorted(deltas, key=lambda value: (value != "wan", value)):
            delta = deltas[iface]
            if iface == main_iface:
                delta = {"rx": main_rx, "tx": main_tx}
            total = self.totals.setdefault(iface, {"rx": 0, "tx": 0})
            total["rx"] += delta["rx"]
            total["tx"] += delta["tx"]
            info = metadata.get(iface, {"interface": iface, "state": "unknown"})
            paths.append(
                {
                    "interface": iface,
                    "device": raw["devices"][iface]["device"],
                    "mac": info.get("mac", "—"),
                    "ip": info.get("ip", "—"),
                    "state": info.get("state", "unknown"),
                    "mark": info.get("mark", "—"),
                    "credentials_saved": bool(info.get("credentials_saved", False)),
                    "rx_bytes_per_second": round(delta["rx"] / dt, 1) if dt else 0.0,
                    "tx_bytes_per_second": round(delta["tx"] / dt, 1) if dt else 0.0,
                    "rx_bytes_since_view": total["rx"],
                    "tx_bytes_since_view": total["tx"],
                }
            )

        memory = raw.get("memory", {})
        memory_total = int(memory.get("total_kib", 0))
        memory_used_percent = (
            100.0 * (memory_total - int(memory.get("available_kib", 0))) / memory_total
            if memory_total
            else 0.0
        )
        conntrack = raw.get("conntrack", {})
        conntrack_max = int(conntrack.get("max", 0))
        conntrack_count = int(conntrack.get("count", 0))

        sample = {
            "timestamp": timestamp,
            "total": {
                "rx_bytes_per_second": round(total_rx / dt, 1) if dt else 0.0,
                "tx_bytes_per_second": round(total_tx / dt, 1) if dt else 0.0,
            },
            "cpu_percent": _cpu_percent(raw.get("cpu", []), previous.get("cpu", []) if previous else []),
            "load": raw.get("load", [0.0, 0.0, 0.0]),
            "memory_used_percent": round(memory_used_percent, 1),
            "conntrack": {
                "count": conntrack_count,
                "max": conntrack_max,
                "used_percent": round(100.0 * conntrack_count / conntrack_max, 1) if conntrack_max else 0.0,
            },
            "uptime_seconds": raw.get("uptime", 0),
            "paths": paths,
        }
        self.previous = raw
        return sample


class RouterCollector:
    def __init__(self, target: str, interval: float, identity: str | None = None, idle_timeout: float = 10.0):
        self.target = target
        self.interval = interval
        self.identity = identity
        self.idle_timeout = idle_timeout
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.process: subprocess.Popen[str] | None = None
        self.last_viewer = 0.0
        self.state = "idle"
        self.error = ""
        self.latest: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=180)
        self.calculator = RateCalculator()
        self.reconnect_lock = threading.Lock()

    def touch(self) -> None:
        with self.lock:
            self.last_viewer = time.monotonic()
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="router-collector", daemon=True)
                self.thread.start()

    def stop(self) -> None:
        with self.lock:
            process = self.process
            self.last_viewer = 0.0
        if process and process.poll() is None:
            process.terminate()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "error": self.error,
                "target": self.target,
                "interval_seconds": self.interval,
                "sample": self.latest,
                "history": list(self.history),
            }

    def _ssh_base_command(self) -> list[str]:
        command = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=6",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.identity:
            command.extend(["-i", self.identity])
        return command

    def _ssh_command(self) -> list[str]:
        return [*self._ssh_base_command(), self.target, f"sh -s -- {self.interval:g}"]

    def reconnect_paths(self, interface: str = "") -> tuple[bool, str]:
        if interface:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", interface):
                return False, "无效线路"
            result = self.router_action({"action": "refresh", "interface": interface})
            return bool(result.get("ok")), result.get("message", "刷新完成")
        if not self.reconnect_lock.acquire(blocking=False):
            return False, "线路刷新正在进行"
        try:
            result = subprocess.run(
                [*self._ssh_base_command(), self.target, "sh -s"],
                input=RECONNECT_SCRIPT.encode("utf-8"),
                capture_output=True,
                timeout=REFRESH_TIMEOUT,
            )
            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")
            result_line = next(
                (line for line in stdout.splitlines() if line.startswith("RESULT ")),
                "",
            )
            fields = result_line.split()
            if len(fields) == 4 and all(value.isdigit() for value in fields[1:]):
                attempted, recovered, remaining = (int(value) for value in fields[1:])
                if attempted == 0:
                    self.touch()
                    return True, "全部线路在线，无需重连"
                if remaining == 0 and result.returncode == 0:
                    self.touch()
                    return True, f"已恢复 {recovered} 条线路"
                self.touch()
                reasons = []
                for line in stdout.splitlines():
                    parts = line.split()
                    if len(parts) == 3 and parts[0] == "AUTH_RESULT" and parts[2] in AUTH_MESSAGES:
                        if re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", parts[1]):
                            reasons.append(f"{parts[1]}：{AUTH_MESSAGES[parts[2]]}")
                detail = "；".join(reasons) or "其余线路可能需要重新认证"
                return False, f"已恢复 {recovered}/{attempted} 条；{detail}"
            if result.returncode == 0:
                self.touch()
                return True, "线路刷新完成"
            return False, f"刷新失败（远程状态 {result.returncode}）"
        except subprocess.TimeoutExpired:
            return False, "线路刷新超时"
        except OSError as exc:
            return False, str(exc)
        finally:
            self.reconnect_lock.release()

    def native_rpc(self, method: str, values: dict[str, Any]) -> dict[str, Any]:
        if method not in {"snapshot", "settings", "start", "job"}:
            return {"ok": False, "message": "无效操作"}
        code = ("import { stdin } from 'fs'; import { connect } from 'ubus'; "
                f"print(sprintf('%J',connect().call('hitwh.mwan','{method}',json(stdin.read('all'))))); ")
        try:
            result = subprocess.run([*self._ssh_base_command(), self.target, "ucode -e " + shlex.quote(code)],
                input=json.dumps(values, ensure_ascii=False).encode(), capture_output=True, timeout=20)
            if result.returncode:
                return {"ok": False, "message": "请在路由器安装新版 IPK 并检查 SSH 连接"}
            data = json.loads(result.stdout)
            return data if isinstance(data, dict) else {"ok": False, "message": "路由器响应无效"}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {"ok": False, "message": "路由器接口不可用，请检查组件版本和连接"}

    def router_action(self, values: dict[str, Any]) -> dict[str, Any]:
        result = self.native_rpc("start", values)
        if not result.get("ok"):
            return result
        job_id = result["job"]
        deadline = time.monotonic() + 320
        while time.monotonic() < deadline:
            time.sleep(0.7)
            result = self.native_rpc("job", {"id": job_id})
            if result.get("state") == "done" or not result.get("ok"):
                break
        else:
            return {"ok": False, "message": "操作超时，请检查线路状态"}
        self.touch()
        details = [f"{d['interface']}：{AUTH_MESSAGES[d['code']]}" for d in result.get("details", [])
                   if d.get("code") in AUTH_MESSAGES and re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", d.get("interface", ""))]
        if details: result["message"] = "；".join(details)
        return result

    def credentials(self, action: str, interface: str, values: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Only explicit dialog actions read secrets; no collector state caches them."""
        if action not in {"get", "save", "remove"} or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", interface):
            return False, {"message": "无效的凭据操作或接口"}
        payload = None
        if action == "save":
            username, password = values.get("username"), values.get("password")
            if (not isinstance(username, str) or not isinstance(password, str) or not username or not password
                    or len(username.encode("utf-8")) > 1024 or len(password.encode("utf-8")) > 2048
                    or any(ord(char) < 32 or ord(char) == 127 for char in username + password)):
                return False, {"message": "请输入有效账号和密码"}
            payload = json.dumps({"username": username, "password": password}, ensure_ascii=False).encode("utf-8")
        command_action = "import" if action == "save" else action
        # interface is a validated identifier; credentials travel over stdin.
        command = [*self._ssh_base_command(), self.target, f"hitwh-mwan auth {command_action} {interface}"]
        try:
            result = subprocess.run(command, input=payload, capture_output=True, timeout=20)
            if result.returncode:
                parts = result.stdout.decode("utf-8", errors="replace").strip().split()
                reason = parts[2] if len(parts) == 3 and parts[0] == "AUTH_RESULT" else "helper_unavailable"
                return False, {"message": AUTH_MESSAGES.get(reason, "凭据操作失败")}
            if action == "get":
                data = json.loads(result.stdout)
                username, password = data.get("username"), data.get("password")
                if isinstance(username, str) and isinstance(password, str):
                    return True, {"configured": True, "username": username, "password": password, "mac": data.get("mac", "")}
                if data.get("configured") is False:
                    return True, {"configured": False, "username": "", "password": ""}
                return False, {"message": "凭据文件格式无效"}
            return True, {"configured": action == "save", "message": "凭据已保存" if action == "save" else "凭据已删除"}
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
            # Exceptions and remote stderr may include payloads: never echo them.
            return False, {"message": "无法访问路由器凭据，请检查 SSH 连接及组件版本"}

    def _run(self) -> None:
        while True:
            with self.lock:
                if time.monotonic() - self.last_viewer > self.idle_timeout:
                    self.state = "idle"
                    return
                self.state = "connecting"
                self.error = ""
            frame: list[str] = []
            try:
                process = subprocess.Popen(
                    self._ssh_command(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                with self.lock:
                    self.process = process
                assert process.stdin is not None
                assert process.stdout is not None
                # Keep POSIX shell scripts as LF; Windows text pipes default to CRLF.
                process.stdin.reconfigure(encoding="utf-8", newline="\n")
                process.stdin.write(REMOTE_SCRIPT)
                process.stdin.close()

                for line in process.stdout:
                    if line.startswith("@@BEGIN"):
                        frame = [line]
                    elif line.startswith("@@END"):
                        if frame:
                            raw = parse_frame(frame)
                            sample = self.calculator.transform(raw)
                            point = {
                                "timestamp": sample["timestamp"],
                                "rx_bytes_per_second": sample["total"]["rx_bytes_per_second"],
                                "tx_bytes_per_second": sample["total"]["tx_bytes_per_second"],
                                "paths": {
                                    item["interface"]: item["rx_bytes_per_second"] for item in sample["paths"]
                                },
                            }
                            with self.lock:
                                self.latest = sample
                                self.history.append(point)
                                self.state = "connected"
                                self.error = ""
                        frame = []
                        with self.lock:
                            idle = time.monotonic() - self.last_viewer > self.idle_timeout
                        if idle:
                            process.terminate()
                            break
                    elif frame:
                        frame.append(line)

                return_code = process.wait(timeout=3)
                if return_code and time.monotonic() - self.last_viewer <= self.idle_timeout:
                    error = process.stderr.read().strip() if process.stderr else ""
                    raise RuntimeError(error or f"SSH exited with status {return_code}")
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                with self.lock:
                    self.state = "disconnected"
                    self.error = str(exc)
            finally:
                with self.lock:
                    if self.process and self.process.poll() is None:
                        self.process.kill()
                    self.process = None

            with self.lock:
                if time.monotonic() - self.last_viewer > self.idle_timeout:
                    self.state = "idle"
                    return
            time.sleep(2)


class DashboardHandler(SimpleHTTPRequestHandler):
    collector: RouterCollector

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        if not self._local_request():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.path == "/api/snapshot":
            native = self.collector.native_rpc("snapshot", {})
            if "raw" in native:
                self._json_response(native, HTTPStatus.OK)
                return
            self.collector.touch()
            body = json.dumps(self.collector.snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/health":
            body = b'{"ok":true}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/config":
            result = self.collector.native_rpc("settings", {})
            self._json_response(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._local_request():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.path in {"/api/paths", "/api/config"}:
            if self.headers.get("X-Dashboard-Action") != "manage-paths" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192: raise ValueError
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict): raise ValueError
            except (ValueError, UnicodeDecodeError):
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid request")
                return
            if self.path == "/api/config": data["action"] = "configure"
            result = self.collector.router_action(data)
            self._json_response(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT)
            return
        if self.path == "/api/credentials":
            if (self.headers.get("X-Dashboard-Action") != "credentials"
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not isinstance(data.get("interface"), str) or not isinstance(data.get("action"), str):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid request")
                return
            ok, result = self.collector.credentials(data["action"], data["interface"], data)
            self._json_response({"ok": ok, **result}, HTTPStatus.OK if ok else HTTPStatus.CONFLICT)
            return
        if self.path == "/api/pause":
            self.collector.stop()
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/api/reconnect":
            if self.headers.get("X-Dashboard-Action") != "reconnect-paths":
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 8192 or length < 0: raise ValueError
                data = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(data, dict) or not isinstance(data.get("interface", ""), str): raise ValueError
            except (ValueError, UnicodeDecodeError):
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid request")
                return
            ok, message = self.collector.reconnect_paths(data.get("interface", ""))
            body = json.dumps({"ok": ok, "message": message}, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK if ok else HTTPStatus.CONFLICT)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/"):
            return
        super().log_message(fmt, *args)

    def _local_request(self) -> bool:
        port = self.server.server_port
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update({"127.0.0.1", "localhost"})
        # Reject DNS rebinding as well as cross-origin credential requests.
        return host in allowed and self.headers.get("Origin", f"http://{host}") == f"http://{host}"

    def _json_response(self, value: dict[str, Any], status: HTTPStatus) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'self'")
        super().end_headers()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HITwh 多路负载本地监控")
    parser.add_argument("--router", default=os.getenv("HITWH_ROUTER", "root@192.168.100.1"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--identity", help="SSH private key path")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.interval < 1:
        raise SystemExit("--interval must be at least 1 second")
    collector = RouterCollector(args.router, args.interval, args.identity)
    DashboardHandler.collector = collector
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    url = f"http://127.0.0.1:{args.port}"

    def shutdown(*_: Any) -> None:
        collector.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"HITwh 多路负载监控已启动：{url}")
    print("按 Ctrl+C 停止。页面隐藏约 10 秒后，路由器采样会自动暂停。")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    finally:
        collector.stop()
        server.server_close()


if __name__ == "__main__":
    main()
