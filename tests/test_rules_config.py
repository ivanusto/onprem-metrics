import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def seconds(v):
    m = re.fullmatch(r"(\d+)([smh])", v)
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


class RulesConfig(unittest.TestCase):
    def test_gb10_group_evaluates_fast_enough(self):
        """The soak alert has 240 - 200 = 40 s before the guard acts. The
        textfile (10 s) and the scrape (15 s) use 25 of them, so the gb10
        group must evaluate every 15 s or less. promtool test rules steps all
        groups at the test file's interval and cannot catch this, and the
        global evaluation_interval in prometheus.yml is 60 s."""
        rules = (HERE / "prometheus" / "rules" / "thresholds.yml").read_text(encoding="utf-8")
        m = re.search(r"- name: gb10\n\s+interval: (\S+)", rules)
        self.assertIsNotNone(m, "gb10 group has no interval, it inherits the global 60 s")
        self.assertLessEqual(seconds(m.group(1)), 15)
        scrape = (HERE / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
        m = re.search(r"job_name: gb10\n(?:\s+#.*\n)*\s+scrape_interval: (\S+)", scrape)
        self.assertIsNotNone(m)
        self.assertLessEqual(seconds(m.group(1)), 15)


if __name__ == "__main__":
    unittest.main()
