import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / "textfile" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gb10 = load("gb10-textfile")
drills = load("drills-textfile")
PROMTOOL = shutil.which("promtool")


def promtool_ok(text):
    """promtool check metrics reads the exposition format from stdin."""
    if not PROMTOOL:
        return True, "promtool not installed, skipped"
    r = subprocess.run([PROMTOOL, "check", "metrics"], input=text, capture_output=True, text=True)
    return r.returncode == 0, r.stdout + r.stderr


class FakeBox:
    """A fake DGX Spark: sysfs zones, nvidia-smi, meminfo, dmesg."""

    def __init__(self, tmp, zones=(70, 85), gpu="78, 55.2, 91", nv_errs=0):
        self.tmp = tmp
        self.sysfs = os.path.join(tmp, "thermal")
        for i, z in enumerate(zones):
            d = os.path.join(self.sysfs, f"thermal_zone{i}")
            os.makedirs(d)
            with open(os.path.join(d, "temp"), "w") as fh:
                fh.write(f"{z * 1000}\n")
        self.meminfo = os.path.join(tmp, "meminfo")
        with open(self.meminfo, "w") as fh:
            fh.write("MemTotal:       131072000 kB\nMemAvailable:    8388608 kB\n")
        self.smi = os.path.join(tmp, "nvidia-smi")
        with open(self.smi, "w") as fh:
            fh.write(f"#!/bin/sh\necho '{gpu}'\n")
        os.chmod(self.smi, 0o755)
        self.dmesg = os.path.join(tmp, "dmesg")
        with open(self.dmesg, "w") as fh:
            fh.write("#!/bin/sh\n")
            for _ in range(nv_errs):
                fh.write("echo '[123.4] NVRM: NV_ERR_NO_MEMORY'\n")
            fh.write("echo '[124.0] ok'\n")
        os.chmod(self.dmesg, 0o755)

    def set_zone(self, i, c):
        with open(os.path.join(self.sysfs, f"thermal_zone{i}", "temp"), "w") as fh:
            fh.write(f"{c * 1000}\n")

    def collect(self, now, state, **kw):
        return gb10.collect(now, state, sysfs=self.sysfs, nvidia_smi=self.smi,
                            meminfo=self.meminfo, dmesg=self.dmesg,
                            hot_flag=os.path.join(self.tmp, "HOT"), **kw)


