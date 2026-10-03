#!/usr/bin/env python3
"""Expose what hw-sample.py (gb10-ops, Day 12) knows as node_exporter textfile metrics.

Standard exporters see GPU temperature and MemAvailable. They do not see the
two things that actually took this box down: time spent hot, continuously
(thermal soak), and the NV_ERR_NO_MEMORY rate in the kernel log that arrives
43 minutes before the livelock. This script samples both and writes one .prom
file that node_exporter's textfile collector picks up on its next scrape.

Soak is stateful, so the script keeps a small JSON state file between runs:
the moment the hottest zone first went to or above SOAK_ZONE without coming
back down. Run it every 5 to 15 seconds from a systemd timer; a gap longer
than --max-gap resets the soak clock, so a stopped timer cannot fake a soak.

    gb10-textfile.py --out /var/lib/node_exporter/textfile/gb10.prom

Metrics
    gb10_zone_temp_celsius            hottest /sys/class/thermal zone
    gb10_gpu_temp_celsius             nvidia-smi temperature.gpu
    gb10_gpu_power_watts              nvidia-smi power.draw
    gb10_gpu_utilization_percent      nvidia-smi utilization.gpu
    gb10_soak_seconds                 seconds the hottest zone has been >= threshold, continuously
    gb10_soak_threshold_celsius       the threshold, so dashboards can draw it
    gb10_soak_budget_seconds          the budget (Day 12: 240)
    gb10_hot                          1 when the HOT flag file from hw-sample.py exists
    gb10_mem_available_bytes          MemAvailable from /proc/meminfo (the honest number on unified memory)
    gb10_nv_err_no_memory_total       NV_ERR_NO_MEMORY lines in dmesg since boot (counter)
    gb10_textfile_last_run_timestamp  unix time of this run, for staleness alerts
    gb10_textfile_errors              sources that failed this run

Python 3.8+, no third-party modules. Every source is optional: a missing
nvidia-smi or an unreadable dmesg produces no sample for that metric and
bumps gb10_textfile_errors, never a crash.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time

SOAK_ZONE = int(os.environ.get("SOAK_ZONE", "88"))
SOAK_BUDGET = int(os.environ.get("SOAK_BUDGET", "240"))
NV_ERR = re.compile(rb"NV_ERR_NO_MEMORY")


def zone(sysfs="/sys/class/thermal"):
    hottest = None
    for path in glob.glob(os.path.join(sysfs, "thermal_zone*", "temp")):
        try:
            with open(path) as fh:
                val = int(fh.read().strip()) // 1000
        except (OSError, ValueError):
            continue
        hottest = val if hottest is None else max(hottest, val)
    return hottest


def gpu(cmd="nvidia-smi"):
    try:
        out = subprocess.run(
            [cmd, "--query-gpu=temperature.gpu,power.draw,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip().splitlines()[0]
        temp, power, util = (p.strip() for p in out.split(","))
        return float(temp), float(power), float(util)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def mem_available(meminfo="/proc/meminfo"):
    try:
        with open(meminfo) as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def nv_err_count(cmd="dmesg"):
    """NV_ERR_NO_MEMORY lines since boot. dmesg needs CAP_SYSLOG or
    kernel.dmesg_restrict=0; the systemd unit grants the capability."""
    try:
        out = subprocess.run([cmd], capture_output=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return len(NV_ERR.findall(out))


def load_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state.")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def soak(state, now, hottest, threshold, max_gap):
    """Return (soak_seconds, new_state). Soak starts when the hottest zone
    reaches the threshold and ends the moment it drops below. A gap between
    runs longer than max_gap resets, because we cannot know what happened."""
    since = state.get("hot_since")
    last = state.get("last_run")
    hot = hottest is not None and hottest >= threshold
    if not hot or (last is not None and now - last > max_gap):
        since = None
    if hot and since is None:
        since = now
    return (0 if since is None else int(now - since)), {"hot_since": since, "last_run": now}


def render(samples, errors, now):
    """samples: list of (name, help, type, value, labels). Prometheus text
    exposition format 0.0.4, one HELP and TYPE per metric."""
    lines = []
    for name, hlp, typ, value, labels in samples:
        if value is None:
            continue
        lines.append(f"# HELP {name} {hlp}")
        lines.append(f"# TYPE {name} {typ}")
        lab = "" if not labels else "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"{name}{lab} {value}")
    lines.append("# HELP gb10_textfile_errors Sources that failed this run")
    lines.append("# TYPE gb10_textfile_errors gauge")
    lines.append(f"gb10_textfile_errors {errors}")
    lines.append("# HELP gb10_textfile_last_run_timestamp Unix time of the last run")
    lines.append("# TYPE gb10_textfile_last_run_timestamp gauge")
    lines.append(f"gb10_textfile_last_run_timestamp {int(now)}")
    return "\n".join(lines) + "\n"


def collect(now, state, *, sysfs="/sys/class/thermal", nvidia_smi="nvidia-smi",
            meminfo="/proc/meminfo", dmesg="dmesg", hot_flag="/run/gb10/HOT",
            threshold=SOAK_ZONE, budget=SOAK_BUDGET, max_gap=60):
    errors = 0
    hottest = zone(sysfs)
    if hottest is None:
        errors += 1
    g = gpu(nvidia_smi)
    if g is None:
        errors += 1
        gtemp = gpower = gutil = None
    else:
        gtemp, gpower, gutil = g
    mem = mem_available(meminfo)
    if mem is None:
        errors += 1
    nv = nv_err_count(dmesg)
    if nv is None:
        errors += 1
    soak_s, new_state = soak(state, now, hottest, threshold, max_gap)
    samples = [
        ("gb10_zone_temp_celsius", "Hottest thermal zone", "gauge", hottest, {}),
        ("gb10_gpu_temp_celsius", "GPU temperature from nvidia-smi", "gauge", gtemp, {}),
        ("gb10_gpu_power_watts", "GPU power draw from nvidia-smi", "gauge", gpower, {}),
        ("gb10_gpu_utilization_percent", "GPU utilization from nvidia-smi", "gauge", gutil, {}),
        ("gb10_soak_seconds", "Seconds the hottest zone has been at or above the threshold, continuously", "gauge", soak_s, {}),
        ("gb10_soak_threshold_celsius", "Soak threshold", "gauge", threshold, {}),
        ("gb10_soak_budget_seconds", "Soak budget before the guard trips", "gauge", budget, {}),
        ("gb10_hot", "1 when the HOT flag from hw-sample.py exists", "gauge", 1 if os.path.exists(hot_flag) else 0, {}),
        ("gb10_mem_available_bytes", "MemAvailable from /proc/meminfo", "gauge", mem, {}),
        ("gb10_nv_err_no_memory_total", "NV_ERR_NO_MEMORY lines in dmesg since boot", "counter", nv, {}),
    ]
    return render(samples, errors, now), new_state


def write_atomic(path, text):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".prom.")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="/var/lib/node_exporter/textfile/gb10.prom")
    ap.add_argument("--state", default="/var/lib/gb10-textfile/state.json")
    ap.add_argument("--hot-flag", default="/run/gb10/HOT")
    ap.add_argument("--max-gap", type=int, default=60, help="seconds between runs beyond which soak resets")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing --out")
    a = ap.parse_args(argv)
    now = time.time()
    text, state = collect(now, load_state(a.state), hot_flag=a.hot_flag, max_gap=a.max_gap)
    if a.stdout:
        sys.stdout.write(text)
    else:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        write_atomic(a.out, text)
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    save_state(a.state, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
