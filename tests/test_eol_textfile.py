import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FIX = HERE / "tests" / "fixtures" / "eol"
SCRIPT = HERE / "textfile" / "eol-textfile.py"
PROMTOOL = shutil.which("promtool")


def load():
    spec = importlib.util.spec_from_file_location("eol_textfile", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eol = load()


def run(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


class Assets(unittest.TestCase):
    def test_inventory_parses_and_every_row_is_accounted_for(self):
        rows = eol.read_assets(HERE / "eol" / "assets.tsv")
        self.assertGreater(len(rows), 10)
        for r in rows:
            self.assertIn(r["basis"], ("vendor", "estimate", "licence", "unknown"))
            if r["basis"] == "unknown":
                self.assertEqual(r["date"], "?", f"{r['asset']} {r['component']}: unknown rows carry ?")
            if r["basis"] in ("vendor", "estimate"):
                self.assertIsNotNone(eol.date_ts(r["date"]), f"{r['asset']} {r['component']}: dated row needs a date")
                self.assertTrue(r["source"].startswith("http"), f"{r['asset']} {r['component']}: a dated row needs a source URL")

    def test_bad_rows_are_rejected(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "a.tsv")
            with open(p, "w") as f:
                f.write("asset\tcomponent\tkind\tversion\tmilestone\tdate\tbasis\teol_ref\tsource\tnote\n")
                f.write("x\ty\tos\t1\tend_of_support\t2026-13-01\tvendor\t-\thttp://a\t\n")
            rc, _, err = run("--assets", p)
            self.assertEqual(rc, 1)
            self.assertIn("not YYYY-MM-DD", err)
            with open(p, "w") as f:
                f.write("asset\tcomponent\tkind\tversion\tmilestone\tdate\tbasis\teol_ref\tsource\tnote\n")
                f.write("x\ty\tos\t1\tend_of_support\t?\tguess\t-\thttp://a\t\n")
            rc, _, err = run("--assets", p)
            self.assertEqual(rc, 1)
            self.assertIn("basis must be", err)
        finally:
            shutil.rmtree(tmp)


class Textfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # a copy of the fixtures so mtime (cache age) is fresh and nothing writes into the repo
        self.cache = os.path.join(self.tmp, "cache")
        shutil.copytree(FIX, self.cache)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_dates_unknowns_mismatch_and_licences(self):
        out = os.path.join(self.tmp, "eol.prom")
        rc, _, err = run("--offline", "--cache", self.cache, "--licence-file", str(FIX / "fortigate-license-status.json"), "--out", out)
        self.assertEqual(rc, 0, err)
        with open(out) as f:
            text = f.read()
        # a dated row: FortiOS 7.6 end of support 2030-01-25 00:00 UTC
        self.assertIn('asset_eol_timestamp_seconds{asset="fgt-edge",component="FortiOS",kind="firmware",version="7.6.7",milestone="end_of_support",basis="vendor"} 1895529600', text)
        # a ? row is an unknown, never a timestamp
        self.assertIn('asset_eol_unknown{asset="fgt-edge",component="FortiGate 60F",kind="hardware",version="60F",milestone="end_of_order",basis="unknown"} 1', text)
        self.assertNotIn('asset_eol_timestamp_seconds{asset="fgt-edge",component="FortiGate 60F"', text)
        # the TSV agrees with the fixture copy of endoflife.date (the qdevice rows
        # follow the live TSV: 13 since CR-2026-0001 on 2026-10-10)
        self.assertIn('asset_eol_source_mismatch{asset="qdevice",component="Debian",kind="os",version="13",milestone="security_support_end",basis="vendor"} 0', text)
        for line in text.splitlines():
            if line.startswith("asset_eol_source_mismatch{"):
                self.assertTrue(line.endswith(" 0"), line)
        # Debian's fields: security support is eoasFrom, LTS is eolFrom (both 0 above)
        self.assertIn('asset_eol_source_mismatch{asset="qdevice",component="Debian",kind="os",version="13",milestone="lts_end",basis="vendor"} 0', text)
        # Proxmox VE 9 has no date on endoflife.date yet: no comparison, no false 1
        self.assertNotIn('asset_eol_source_mismatch{asset="pve1",component="Proxmox VE"', text)
        # licences walked out of the 60F's real answer (2026-10-09, serial and
        # addresses masked): 20 expired FortiGuard services, all 2025-09-20
        self.assertIn('asset_eol_timestamp_seconds{asset="fgt-edge",component="web_filtering",kind="licence",version="-",milestone="expires",basis="licence"} 1758326400', text)
        self.assertIn('asset_licence_status{asset="fgt-edge",component="web_filtering",status="expired"} 1', text)
        lic = [l for l in text.splitlines() if l.startswith("asset_eol_timestamp_seconds{") and 'kind="licence"' in l]
        self.assertEqual(len(lic), 20)
        # the free sandbox tier and the definitions nested in iot_detection are not licences of their own
        self.assertNotIn("forticloud_sandbox", text)
        self.assertNotIn("iot_detection.definitions", text)
        # the licence placeholder row in the TSV is neither a timestamp nor an unknown
        self.assertNotIn('component="FortiGuard licences"', text)
        self.assertIn("eol_textfile_last_run_timestamp ", text)
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o644")
        if PROMTOOL:
            r = subprocess.run([PROMTOOL, "check", "metrics"], input=text, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_mismatch_is_reported_when_upstream_moves(self):
        # pretend endoflife.date moved FortiOS 7.6 EOS by a year
        p = os.path.join(self.cache, "fortios.json")
        with open(p) as f:
            doc = json.load(f)
        for rel in doc["result"]["releases"]:
            if rel["name"] == "7.6":
                rel["eolFrom"] = "2031-01-25"
        with open(p, "w") as f:
            json.dump(doc, f)
        rc, text, err = run("--offline", "--cache", self.cache)
        self.assertEqual(rc, 0, err)
        self.assertIn('asset_eol_source_mismatch{asset="fgt-edge",component="FortiOS",kind="firmware",version="7.6.7",milestone="end_of_support",basis="vendor"} 1', text)
        self.assertIn('milestone="end_of_engineering",basis="vendor"} 0', text)
        self.assertIn("TSV 2030-01-25, endoflife.date 2031-01-25", text)

    def test_missing_cache_is_not_an_error_offline(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        rc, text, err = run("--offline", "--cache", empty)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("asset_eol_source_mismatch{", text)
        self.assertIn("asset_eol_timestamp_seconds{", text)

    def test_licence_walk(self):
        doc = {"results": {"a": {"status": "x", "expires": 5, "defs": {"expires": 5}}, "b": {"support": {"hw": {"expires": 7}}},
                           "c": {"expires": 0}, "d": [{"expires": 9}], "e": {"status": "free_license", "expires": 11},
                           "f": {"expires": 13, "defs": {"expires": 15}}}}
        got = sorted(eol.walk_licences(doc["results"]))
        self.assertEqual(got, [("a", "x", 5), ("b.support.hw", "", 7), ("d", "", 9), ("f", "", 13), ("f.defs", "", 15)])


if __name__ == "__main__":
    unittest.main()
