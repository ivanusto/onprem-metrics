#!/usr/bin/env python3
"""Build snmp.yml for snmp_exporter from QNAP's QTS-MIB, without the generator.

The upstream generator needs net-snmp and a MIB directory; this lab has
neither on the collector. The QTS MIB is small and regular, so this script
resolves the OIDs itself and writes the module in snmp_exporter's own
config format. Download NAS.mib from the NAS (Control Panel, SNMP) and run:

    ./snmp-build.py NAS.mib > snmp.yml
    snmp_exporter --config.file=snmp.yml --config.file=snmp-auth.yml --dry-run

The MIB itself is QNAP's and is not committed; the generated snmp.yml is.
Objects chosen below are the ones a dashboard and the Day 22 to 25 tools
need: disks, SMART, RAID, pools, shared folders (WORM, compression), fans,
temperatures, CPU and memory, UPS, firmware, exposed services, installed
apps. Network counters come from IF-MIB, which every agent serves.

Only QuTS hero (enterprises 55062.2) is handled. QTS uses 24681 (待查).
"""
import re
import sys

OBJ = re.compile(
    r"^\s*([A-Za-z][\w-]*)\s+OBJECT-TYPE\s+SYNTAX\s+(.+?)\s+MAX-ACCESS.*?(?:INDEX\s*\{\s*([\w-]+)\s*\})?\s*::=\s*\{\s*([\w-]+)\s+(\d+)\s*\}",
    re.S | re.M)
OID = re.compile(r"^\s*([\w-]+)\s+OBJECT IDENTIFIER\s*::=\s*\{\s*([\w-]+)\s+(\d+)\s*\}", re.M)
MODID = re.compile(r"^(\w+)\s+MODULE-IDENTITY.*?::=\s*\{\s*enterprises\s+(\d+)\s*\}", re.S | re.M)

TYPE = {"Integer32": "gauge", "Counter64": "counter", "OCTET STRING": "DisplayString",
        "TimeTicks": "gauge", "IpAddress": "IpAddr", "INTEGER": "EnumAsInfo"}
ENUM = re.compile(r"([\w-]+)\((-?\d+)\)")


def parse(text):
    tree = {"enterprises": "1.3.6.1.4.1"}
    m = MODID.search(text)
    tree[m.group(1)] = f"1.3.6.1.4.1.{m.group(2)}"
    objs = {}
    pending = []
    for name, parent, n in OID.findall(text):
        pending.append((name, parent, n))
    for name, syntax, index, parent, n in OBJ.findall(text):
        pending.append((name, parent, n))
        objs[name] = {"syntax": syntax.strip(), "index": index or None, "parent": parent}
    # resolve iteratively
    while pending:
        rest = []
        for name, parent, n in pending:
            if parent in tree:
                tree[name] = f"{tree[parent]}.{n}"
            else:
                rest.append((name, parent, n))
        if len(rest) == len(pending):
            raise SystemExit(f"unresolved: {[r[0] for r in rest][:5]}")
        pending = rest
    return tree, objs


def table_index(objs, name):
    """Walk up from a column to its ...Entry object and return the INDEX name."""
    cur = objs[name]["parent"]
    while cur in objs:
        if objs[cur]["index"]:
            return objs[cur]["index"]
        cur = objs[cur]["parent"]
    return None


