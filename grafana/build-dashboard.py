#!/usr/bin/env python3
"""Build grafana/dashboards/onprem-overview.json.

One dashboard, six rows, each row answers one question an operator asks
when paged at 03:00. The JSON is generated so thresholds stay in one place
and match prometheus/rules/thresholds.yml: edit this file, run it, commit
both. Grafana loads the JSON through provisioning with allowUiUpdates=false.

    ./build-dashboard.py            # writes dashboards/onprem-overview.json
    ./build-dashboard.py --check    # exit 1 if the JSON on disk is stale
"""
import json, os, sys

DS = {"type": "prometheus", "uid": "prometheus"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards", "onprem-overview.json")

# Thresholds shared with prometheus/rules/thresholds.yml
SOAK_WARN, SOAK_BUDGET = 180, 240
MEM_WARN, MEM_CRIT = 6 * 1024 ** 3, 3 * 1024 ** 3
POOL_WARN, POOL_CRIT = 80, 90
DISK_TEMP_WARN = 50
DRILL_STALE_DAYS, CLOUD_DRILL_STALE_DAYS = 30, 90
RTO_TARGET = 900

# Labels no table needs to show
NOISE = ("Time", "__name__", "job", "instance", "site", "role", "hw")


def pve(expr, agg="max"):
    """pve-exporter is scraped through both nodes and each reports the whole
    cluster; drop the instance label so a VM or a storage is one row."""
    return f"{agg} without (instance) ({expr})"


_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def steps(*pairs):
    """[(None,'green'), (200,'orange'), (240,'red')] -> Grafana threshold steps"""
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in pairs]}

def vmap(*pairs, other=None):
    """Value mappings for a status column: (value, text, color) ...; `other`
    is (text_or_None, color) for anything not listed, so an unseen string
    shows up coloured instead of blending in."""
    opts = {str(v): {"text": t, "color": c, "index": i} for i, (v, t, c) in enumerate(pairs)}
    m = [{"type": "value", "options": opts}]
    if other:
        res = {"color": other[1], "index": len(pairs)}
        if other[0]:
            res["text"] = other[0]
        m.append({"type": "regex", "options": {"pattern": ".*", "result": res}})
    return m

def target(expr, legend="", instant=False, fmt="time_series"):
    t = {"datasource": DS, "expr": expr, "refId": "A", "legendFormat": legend or "__auto"}
    if instant:
        t["instant"] = True
        t["range"] = False
    if fmt != "time_series":
        t["format"] = fmt
    return t

def panel(ptype, title, x, y, w, h, targets, unit=None, thresholds=None, desc="", **extra):
    fc = {"defaults": {"color": {"mode": "thresholds" if thresholds else "palette-classic"}}, "overrides": []}
    if unit:
        fc["defaults"]["unit"] = unit
    if thresholds:
        fc["defaults"]["thresholds"] = thresholds
    for i, t in enumerate(targets):
        t["refId"] = chr(65 + i)
    p = {"id": nid(), "type": ptype, "title": title, "description": desc, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets, "fieldConfig": fc, "options": {}}
    for k, v in extra.items():
        if k in ("options", "fieldConfig"):
            p[k].update(v) if k == "options" else p["fieldConfig"]["defaults"].update(v)
        else:
            p[k] = v
    return p

def stat(title, x, y, w, h, expr, legend, unit=None, thresholds=None, desc="", mode="value_and_name", decimals=None):
    opts = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": mode, "colorMode": "background", "graphMode": "none", "justifyMode": "center", "orientation": "auto"}
    p = panel("stat", title, x, y, w, h, [target(expr, legend, instant=True)], unit, thresholds, desc, options=opts)
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p

def bars(title, x, y, w, h, expr, legend, unit=None, thresholds=None, desc="", decimals=None, minv=0, maxv=None, overrides=()):
    """Horizontal bar gauge: one labelled bar per series, readable with long names."""
    opts = {"displayMode": "basic", "orientation": "horizontal", "showUnfilled": True, "namePlacement": "left",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "minVizHeight": 14, "maxVizHeight": 22, "valueMode": "color", "sizing": "manual"}
    fc = {"min": minv}
    if maxv is not None:
        fc["max"] = maxv
    p = panel("bargauge", title, x, y, w, h, [target(expr, legend, instant=True)], unit, thresholds, desc,
              options=opts, fieldConfig=fc)
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    p["fieldConfig"]["overrides"] = list(overrides)
    return p

