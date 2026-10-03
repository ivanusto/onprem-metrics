#!/usr/bin/env python3
"""Turn the drills.jsonl files from Day 16, 17 and 18 into textfile metrics.

Each drill repo appends one JSON object per drill. The three formats differ
a little, so this script normalises them:

    field    pve-backup-drill   nas-backup-drill   cloud-offload-drill
    label    label              label              label
    t0       t0 (ISO UTC)       t0                 t0
    result   result OK/FAIL     result             result
    rto      rto_s (int)        rto ("69", "-")    seconds (int)
    rpo      rpo_s ("784")      rpo                rpo ("-")
    rate     -                  rate_mbs ("297")   rate ("58.2 MiB/s")

The JSONL is what the tool printed at the time. Corrections made by hand
later in each repo's drill-log.md do not flow back here.

For every (source, label) it exposes the most recent drill only. A
dashboard then shows "last successful restore N days ago" per drill and
alerts when that passes the policy (Day 16: monthly; Day 18: monthly).

    drills-textfile.py --out /var/lib/node_exporter/textfile/drills.prom \
        pve=/srv/drills/pve-backup-drill/drills.jsonl \
        nas=/srv/drills/nas-backup-drill/drills.jsonl \
        cloud=/srv/drills/cloud-offload-drill/drills.jsonl

Metrics
    drill_last_timestamp{source,label}        unix time of the last drill
    drill_last_result{source,label}           1 OK, 0 FAIL
    drill_last_rto_seconds{source,label}
    drill_last_rpo_seconds{source,label}
    drill_last_rate_mib_per_second{source,label}
    drill_last_success_timestamp{source,label} unix time of the last OK drill
    drill_count_total{source,label}           drills recorded (counter)
    drills_textfile_last_run_timestamp
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone


def parse_t0(s):
    try:
        return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def num(v):
    """'118', '118s', 118, '-', '?', '47.7 MiB/s' -> float or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    for suffix in (" MiB/s", "MiB/s", " MB/s", "MB/s", "s"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    try:
        return float(s)
    except ValueError:
        return None


def first(rec, *keys):
    for k in keys:
        if k in rec:
            return rec[k]
    return None


def normalise(rec):
    t0 = parse_t0(rec.get("t0"))
    if t0 is None:
        return None
    rate = rec.get("rate_mbs", rec.get("rate"))
    return {
        "label": str(rec.get("label", "")).strip() or "unlabeled",
        "t0": t0,
        "ok": 1 if str(rec.get("result", "")).upper() == "OK" else 0,
        "rto": num(first(rec, "rto", "rto_s", "seconds")),
        "rpo": num(first(rec, "rpo", "rpo_s")),
        "rate": num(rate),
    }


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = normalise(json.loads(line))
            except ValueError:
                continue
            if rec:
                out.append(rec)
    return out


def latest_per_label(recs):
    last, last_ok, count = {}, {}, {}
    for r in sorted(recs, key=lambda r: r["t0"]):
        last[r["label"]] = r
        count[r["label"]] = count.get(r["label"], 0) + 1
        if r["ok"]:
            last_ok[r["label"]] = r["t0"]
    return last, last_ok, count


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


HELP = [
    ("drill_last_timestamp", "Unix time of the last drill", "gauge"),
    ("drill_last_result", "1 when the last drill passed", "gauge"),
    ("drill_last_rto_seconds", "RTO of the last drill", "gauge"),
    ("drill_last_rpo_seconds", "RPO of the last drill", "gauge"),
    ("drill_last_rate_mib_per_second", "Transfer rate of the last drill", "gauge"),
    ("drill_last_success_timestamp", "Unix time of the last passing drill", "gauge"),
    ("drill_count_total", "Drills recorded", "counter"),
]


def render(sources, now):
    rows = {name: [] for name, _, _ in HELP}
    for source, path in sources:
        try:
            recs = read_jsonl(path)
        except OSError:
            continue
        last, last_ok, count = latest_per_label(recs)
        for label, r in sorted(last.items()):
            lab = f'{{source="{esc(source)}",label="{esc(label)}"}}'
            rows["drill_last_timestamp"].append(f"drill_last_timestamp{lab} {r['t0']}")
            rows["drill_last_result"].append(f"drill_last_result{lab} {r['ok']}")
            if r["rto"] is not None:
                rows["drill_last_rto_seconds"].append(f"drill_last_rto_seconds{lab} {r['rto']:g}")
            if r["rpo"] is not None:
                rows["drill_last_rpo_seconds"].append(f"drill_last_rpo_seconds{lab} {r['rpo']:g}")
            if r["rate"] is not None:
                rows["drill_last_rate_mib_per_second"].append(f"drill_last_rate_mib_per_second{lab} {r['rate']:g}")
            if label in last_ok:
                rows["drill_last_success_timestamp"].append(f"drill_last_success_timestamp{lab} {last_ok[label]}")
            rows["drill_count_total"].append(f"drill_count_total{lab} {count[label]}")
    lines = []
    for name, hlp, typ in HELP:
        if rows[name]:
            lines += [f"# HELP {name} {hlp}", f"# TYPE {name} {typ}"] + rows[name]
    lines += ["# HELP drills_textfile_last_run_timestamp Unix time of the last run",
              "# TYPE drills_textfile_last_run_timestamp gauge",
              f"drills_textfile_last_run_timestamp {int(now)}"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="name=path/to/drills.jsonl")
    ap.add_argument("--out", help="write here atomically; default stdout")
    a = ap.parse_args(argv)
    sources = []
    for s in a.sources:
        if "=" not in s:
            ap.error(f"expected name=path, got {s}")
        sources.append(tuple(s.split("=", 1)))
    text = render(sources, time.time())
    if a.out:
        d = os.path.dirname(a.out) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".prom.")
        # mkstemp makes 0600; node_exporter runs as another user and must read it
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, a.out)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