# (object, prometheus name, help); lookups add string columns as labels
TABLES = {
    "diskTableEntry": {
        "labels": ["diskModel", "diskSerialNumber", "diskType"],
        "cols": [("diskTemperature", "qnap_disk_temperature_celsius", "Disk temperature"),
                 ("diskCapacity", "qnap_disk_capacity_bytes", "Disk capacity"),
                 ("diskStatus", "qnap_disk_status", "Disk status string as label, value 1")],
    },
    "diskSMARTInfoTableEntry": {
        "labels": ["diskSMARTInfoDeviceName", "diskSMARTInfoAttributeName", "diskSMARTInfoAttributeID"],
        "cols": [("diskSMARTInfoAttributeCurrent", "qnap_smart_attribute_current", "SMART attribute current value"),
                 ("diskSMARTInfoAttributeWorst", "qnap_smart_attribute_worst", "SMART attribute worst value"),
                 ("diskSMARTInfoAttributeThreshold", "qnap_smart_attribute_threshold", "SMART attribute threshold"),
                 ("diskSMARTInfoAttributeRAW", "qnap_smart_attribute_raw", "SMART attribute raw value"),
                 ("diskSMARTInfoAttributeStatus", "qnap_smart_attribute_status", "SMART attribute status string, value 1")],
    },
    "raidTableEntry": {
        "labels": ["raidName", "raidLevel"],
        "cols": [("raidCapacity", "qnap_raid_capacity_bytes", "RAID group capacity"),
                 ("raidStatus", "qnap_raid_status", "RAID group status string, value 1")],
    },
    "storagepoolTableEntry": {
        "labels": ["storagepoolID"],
        "cols": [("storagepoolCapacity", "qnap_pool_capacity_bytes", "Pool capacity"),
                 ("storagepoolFreeSize", "qnap_pool_free_bytes", "Pool free"),
                 ("storagepoolStatus", "qnap_pool_status", "Pool status string, value 1")],
    },
    "sharedFolderTableEntry": {
        "labels": ["sharedFolderName"],
        "cols": [("sharedFolderCapacity", "qnap_folder_capacity_bytes", "Shared folder capacity"),
                 ("sharedFolderFreeSize", "qnap_folder_free_bytes", "Shared folder free"),
                 ("sharedFolderStatus", "qnap_folder_status", "Shared folder status string, value 1"),
                 ("sharedFolderWORMStatus", "qnap_folder_worm_status", "WORM status string, value 1"),
                 ("sharedFolderCompressionEnabled", "qnap_folder_compression_enabled", "Compression flag"),
                 ("sharedFolderDeduplicationEnabled", "qnap_folder_dedup_enabled", "Deduplication flag")],
    },
    "systemFanEntry": {
        "labels": ["sysFanDescr"],
        "cols": [("sysFanSpeed", "qnap_fan_speed_rpm", "Fan speed")],
    },
    "applicationsTableEntry": {
        "labels": ["appName", "appVersion", "appLatestVersion"],
        "cols": [("appInstallationDate", "qnap_app_info", "Installed app as labels, value 1")],
    },
}

SCALARS = [
    ("cpuTemperature", "qnap_cpu_temperature_celsius", "CPU temperature"),
    ("systemTemperature", "qnap_system_temperature_celsius", "System temperature"),
    ("systemCPU-Usage", "qnap_cpu_usage_percent", "CPU usage"),
    ("systemTotalMem", "qnap_memory_total_bytes", "Total memory"),
    ("systemFreeMem", "qnap_memory_free_bytes", "Free memory"),
    ("systemAvailableMem", "qnap_memory_available_bytes", "Available memory"),
    ("systemUsedMemory", "qnap_memory_used_bytes", "Used memory"),
    ("sysUptime", "qnap_uptime", "System uptime"),
    ("sysPowerStatus", "qnap_power_status", "Power status string, value 1"),
    ("upsStatus", "qnap_ups_status", "UPS status string, value 1"),
    ("upsLoadPercentage", "qnap_ups_load_percent", "UPS load"),
    ("upsBatteryCharge", "qnap_ups_battery_percent", "UPS battery charge"),
    ("firmwareVersion", "qnap_firmware_info", "Firmware version as label, value 1"),
    ("firmwareUpgradeAvailable", "qnap_firmware_upgrade_available", "Firmware upgrade available string, value 1"),
    ("systemModel", "qnap_model_info", "Model as label, value 1"),
    ("sshIsEnabled", "qnap_service_ssh_enabled", "SSH enabled"),
    ("telnetIsEnabled", "qnap_service_telnet_enabled", "Telnet enabled"),
    ("ftpEnabled", "qnap_service_ftp_enabled", "FTP enabled"),
    ("nfsV4IsEnabled", "qnap_service_nfsv4_enabled", "NFSv4 enabled"),
    ("nfsV2V3IsEnabled", "qnap_service_nfsv2v3_enabled", "NFSv2/v3 enabled"),
    ("diskCount", "qnap_disk_count", "Number of disks"),
    ("storagepoolCount", "qnap_pool_count", "Number of pools"),
]