def ts(title, x, y, w, h, targets, unit=None, lines=(), desc="", maxv=None, minv=None):
    """timeseries with optional constant threshold lines drawn on the plot"""
    p = panel("timeseries", title, x, y, w, h, targets, unit, None, desc,
              options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                       "tooltip": {"mode": "multi", "sort": "desc"}})
    d = p["fieldConfig"]["defaults"]
    d["custom"] = {"lineWidth": 1, "fillOpacity": 8, "showPoints": "never", "spanNulls": 60000}
    if lines:
        d["thresholds"] = steps((None, "transparent"), *lines)
        d["custom"]["thresholdsStyle"] = {"mode": "line+area"}
    if maxv is not None:
        d["max"] = maxv
    if minv is not None:
        d["min"] = minv
    return p

def table(title, x, y, w, h, targets, desc="", overrides=(), hide=(), rename=None, order=None, sort=None, show=()):
    """Instant queries in table format. Several queries are merged into one
    row per label set; their values arrive as "Value #A", "Value #B"... and
    are renamed here so the header says what the number is. The merge
    compares every label column, __name__ included, so with more than one
    query each one drops __name__ first or nothing lines up."""
    if len(targets) > 1:
        for t in targets:
            t["expr"] = f"max without (__name__) ({t['expr']})"
    p = panel("table", title, x, y, w, h, targets, None, None, desc,
              options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}})
    org = {"excludeByName": {k: True for k in NOISE + tuple(hide) if k not in show}, "renameByName": rename or {}}
    if order:
        org["indexByName"] = {k: i for i, k in enumerate(order)}
    p["transformations"] = [{"id": "merge", "options": {}}, {"id": "organize", "options": org}]
    if sort:
        p["options"]["sortBy"] = [{"displayName": sort, "desc": False}]
    p["fieldConfig"]["overrides"] = list(overrides)
    return p