class Gb10(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.box = FakeBox(self.tmp, nv_errs=3)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_values(self):
        text, state = self.box.collect(1000.0, {})
        self.assertIn("gb10_zone_temp_celsius 85", text)
        self.assertIn("gb10_gpu_temp_celsius 78.0", text)
        self.assertIn("gb10_gpu_power_watts 55.2", text)
        self.assertIn("gb10_mem_available_bytes 8589934592", text)
        self.assertIn("gb10_nv_err_no_memory_total 3", text)
        self.assertIn("gb10_soak_seconds 0", text)
        self.assertIn("gb10_hot 0", text)
        self.assertIn("gb10_textfile_errors 0", text)
        self.assertIsNone(state["hot_since"])
        ok, msg = promtool_ok(text)
        self.assertTrue(ok, msg)

    def test_soak_accumulates_and_resets(self):
        self.box.set_zone(1, 89)
        _, s = self.box.collect(1000.0, {})
        self.assertEqual(s["hot_since"], 1000.0)
        for t in (1050.0, 1100.0, 1150.0, 1200.0, 1250.0):
            text, s = self.box.collect(t, s)
        self.assertIn("gb10_soak_seconds 250", text)
        self.box.set_zone(1, 80)
        text, s = self.box.collect(1260.0, s)
        self.assertIn("gb10_soak_seconds 0", text)
        self.assertIsNone(s["hot_since"])

    def test_soak_resets_after_gap(self):
        self.box.set_zone(1, 90)
        _, s = self.box.collect(1000.0, {})
        text, s = self.box.collect(1200.0, s, max_gap=60)  # timer was dead for 200 s
        self.assertIn("gb10_soak_seconds 0", text)
        self.assertEqual(s["hot_since"], 1200.0)

    def test_missing_sources_counted_not_fatal(self):
        text, _ = gb10.collect(1.0, {}, sysfs=os.path.join(self.tmp, "nope"), nvidia_smi="/nonexistent",
                               meminfo="/nonexistent", dmesg="/nonexistent", hot_flag="/nonexistent")
        self.assertIn("gb10_textfile_errors 4", text)
        self.assertNotIn("gb10_gpu_temp_celsius", text)
        ok, msg = promtool_ok(text)
        self.assertTrue(ok, msg)

    def test_hot_flag(self):
        open(os.path.join(self.tmp, "HOT"), "w").close()
        text, _ = self.box.collect(1.0, {})
        self.assertIn("gb10_hot 1", text)

    def test_cli_writes_atomically(self):
        out = os.path.join(self.tmp, "x", "gb10.prom")
        st = os.path.join(self.tmp, "x", "state.json")
        rc = gb10.main(["--out", out, "--state", st, "--hot-flag", "/nonexistent"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(out))
        self.assertTrue(os.path.exists(st))
        self.assertFalse([f for f in os.listdir(os.path.dirname(out)) if f.startswith(".prom.")])
        # node_exporter reads it as another user
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o644)


class Drills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, recs):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
        return p

    def test_three_formats(self):
        # one line of each, copied from the drills.jsonl the three tools wrote
        pve = self.write("pve.jsonl", [
            {"t0": "2026-09-30T15:47:06Z", "label": "d3-full", "vmid": "104", "ip": "192.168.2.49", "restore_point": "2026-09-30T15:34:02Z", "t_exists": 23, "t_running": 557, "t_ping": 568, "t_ssh": 568, "rto_s": 569, "rpo_s": "784", "result": "OK", "host": "spark1"},
            {"t0": "2026-09-30T15:08:44Z", "label": "d1-instant", "vmid": "vs", "restore_point": "2026-09-30T15:05:02Z", "t_exists": None, "t_running": None, "t_ping": 0, "t_ssh": 0, "rto_s": 0, "rpo_s": "", "result": "FAIL", "host": "x"},
        ])
        nas = self.write("nas.jsonl", [
            {"t0": "2026-10-01T14:22:55Z", "label": "3. 從次要 NAS 還原回主 NAS", "path": "/mnt/drill/canary", "kind": "frozen", "restore_point": "2026-10-01T14:14:01Z", "appear": "68", "canary": "99", "manifest": "99", "rto": "-", "rpo": "534", "payload": "ok", "size_mb": 55, "rate_mbs": "-", "result": "FAIL", "code": 4},
            {"t0": "2026-10-01T13:30:25Z", "label": "2. 整個共享資料夾回復到快照", "rto": "1198", "rpo": "1799", "rate_mbs": "17", "result": "OK", "code": 0},
        ])
        cloud = self.write("cloud.jsonl", [
            {"t0": "2026-10-01T23:43:59Z", "label": "2. 還原 Music", "mib": 4808, "seconds": 92, "rate": "52 MiB/s", "rpo": "-", "result": "OK"},
            {"t0": "2026-10-01T17:38:09Z", "label": "2. 還原 Music", "mib": 100, "seconds": 500, "rate": "1 MiB/s", "result": "FAIL"},
        ])
        text = drills.render([("pve", pve), ("nas", nas), ("cloud", cloud)], 1_800_000_000)
        # pve-backup-drill writes rto_s / rpo_s
        self.assertIn('drill_last_rto_seconds{source="pve",label="d3-full"} 569', text)
        self.assertIn('drill_last_rpo_seconds{source="pve",label="d3-full"} 784', text)
        self.assertIn('drill_last_result{source="pve",label="d1-instant"} 0', text)
        self.assertNotIn('drill_last_success_timestamp{source="pve",label="d1-instant"}', text)
        # nas-backup-drill writes "-" for what it could not measure
        self.assertIn('drill_last_result{source="nas",label="3. 從次要 NAS 還原回主 NAS"} 0', text)
        self.assertNotIn('drill_last_rto_seconds{source="nas",label="3. 從次要 NAS 還原回主 NAS"}', text)
        self.assertIn('drill_last_rpo_seconds{source="nas",label="3. 從次要 NAS 還原回主 NAS"} 534', text)
        self.assertIn('drill_last_rate_mib_per_second{source="nas",label="2. 整個共享資料夾回復到快照"} 17', text)
        # cloud: latest record wins, seconds maps to rto, rate parsed from "52 MiB/s"
        self.assertIn('drill_last_rto_seconds{source="cloud",label="2. 還原 Music"} 92', text)
        self.assertIn('drill_last_rate_mib_per_second{source="cloud",label="2. 還原 Music"} 52', text)
        self.assertIn('drill_count_total{source="cloud",label="2. 還原 Music"} 2', text)
        self.assertIn('drill_last_success_timestamp{source="cloud",label="2. 還原 Music"} %d' % drills.parse_t0("2026-10-01T23:43:59Z"), text)
        ok, msg = promtool_ok(text)
        self.assertTrue(ok, msg)

    def test_missing_file_and_bad_lines(self):
        bad = self.write("bad.jsonl", [{"t0": "garbage"}])
        with open(bad, "a") as fh:
            fh.write("not json\n")
        text = drills.render([("x", bad), ("y", os.path.join(self.tmp, "none.jsonl"))], 5)
        self.assertIn("drills_textfile_last_run_timestamp 5", text)
        self.assertNotIn("drill_last_timestamp{", text)

    def test_out_is_world_readable(self):
        out = os.path.join(self.tmp, "d", "drills.prom")
        drills.main(["--out", out, "x=" + os.path.join(self.tmp, "none.jsonl")])
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o644)

    def test_num(self):
        self.assertEqual(drills.num("118s"), 118.0)
        self.assertEqual(drills.num("47.7 MiB/s"), 47.7)
        self.assertIsNone(drills.num("-"))
        self.assertIsNone(drills.num("?"))


if __name__ == "__main__":
    unittest.main()