IFMIB = """  - name: ifName
    oid: 1.3.6.1.2.1.31.1.1.1.1
    type: DisplayString
    help: Interface name
    indexes: [{labelname: ifIndex, type: gauge}]
  - name: ifHCInOctets
    oid: 1.3.6.1.2.1.31.1.1.1.6
    type: counter
    help: Bytes received
    indexes: [{labelname: ifIndex, type: gauge}]
    lookups: [{labels: [ifIndex], labelname: ifName, oid: 1.3.6.1.2.1.31.1.1.1.1, type: DisplayString}]
  - name: ifHCOutOctets
    oid: 1.3.6.1.2.1.31.1.1.1.10
    type: counter
    help: Bytes sent
    indexes: [{labelname: ifIndex, type: gauge}]
    lookups: [{labels: [ifIndex], labelname: ifName, oid: 1.3.6.1.2.1.31.1.1.1.1, type: DisplayString}]
  - name: ifHighSpeed
    oid: 1.3.6.1.2.1.31.1.1.1.15
    type: gauge
    help: Interface speed in Mbps
    indexes: [{labelname: ifIndex, type: gauge}]
    lookups: [{labels: [ifIndex], labelname: ifName, oid: 1.3.6.1.2.1.31.1.1.1.1, type: DisplayString}]
  - name: sysName
    oid: 1.3.6.1.2.1.1.5
    type: DisplayString
    help: System name
"""


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    tree, objs = parse(text)
    walk = set()
    out = ["# Generated by snmp-build.py from QTS-MIB (enterprises 55062, qutshero). Do not edit by hand.",
           "# Auth lives in snmp-auth.yml (gitignored); see snmp-auth.yml.example.",
           "modules:", "  qnap:", "    walk:"]
    metrics = []

    def emit(obj, pname, hlp, index=None, lookups=()):
        raw = objs[obj]["syntax"]
        syn = raw.split("(")[0].split("{")[0].strip()
        t = TYPE[syn]
        m = [f"  - name: {pname}", f"    oid: {tree[obj]}", f"    type: {t}", f"    help: {hlp} ({obj})"]
        if t == "EnumAsInfo":
            m.append("    enum_values:")
            for lab, val in ENUM.findall(raw):
                m.append(f"      {val}: {lab}")
        if index:
            m.append(f"    indexes: [{{labelname: {index}, type: gauge}}]")
            if lookups:
                m.append("    lookups:")
                for lab in lookups:
                    m.append(f"      - {{labels: [{index}], labelname: {lab}, oid: {tree[lab]}, type: DisplayString}}")
        metrics.append("\n".join(m))
        walk.add(tree[obj])

    for entry, spec in TABLES.items():
        index = objs[entry]["index"]
        for lab in spec["labels"]:
            walk.add(tree[lab])
        for obj, pname, hlp in spec["cols"]:
            emit(obj, pname, hlp, index, spec["labels"])
    for obj, pname, hlp in SCALARS:
        emit(obj, pname, hlp)
    for o in sorted(walk, key=lambda s: [int(x) for x in s.split(".")]):
        out.append(f"      - {o}")
    out += ["      - 1.3.6.1.2.1.1.5", "      - 1.3.6.1.2.1.31.1.1.1.1", "      - 1.3.6.1.2.1.31.1.1.1.6",
            "      - 1.3.6.1.2.1.31.1.1.1.10", "      - 1.3.6.1.2.1.31.1.1.1.15"]
    out += ["    max_repetitions: 25", "    retries: 3", "    timeout: 10s", "    metrics:"]
    out += ["  " + m.replace("\n", "\n  ") for m in metrics]
    out.append("  " + IFMIB.rstrip().replace("\n", "\n  "))
    print("\n".join(out))


if __name__ == "__main__":
    main()
