#!/usr/bin/env python3
"""Local-only dashboard for the HITwh multi-WAN router.

The browser talks only to this process. While at least one dashboard tab is
visible, one persistent SSH session reads lightweight counters from OpenWrt.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
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
	awk '{ printf "UPTIME\t%d\n", $1 }' /proc/uptime

	if [ -n "$STATUS" ]; then
		while IFS= read -r LINE; do
			case "$LINE" in
				wan\ *) printf 'PATH\t%s\n' "$LINE" ;;
				wan[0-9]*\ *) printf 'PATH\t%s\n' "$LINE" ;;
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
                result["uptime"] = int(float(parts[1]))
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
        dt = max(0.25, timestamp - int(previous.get("timestamp", timestamp))) if previous else 0.0
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

    def _ssh_command(self) -> list[str]:
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
        command.extend([self.target, f"sh -s -- {self.interval:g}"])
        return command

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
        if self.path == "/api/snapshot":
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
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/pause":
            self.collector.stop()
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/"):
            return
        super().log_message(fmt, *args)


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
