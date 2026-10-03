import importlib.util
import pathlib
import sys
import unittest


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
UPTIME\t1000
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
UPTIME\t1002
PATH\twan mac=aa:aa:aa:aa:aa:aa ip=10.0.0.1 state=active mark=0x101
PATH\twan2 mac=02:00:00:00:00:02 ip=10.0.0.2 state=active mark=0x102
DEV\twan\teth1\t1400000\t620000
DEV\twan2\tmacwan2\t450000\t140000
""".splitlines()


class DashboardTests(unittest.TestCase):
    def test_parse_frame(self):
        parsed = SERVER.parse_frame(FRAME_ONE)
        self.assertEqual(parsed["timestamp"], 100)
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


if __name__ == "__main__":
    unittest.main()
