import importlib.util
import http.client
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("dashboard_server", MODULE_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = SERVER
SPEC.loader.exec_module(SERVER)


FRAME_ONE = """@@BEGIN\t100
CPU\t100\t0\t30\t800\t10\t0\t5\t0
LOAD\t0.10\t0.20\t0.30
MEM\t512000\t384000
CONN\t100\t16384
UPTIME\t1000.25
PATH\twan mac=aa:aa:aa:aa:aa:aa ip=10.0.0.1 state=active mark=0x101
PATH\twan2 mac=02:00:00:00:00:02 ip=10.0.0.2 state=active mark=0x102
DEV\twan\teth1\t1000000\t500000
DEV\twan2\tmacwan2\t300000\t100000
""".splitlines()

FRAME_TWO = """@@BEGIN\t102
CPU\t120\t0\t40\t940\t10\t0\t10\t0
LOAD\t0.20\t0.20\t0.30
MEM\t512000\t358400
CONN\t120\t16384
UPTIME\t1002.25
PATH\twan mac=aa:aa:aa:aa:aa:aa ip=10.0.0.1 state=active mark=0x101
PATH\twan2 mac=02:00:00:00:00:02 ip=10.0.0.2 state=active mark=0x102
DEV\twan\teth1\t1400000\t620000
DEV\twan2\tmacwan2\t450000\t140000
""".splitlines()


class DashboardTests(unittest.TestCase):
    def test_credential_choices_are_explicit_metadata_requests_not_sampling(self):
        collector=SERVER.RouterCollector('root@router',2.0)
        response={'ok':True,'choices':[{'username':'fake-user','interface':'wan2','interfaces':['wan2','wan3']}]}
        with mock.patch.object(collector,'native_rpc',return_value=response) as rpc:
            ok,result=collector.credentials('choices','',{})
        self.assertTrue(ok); self.assertEqual(result,response)
        rpc.assert_called_once_with('credential_choices',{})
        self.assertNotIn('choices',collector.__dict__)

    def test_native_rpc_credentials_stay_on_stdin(self):
        collector=SERVER.RouterCollector('root@router',2.0)
        values={"action":"add-random","username":"fake-user","password":"fake-secret &账"}
        result=subprocess.CompletedProcess([],0,stdout=b'{"ok":true,"job":"ABC123"}',stderr=b'')
        with mock.patch.object(SERVER.subprocess,'run',return_value=result) as run:
            self.assertTrue(collector.native_rpc('start',values)['ok'])
        self.assertNotIn(values['password'],repr(run.call_args.args))
        self.assertEqual(json.loads(run.call_args.kwargs['input']),values)

    def test_native_job_polls_one_id_without_resubmitting_login(self):
        collector=SERVER.RouterCollector('root@router',2.0)
        with mock.patch.object(collector,'native_rpc',side_effect=[
            {'ok':True,'job':'ABC123'},{'ok':True,'state':'running'},{'ok':True,'state':'done','message':'完成'}]) as rpc, \
            mock.patch.object(SERVER.time,'sleep'),mock.patch.object(collector,'touch'):
            self.assertTrue(collector.router_action({'action':'refresh','interface':'wan3'})['ok'])
        self.assertEqual([call.args[0] for call in rpc.call_args_list],['start','job','job'])
        self.assertEqual(rpc.call_args_list[1].args[1],{'id':'ABC123'})
        self.assertEqual(rpc.call_args_list[2].args[1],{'id':'ABC123'})

    def test_credential_payload_uses_stdin_and_is_not_cached(self):
        collector = SERVER.RouterCollector("root@router", 2.0)
        values = {"username": "fake-user", "password": "fake-secret &账"}
        completed = subprocess.CompletedProcess([], 0, stdout=b"AUTH_RESULT wan saved\n", stderr=b"")
        with mock.patch.object(SERVER.subprocess, "run", return_value=completed) as run:
            ok, result = collector.credentials("save", "wan", values)
        self.assertTrue(ok)
        self.assertNotIn(values["password"], repr(run.call_args.args))
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), values)
        self.assertNotIn(values["password"], repr(collector.__dict__) + repr(result))

    def test_credentials_require_valid_identifiers_and_do_not_echo_errors(self):
        collector = SERVER.RouterCollector("root@router", 2.0)
        with mock.patch.object(SERVER.subprocess, "run") as run:
            self.assertFalse(collector.credentials("get", "wan;echo injected", {})[0])
            self.assertFalse(collector.credentials("save", "wan", {"username": "fake-user", "password": ""})[0])
            run.assert_not_called()
        completed = subprocess.CompletedProcess([], 1, stdout=b"fake-secret echoed", stderr=b"fake-secret")
        with mock.patch.object(SERVER.subprocess, "run", return_value=completed):
            self.assertNotIn("fake-secret", repr(collector.credentials("get", "wan", {})))

    def test_saved_metadata_never_includes_credentials(self):
        sample = SERVER.RateCalculator().transform(SERVER.parse_frame([*FRAME_ONE, "AUTHCFG\twan\t1"]))
        self.assertTrue(sample["paths"][0]["credentials_saved"])
        self.assertNotIn("password", json.dumps(sample))

    def test_credential_http_requires_explicit_same_origin_post(self):
        collector = mock.Mock()
        collector.credentials.return_value = (True, {"configured": False})
        handler = type("TestHandler", (SERVER.DashboardHandler,), {"collector": collector})
        httpd = SERVER.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        host = f"127.0.0.1:{httpd.server_port}"
        body = json.dumps({"action": "get", "interface": "wan"})
        def request(method="POST", headers=None, payload=body):
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            connection.request(method, "/api/credentials", payload, headers or {})
            response = connection.getresponse()
            status, cache, data = response.status, response.getheader("Cache-Control"), response.read()
            connection.close()
            return status, cache, data
        headers = {"Content-Type": "application/json", "X-Dashboard-Action": "credentials"}
        self.assertEqual(request("GET")[0], 404)
        self.assertEqual(request(headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(request(headers={**headers, "Origin": "http://attacker.invalid"})[0], 403)
        self.assertEqual(request(headers={**headers, "Host": "attacker.invalid"})[0], 403)
        self.assertEqual(request(headers=headers, payload="[]")[0], 400)
        collector.credentials.assert_not_called()
        status, cache, data = request(headers={**headers, "Origin": f"http://{host}"})
        self.assertEqual((status, cache), (200, "no-store"))
        self.assertTrue(json.loads(data)["ok"])
        collector.credentials.assert_called_once()

    def test_launcher_prints_utf8_help_with_legacy_locale(self):
        if os.name == "nt":
            command = [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(MODULE_PATH.parent / "start.ps1"), "--help",
            ]
        else:
            command = ["sh", str(MODULE_PATH.parent / "start.command"), "--help"]
        env = dict(os.environ, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C")
        env.pop("PYTHONIOENCODING", None)
        # Cold PowerShell/Python startup can exceed 10s on hosted Windows runners.
        timeout = 60 if os.name == "nt" else 10
        result = subprocess.run(command, env=env, capture_output=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn(SERVER.build_parser().description, result.stdout.decode("utf-8"))

    def test_collector_sends_lf_script_and_reads_text_frames(self):
        collector = SERVER.RouterCollector("unused", 2.0)
        frame = "\n".join([*FRAME_ONE, "@@END", ""])
        with tempfile.TemporaryDirectory() as directory:
            payload_path = pathlib.Path(directory) / "script.sh"
            command = [
                sys.executable,
                "-c",
                "import pathlib, sys; "
                "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read()); "
                "sys.stdout.write(sys.argv[2])",
                str(payload_path),
                frame,
            ]
            real_popen = SERVER.subprocess.Popen
            with ExitStack() as processes:
                def spawn(*args, **kwargs):
                    return processes.enter_context(real_popen(*args, **kwargs))

                with (
                    mock.patch.object(collector, "_ssh_command", return_value=command),
                    mock.patch.object(SERVER.time, "monotonic", side_effect=[0.0, 0.0, 11.0]),
                    mock.patch.object(SERVER.subprocess, "Popen", side_effect=spawn),
                ):
                    collector._run()
            payload = payload_path.read_bytes()
        self.assertNotIn(b"\r", payload)
        self.assertEqual(payload, SERVER.REMOTE_SCRIPT.encode("utf-8"))
        self.assertEqual(collector.latest["timestamp"], 100)
        self.assertEqual(len(collector.latest["paths"]), 2)

    def test_parse_frame(self):
        parsed = SERVER.parse_frame(FRAME_ONE)
        self.assertEqual(parsed["timestamp"], 100)
        self.assertEqual(parsed["uptime"], 1000)
        self.assertEqual(parsed["sample_clock"], 1000.25)
        self.assertEqual(parsed["paths"]["wan2"]["state"], "active")
        self.assertEqual(parsed["devices"]["wan"]["rx_bytes"], 1_000_000)

    def test_parent_counter_is_split_into_main_and_macvlan(self):
        calculator = SERVER.RateCalculator()
        calculator.transform(SERVER.parse_frame(FRAME_ONE))
        sample = calculator.transform(SERVER.parse_frame(FRAME_TWO))
        paths = {item["interface"]: item for item in sample["paths"]}
        self.assertEqual(sample["total"]["rx_bytes_per_second"], 200_000)
        self.assertEqual(paths["wan2"]["rx_bytes_per_second"], 75_000)
        self.assertEqual(paths["wan"]["rx_bytes_per_second"], 125_000)
        self.assertAlmostEqual(sample["memory_used_percent"], 30.0)

    def test_rate_uses_monotonic_clock_when_wall_clock_rounding_skips_a_second(self):
        first = SERVER.parse_frame(FRAME_ONE)
        second = SERVER.parse_frame(FRAME_TWO)
        second["timestamp"] = 103

        calculator = SERVER.RateCalculator()
        calculator.transform(first)
        sample = calculator.transform(second)

        self.assertEqual(sample["timestamp"], 103)
        self.assertEqual(sample["total"]["rx_bytes_per_second"], 200_000)

    def test_reconnect_paths_runs_supported_remote_reconnect_script(self):
        collector = SERVER.RouterCollector("root@router", 2.0, identity="router-key")
        completed = subprocess.CompletedProcess([], 0, stdout=b"RESULT 2 2 0\n", stderr=b"")
        with (
            mock.patch.object(SERVER.subprocess, "run", return_value=completed) as run,
            mock.patch.object(collector, "touch") as touch,
        ):
            ok, message = collector.reconnect_paths()

        self.assertTrue(ok)
        self.assertEqual(message, "已恢复 2 条线路")
        run.assert_called_once_with(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
                "-o", "StrictHostKeyChecking=accept-new", "-i", "router-key",
                "root@router", "sh -s",
            ],
            input=SERVER.RECONNECT_SCRIPT.encode("utf-8"),
            capture_output=True,
            timeout=SERVER.REFRESH_TIMEOUT,
        )
        touch.assert_called_once_with()

    def test_reconnect_paths_reports_lines_that_still_need_authentication(self):
        collector = SERVER.RouterCollector("root@router", 2.0)
        completed = subprocess.CompletedProcess([], 1, stdout=b"RESULT 2 1 1\n", stderr=b"")
        with (
            mock.patch.object(SERVER.subprocess, "run", return_value=completed),
            mock.patch.object(collector, "touch") as touch,
        ):
            ok, message = collector.reconnect_paths()

        self.assertFalse(ok)
        self.assertEqual(message, "已恢复 1/2 条；其余线路可能需要重新认证")
        touch.assert_called_once_with()

    def test_reconnect_paths_skips_restart_when_all_lines_are_online(self):
        collector = SERVER.RouterCollector("root@router", 2.0)
        completed = subprocess.CompletedProcess([], 0, stdout=b"RESULT 0 0 0\n", stderr=b"")
        with (
            mock.patch.object(SERVER.subprocess, "run", return_value=completed),
            mock.patch.object(collector, "touch") as touch,
        ):
            ok, message = collector.reconnect_paths()

        self.assertTrue(ok)
        self.assertEqual(message, "全部线路在线，无需重连")
        touch.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