def row(title, y, collapsed=False):
    return {"id": nid(), "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}

def cell(field, thresholds=None, unit=None, mappings=None, width=None):
    """Colour one table column. The color mode has to be set to thresholds
    on the column itself, or Grafana paints the cell with the series
    palette colour and the thresholds are ignored."""
    props = [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
             {"id": "color", "value": {"mode": "thresholds"}}]
    if thresholds:
        props.append({"id": "thresholds", "value": thresholds})
    if mappings:
        props.append({"id": "mappings", "value": mappings})
    if unit:
        props.append({"id": "unit", "value": unit})
    if width:
        props.append({"id": "custom.width", "value": width})
    return {"matcher": {"id": "byName", "options": field}, "properties": props}

def col(field, unit=None, width=None, decimals=None):
    props = []
    if unit:
        props.append({"id": "unit", "value": unit})
    if width:
        props.append({"id": "custom.width", "value": width})
    if decimals is not None:
        props.append({"id": "decimals", "value": decimals})
    return {"matcher": {"id": "byName", "options": field}, "properties": props}

UPDOWN = vmap((1, "UP", "green"), (0, "DOWN", "red"))


def build():
    P = []
    y = 0

    # ---- Row 0: what is firing right now ---------------------------------
    P.append(row("現在", y)); y += 1
    P.append(stat("critical", 0, y, 4, 6,
                  'count(ALERTS{alertstate="firing",severity="critical"}) or vector(0)', "critical",
                  thresholds=steps((None, "green"), (1, "red")), desc="Prometheus 規則正在 firing 的 critical 數"))
    P.append(stat("warning", 4, y, 4, 6,
                  'count(ALERTS{alertstate="firing",severity="warning"}) or vector(0)', "warning",
                  thresholds=steps((None, "green"), (1, "orange"))))
    P.append(table("firing 清單", 8, y, 16, 6,
                   [target('ALERTS{alertstate="firing"}', instant=True, fmt="table")],
                   desc="每一列是一條規則對一組標籤，與 Alertmanager 收到的是同一份。嚴重度與規則名稱來自 prometheus/rules",
                   hide=("Value", "alertstate"), sort="severity",
                   order=("alertname", "severity"),
                   overrides=[cell("severity", mappings=vmap(("critical", "critical", "red"), ("warning", "warning", "orange"),
                                                             ("info", "info", "blue")), width=90)]))
    y += 6

    # ---- Row 1: GPU nodes ------------------------------------------------
    P.append(row("GPU 節點（DGX Spark）", y)); y += 1
    P.append(stat("熱 soak 秒數", 0, y, 6, 5, "gb10_soak_seconds", "{{node}}", unit="s",
                  thresholds=steps((None, "green"), (SOAK_WARN, "orange"), (SOAK_BUDGET, "red")),
                  desc=f"最熱 thermal zone 連續 ≥ 88 °C 的秒數。守護在 {SOAK_BUDGET} s 動手，告警在 {SOAK_WARN} s，差額涵蓋 textfile 最多 10 s 的舊值、抓取 10 或 20 s 的跳階、規則評估最多 15 s（Day 20 實測）"))
    P.append(stat("可用記憶體", 6, y, 6, 5, "gb10_mem_available_bytes", "{{node}}", unit="bytes",
                  thresholds=steps((None, "red"), (MEM_CRIT, "orange"), (MEM_WARN, "green")),
                  desc="MemAvailable。統一記憶體看不到 GPU 配置，這個數掉到 6 GiB 以下 hw-sample 會警告，3 GiB 以下動手（gb10-ops）"))
    P.append(stat("NV_ERR_NO_MEMORY（24h）", 12, y, 6, 5, "increase(gb10_nv_err_no_memory_total[24h])", "{{node}}",
                  thresholds=steps((None, "green"), (1, "red")), decimals=0,
                  desc="dmesg 裡 NV_ERR_NO_MEMORY 24 小時內增加的筆數。非零就是統一記憶體耗盡的早期訊號"))
    P.append(stat("HOT 旗標", 18, y, 6, 5, "gb10_hot", "{{node}}",
                  thresholds=steps((None, "green"), (1, "orange")), decimals=0,
                  desc="hw-sample.py 立的 HOT 旗標，批次工作在階段之間要看它"))
    y += 5
    P.append(ts("溫度", 0, y, 12, 8,
                [target("gb10_zone_temp_celsius", "zone {{node}}"), target("gb10_gpu_temp_celsius", "gpu {{node}}")],
                unit="celsius", lines=((88, "orange"),), minv=30, maxv=100,
                desc="最熱 zone 與 nvidia-smi 的 GPU 溫度。橘線 88 °C 是 soak 門檻"))
    P.append(ts("soak 累積", 12, y, 12, 8,
                [target("gb10_soak_seconds", "{{node}}")],
                unit="s", lines=((SOAK_WARN, "orange"), (SOAK_BUDGET, "red")), minv=0, maxv=300,
                desc="Day 19 第六節的曲線。15 s 抓取下一次升溫約 15 個點，守護動手時歸零"))
    y += 8
    P.append(ts("GPU 功耗", 0, y, 12, 6, [target("gb10_gpu_power_watts", "{{node}}")], unit="watt"))
    P.append(ts("GPU 使用率", 12, y, 12, 6, [target("gb10_gpu_utilization_percent", "{{node}}")], unit="percent", minv=0, maxv=100))
    y += 6

    # ---- Row 2: NAS ------------------------------------------------------
    P.append(row("儲存（QNAP NAS）", y)); y += 1
    P.append(panel("bargauge", "儲存池使用率", 0, y, 8, 7,
                   [target("nas_zpool_capacity_percent", "{{nas}} {{pool}}", instant=True)], unit="percent",
                   thresholds=steps((None, "green"), (POOL_WARN, "orange"), (POOL_CRIT, "red")),
                   desc="zpool list 的 CAP。80 % 是 ZFS 碎片化的線（Day 14），90 % 以上寫入會明顯變慢",
                   options={"displayMode": "lcd", "orientation": "horizontal", "showUnfilled": True,
                            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                            "minVizHeight": 16, "maxVizHeight": 32}, fieldConfig={"min": 0, "max": 100}))
    P.append(table("儲存池狀態", 8, y, 8, 7,
                   [target("nas_zpool_health == 1", instant=True, fmt="table")],
                   desc="zpool status 的 state。ONLINE 以外都該有人在處理",
                   hide=("Value",), rename={"nas": "NAS", "pool": "池", "state": "狀態"},
                   overrides=[cell("狀態", mappings=vmap(("ONLINE", "ONLINE", "green"), other=(None, "red")))]))
    P.append(ts("磁碟最高溫", 16, y, 8, 7,
                [target("max by (nas) (qnap_disk_temperature_celsius)", "{{nas}}")],
                unit="celsius", lines=((DISK_TEMP_WARN, "orange"),), minv=20, maxv=70,
                desc="SNMP 的 diskTemperature，每台 NAS 取最高那顆"))
    y += 7
    # One query: temperature as the value, the status string joined in as a
    # label. Two queries merged would need identical label sets.
    P.append(table("磁碟", 0, y, 12, 8,
                   [target("qnap_disk_temperature_celsius * on (instance, diskIndex) group_left (qnap_disk_status) (qnap_disk_status == 1)",
                           instant=True, fmt="table")],
                   desc="每顆磁碟的狀態字串與溫度。QuTS hero 的健康值是 Good，其他字串 NasDiskNotGood 會響",
                   hide=("diskSerialNumber", "diskType"),
                   rename={"nas": "NAS", "diskIndex": "槽", "diskModel": "型號", "qnap_disk_status": "狀態", "Value": "溫度"},
                   order=("NAS", "槽", "型號", "狀態", "溫度"),
                   overrides=[col("槽", width=50), col("NAS", width=90),
                              cell("狀態", mappings=vmap(("Good", "Good", "green"), other=(None, "red")), width=80),
                              cell("溫度", steps((None, "green"), (DISK_TEMP_WARN, "orange")), "celsius", width=80)]))
    P.append(table("快照", 12, y, 12, 8,
                   [target("nas_zfs_snapshots", instant=True, fmt="table"),
                    target("(time() - nas_zfs_snapshot_newest_timestamp) / 86400", instant=True, fmt="table"),
                    target("nas_zfs_snapshot_used_bytes", instant=True, fmt="table")],
                   desc="每個資料集的快照數、最新快照距今天數、快照佔用。:init: 不計。Day 17 的政策是每日快照，超過 2 天 NasSnapshotStale 會響",
                   rename={"nas": "NAS", "dataset": "資料集", "Value #A": "快照數", "Value #B": "最新距今", "Value #C": "佔用"},
                   overrides=[cell("最新距今", steps((None, "green"), (2, "orange"), (7, "red")), "d"),
                              col("佔用", unit="bytes")]))
    y += 8
    P.append(ts("NAS 溫度", 0, y, 8, 6,
                [target("qnap_cpu_temperature_celsius", "cpu {{nas}}"), target("qnap_system_temperature_celsius", "system {{nas}}")],
                unit="celsius", desc="SNMP 的 CPU 與系統溫度，有快取，看趨勢不看絕對值（Day 19）"))
    # Physical ports and bonds only: QuTS hero also lists every docker veth,
    # bridge and VM tap, 50+ series on a busy NAS.
    nics = 'ifName=~"eth[0-9]+|bond[0-9]+"'
    P.append(ts("NAS 網路", 8, y, 8, 6,
                [target(f'rate(ifHCInOctets{{{nics}}}[5m]) * 8', "in {{nas}} {{ifName}}"),
                 target(f'rate(ifHCOutOctets{{{nics}}}[5m]) * 8', "out {{nas}} {{ifName}}")],
                unit="bps", desc="IF-MIB 計數，只看實體埠與 bond。Day 14 量到的碟組上限約 666 MB/s，網路不是瓶頸時應低於它"))
    P.append(ts("風扇", 16, y, 8, 6, [target("qnap_fan_speed_rpm", "{{nas}} fan {{sysFanIndex}}")], unit="rotrpm"))
    y += 6

    # ---- Row 3: PVE ------------------------------------------------------
    P.append(row("虛擬化（Proxmox VE）", y)); y += 1
    P.append(stat("法定人數", 0, y, 4, 5, "pve_quorum_quorate", "{{cluster}}",
                  thresholds=steps((None, "red"), (1, "green")), decimals=0,
                  desc="pvecm status 的 Quorate 旗標。0 時有 HA 資源的節點約 60 秒後自己 fence（Day 15）"))
    P.append(stat("在場票數", 4, y, 4, 5, "pve_quorum_total_votes", "{{cluster}}",
                  thresholds=steps((None, "red"), (2, "orange"), (3, "green")), decimals=0,
                  desc="Total votes。3 正常，2 是 Day 15 check.sh 的 rc=1，再掉任何一個就失去法定人數"))
    P.append(stat("QDevice 票", 8, y, 4, 5, "pve_quorum_qdevice_votes", "{{cluster}}",
                  thresholds=steps((None, "orange"), (1, "green")), decimals=0,
                  desc="0 多半是 NAS 重開中，Virtualization Station 上的 qnetd 還沒回來，或節點上的 corosync-qdevice 停了"))
    P.append(stat("節點", 12, y, 12, 5, pve('pve_up{id=~"node/.*"}'), "{{id}}",
                  thresholds=steps((None, "red"), (1, "green")), decimals=0))
    y += 5
    P.append(table("HA 資源", 0, y, 8, 8,
                   [target(pve("pve_ha_state == 1"), instant=True, fmt="table")],
                   desc="每個 HA 管理的客體與節點目前的狀態。error、fence、recovery 會響",
                   hide=("Value",), rename={"id": "資源", "state": "狀態"},
                   overrides=[cell("狀態", mappings=vmap(("started", "started", "green"), ("online", "online", "green"),
                                                         ("stopped", "stopped", "blue"), ("error", "error", "red"),
                                                         ("fence", "fence", "red"), ("recovery", "recovery", "red"),
                                                         other=(None, "orange")))]))
    P.append(table("儲存", 8, y, 8, 8,
                   [target(pve('pve_up{id=~"storage/.*"}'), instant=True, fmt="table")],
                   desc="各節點看到的資料存放區。NFS 在 NAS 重開時會變不可用，約 8 分鐘（Day 15 演練 1）",
                   rename={"id": "儲存", "Value": "狀態"},
                   overrides=[cell("狀態", mappings=vmap((1, "可用", "green"), (0, "不可用", "red")), width=90)]))
    P.append(ts("執行中的 VM 數", 16, y, 8, 8,
                [target('count(' + pve('pve_up{id=~"qemu/.*|lxc/.*"}') + ' == 1)', "running"),
                 target('count(' + pve('pve_up{id=~"qemu/.*|lxc/.*"}') + ')', "defined")],
                desc="pve-exporter 兩個節點都回整座叢集，先去掉 instance 再 count，否則每台客體算兩次",
                minv=0))
    y += 8
    P.append(ts("節點 CPU", 0, y, 8, 6, [target(pve('pve_cpu_usage_ratio{id=~"node/.*"}'), "{{id}}")], unit="percentunit", minv=0, maxv=1))
    P.append(ts("節點記憶體", 8, y, 8, 6,
                [target(pve('pve_memory_usage_bytes{id=~"node/.*"}') + " / " + pve('pve_memory_size_bytes{id=~"node/.*"}'), "{{id}}")],
                unit="percentunit", minv=0, maxv=1))
    P.append(ts("客體磁碟 I/O", 16, y, 8, 6,
                [target('sum(' + pve('rate(pve_disk_read_bytes{id=~"qemu/.*"}[5m])') + ')', "read"),
                 target('sum(' + pve('rate(pve_disk_write_bytes{id=~"qemu/.*"}[5m])') + ')', "write")],
                unit="Bps"))
    y += 6

    # ---- Row 4: drills ---------------------------------------------------
    P.append(row("還原演練（Day 16 至 18）", y)); y += 1
    P.append(bars("距上次成功（天）", 0, y, 10, 10,
                  "(time() - drill_last_success_timestamp) / 86400", "{{source}} {{label}}", unit="d", decimals=1,
                  thresholds=steps((None, "green"), (DRILL_STALE_DAYS, "orange"), (CLOUD_DRILL_STALE_DAYS, "red")),
                  maxv=CLOUD_DRILL_STALE_DAYS,
                  desc=f"每個演練標籤一條。pve 與 nas 的政策是每月，{DRILL_STALE_DAYS} 天轉橘。cloud 是每季，{CLOUD_DRILL_STALE_DAYS} 天轉紅。這一排全綠才叫備份存在"))
    P.append(table("最近一次演練", 10, y, 14, 10,
                   [target("drill_last_result", instant=True, fmt="table"),
                    target("drill_last_rto_seconds", instant=True, fmt="table"),
                    target("drill_last_rpo_seconds", instant=True, fmt="table"),
                    target("drill_last_rate_mib_per_second", instant=True, fmt="table")],
                   desc=f"結果、RTO、RPO、速率。RTO 目標 {RTO_TARGET} s 只對 pve 演練（Day 16 量到 569 s），nas 與 cloud 的標籤含上傳與整個資料夾回復，量級不同",
                   rename={"source": "來源", "label": "演練", "Value #A": "結果", "Value #B": "RTO", "Value #C": "RPO", "Value #D": "速率"},
                   order=("來源", "演練", "結果", "RTO", "RPO", "速率"),
                   overrides=[col("來源", width=70),
                              cell("結果", mappings=vmap((1, "通過", "green"), (0, "失敗", "red")), width=70),
                              cell("RTO", steps((None, "green"), (RTO_TARGET, "orange")), "s", width=90),
                              col("RPO", unit="s", width=90), col("速率", unit="MiBs", width=100, decimals=1)]))
    y += 10
    P.append(ts("RTO 走勢", 0, y, 24, 7, [target("drill_last_rto_seconds", "{{source}} {{label}}")],
                unit="s", lines=((RTO_TARGET, "orange"),), minv=0,
                desc="每次演練寫入 drills.jsonl 後 15 分鐘內更新。走勢往上就是還原在變慢"))
    y += 7

    # ---- Row 5: the pipeline itself -------------------------------------
    P.append(row("管線", y)); y += 1
    P.append(table("抓取目標", 0, y, 12, 8,
                   [target("up", instant=True, fmt="table")],
                   desc="每個抓取目標最後一次抓取是否成功。DOWN 五分鐘 TargetDown 會響",
                   rename={"Value": "狀態"}, show=("job", "instance"), order=("job", "instance", "nas", "狀態"),
                   overrides=[cell("狀態", mappings=UPDOWN, width=80)]))
    # Each textfile writes <name>_textfile_last_run_timestamp. Subtracting
    # drops __name__, and the collector's drills and pve-quorum files would
    # then collide on the same label set, so the name is copied to a label
    # first.
    P.append(bars("textfile 距上次執行", 12, y, 12, 8,
                  'time() - label_replace({__name__=~".+_textfile_last_run_timestamp"}, "textfile", "$1", "__name__", "(.+)_textfile_last_run_timestamp")',
                  "{{textfile}} {{node}}{{nas}}", unit="s", decimals=0, maxv=1200,
                  thresholds=steps((None, "green"), (120, "orange"), (600, "red")),
                  overrides=[{"matcher": {"id": "byRegexp", "options": "drills.*"},
                              "properties": [{"id": "thresholds", "value": steps((None, "green"), (1800, "orange"), (7200, "red"))}]}],
                  desc="Day 19 第四節。舊的 .prom 檔會讓數字看起來正常，這一格變橘就是靜默失效。drills 每 15 分鐘一次，其他每分鐘或更快"))
    y += 8

    return {
        "uid": "onprem-overview", "title": "地端機房總覽", "tags": ["onprem", "day20"],
        "timezone": "browser", "editable": False, "graphTooltip": 1, "schemaVersion": 39, "version": 1,
        "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
        "annotations": {"list": [
            {"name": "告警", "datasource": DS, "enable": True, "iconColor": "red",
             "expr": 'ALERTS{alertstate="firing",severity="critical"}', "step": "60s",
             "titleFormat": "{{alertname}}", "textFormat": "{{summary}}"}]},
        "templating": {"list": []},
        "panels": P,
    }


def lint(dash):
    """Panel ids unique, every panel inside the 24-column grid, no two panels
    overlapping. Grafana silently reflows overlaps, so a typo in a y offset
    shows up as a shuffled layout rather than an error."""
    errs, seen, cells = [], set(), {}
    for p in dash["panels"]:
        g = p["gridPos"]
        if p["id"] in seen:
            errs.append(f"duplicate id {p['id']}")
        seen.add(p["id"])
        if g["x"] + g["w"] > 24:
            errs.append(f"{p['title']} wider than the grid")
        for cx in range(g["x"], g["x"] + g["w"]):
            for cy in range(g["y"], g["y"] + g["h"]):
                if (cx, cy) in cells:
                    errs.append(f"{p['title']} overlaps {cells[(cx, cy)]}")
                    break
                cells[(cx, cy)] = p["title"]
            else:
                continue
            break
    return errs


def main():
    dash = build()
    doc = json.dumps(dash, ensure_ascii=False, indent=1) + "\n"
    errs = lint(dash)
    if errs:
        sys.exit("\n".join(errs))
    if "--check" in sys.argv:
        cur = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        if cur != doc:
            sys.exit(f"{OUT} is stale, run build-dashboard.py")
        print("dashboard up to date")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"wrote {OUT} ({len(dash['panels'])} panels)")


if __name__ == "__main__":
    main()
