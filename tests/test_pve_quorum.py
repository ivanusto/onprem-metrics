import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_textfile import promtool_ok

HERE = Path(__file__).resolve().parent.parent
SCRIPT = HERE / "textfile" / "pve-quorum-textfile.sh"
FIXTURES = Path(__file__).resolve().parent


def run(args=(), stdin="", env=None):
    r = subprocess.run(["sh", str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    return r.returncode, r.stdout


def samples(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            k, v = line.rsplit(" ", 1)
            out[k] = v
    return out


class PveQuorum(unittest.TestCase):
    def test_real_output_all_votes(self):
        """pvecm status from the field cluster (PVE 9.2, two nodes plus QDevice)."""
        rc, out = run(stdin=(FIXTURES / "pvecm-status.txt").read_text())
        self.assertEqual(rc, 0)
        s = samples(out)
        self.assertEqual(s['pve_quorum_up{cluster="lab"}'], "1")
        self.assertEqual(s['pve_quorum_expected_votes{cluster="lab"}'], "3")
        self.assertEqual(s['pve_quorum_total_votes{cluster="lab"}'], "3")
        self.assertEqual(s['pve_quorum_quorum{cluster="lab"}'], "2")
        self.assertEqual(s['pve_quorum_quorate{cluster="lab"}'], "1")
        self.assertEqual(s['pve_quorum_qdevice_votes{cluster="lab"}'], "1")
        self.assertEqual(s['pve_quorum_node_votes{cluster="lab",node="192.168.2.9"}'], "1")
        self.assertEqual(s['pve_quorum_node_votes{cluster="lab",node="192.168.2.5"}'], "1")
        ok, msg = promtool_ok(out)
        self.assertTrue(ok, msg)

    def test_qdevice_down(self):
        """corosync-qdevice stopped on both nodes: one vote short, still quorate."""
        rc, out = run(stdin=(FIXTURES / "pvecm-status-qdevice-down.txt").read_text())
        s = samples(out)
        self.assertEqual(s['pve_quorum_expected_votes{cluster="lab"}'], "3")
        self.assertEqual(s['pve_quorum_total_votes{cluster="lab"}'], "2")
        self.assertEqual(s['pve_quorum_quorate{cluster="lab"}'], "1")
        self.assertEqual(s['pve_quorum_qdevice_votes{cluster="lab"}'], "0")

    def test_empty_input_is_down(self):
        rc, out = run(stdin="")
        s = samples(out)
        self.assertEqual(s['pve_quorum_up{cluster="pve"}'], "0")
        self.assertNotIn('pve_quorum_total_votes{cluster="pve"}', s)
        self.assertIn("pve_quorum_textfile_last_run_timestamp", s)

    def test_falls_back_to_second_node(self):
        """A fake ssh that fails for the first node and answers for the second."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ssh"
            fake.write_text('#!/bin/sh\nfor a; do host=$a; break; done\n'
                            'while [ $# -gt 0 ]; do case $1 in root@*) host=$1;; esac; shift; done\n'
                            f'[ "$host" = root@n2 ] && cat {FIXTURES / "pvecm-status.txt"} || exit 255\n')
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            rc, out = run(["lab=root@n1,root@n2"], env={"SSH": str(fake)})
            s = samples(out)
            self.assertEqual(s['pve_quorum_up{cluster="lab"}'], "1")
            rc, out = run(["lab=root@n1"], env={"SSH": str(fake)})
            self.assertEqual(samples(out)['pve_quorum_up{cluster="lab"}'], "0")


if __name__ == "__main__":
    unittest.main()
