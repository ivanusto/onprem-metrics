#!/usr/bin/env python3
"""Build grafana/dashboards/onprem-overview.json.

One dashboard, five rows, each row answers one question an operator asks
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
SOAK_WARN, SOAK_BUDGET = 200, 240
MEM_WARN, MEM_CRIT = 6 * 1024 ** 3, 3 * 1024 ** 3
POOL_WARN, POOL_CRIT = 80, 90
DISK_TEMP_WARN = 50
DRILL_STALE_DAYS, CLOUD_DRILL_STALE_DAYS = 30, 90
RTO_TARGET = 900

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def steps(*pairs):
    """[(None,'green'), (200,'orange'), (240,'red')] -> Grafana threshold steps"""
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in pairs]}

def target(expr, legend="", instant=False, fmt="time_series"):
    t = {"datasource": DS, "expr": expr, "refId": chr(65 + len(legend) % 26), "legendFormat": legend or "__auto"}
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

def table(title, x, y, w, h, targets, desc="", overrides=(), unit=None, thresholds=None, hide=("Time", "__name__", "job", "instance")):
    p = panel("table", title, x, y, w, h, targets, unit, thresholds, desc,
              options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}})
    p["transformations"] = [{"id": "merge", "options": {}},
                            {"id": "organize", "options": {"excludeByName": {k: True for k in hide}}}]
    p["fieldConfig"]["overrides"] = list(overrides)
    return p

def row(title, y, collapsed=False):
    return {"id": nid(), "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}

def color_cell(field, thresholds, unit=None):
    props = [{"id": "custom.cellOptions", "value": {"type": "color-background"}}, {"id": "thresholds", "value": thresholds}]
    if unit:
        props.append({"id": "unit", "value": unit})
    return {"matcher": {"id": "byName", "options": field}, "properties": props}


def build():
    P = []
    y = 0

    # ---- Row 0: what is firing right now ---------------------------------
    P.append(row("現在", y)); y += 1
    P.append(stat("firing 的告警", 0, y, 4, 4,
                  'count(ALERTS{alertstate="firing",severity="critical"}) or vector(0)', "critical",
                  thresholds=steps((None, "green"), (1, "red")), desc="Prometheus 規則正在 firing 的 critical 數"))
    P.append(stat("warning", 4, y, 4, 4,
                  'count(ALERTS{alertstate="firing",severity="warning"}) or vector(0)', "warning",
                  thresholds=steps((None, "green"), (1, "orange"))))
    P.append(table("firing 清單", 8, y, 16, 4,
                   [target('ALERTS{alertstate="firing"}', instant=True, fmt="table")],
                   desc="每一列是一條規則對一組標籤。嚴重度與規則名稱來自 prometheus/rules",
                   hide=("Time", "Value", "__name__", "alertstate", "job", "instance", "site")))
    y += 4

    # ---- Row 1: GPU nodes ------------------------------------------------
    P.append(row("GPU 節點（DGX Spark）", y)); y += 1
    P.append(stat("熱 soak 秒數", 0, y, 6, 5, "gb10_soak_seconds", "{{node}}", unit="s",
                  thresholds=steps((None, "green"), (SOAK_WARN, "orange"), (SOAK_BUDGET, "red")),
                  desc=f"最熱 thermal zone 連續 ≥ 88 °C 的秒數。守護在 {SOAK_BUDGET} s 動手，告警在 {SOAK_WARN} s，差額是 textfile 10 s 加抓取 15 s 的相位差（Day 19）"))
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
                   overrides=[color_cell("state", steps((None, "green")))],
                   hide=("Time", "Value", "__name__", "job", "instance", "site")))
    P.append(ts("磁碟最高溫", 16, y, 8, 7,
                [target("max by (nas) (qnap_disk_temperature_celsius)", "{{nas}}")],
                unit="celsius", lines=((DISK_TEMP_WARN, "orange"),), minv=20, maxv=70,
                desc="SNMP 的 diskTemperature，每台 NAS 取最高那顆"))
    y += 7
    P.append(table("磁碟", 0, y, 12, 8,
                   [target("qnap_disk_status == 1", instant=True, fmt="table"),
                    target("qnap_disk_temperature_celsius", instant=True, fmt="table")],
                   desc="每顆磁碟的狀態字串與溫度。狀態字串不是 GOOD 時 NasDiskNotGood 會響",
                   overrides=[color_cell("Value #B", steps((None, "green"), (DISK_TEMP_WARN, "orange")), "celsius")],
                   hide=("Time", "Value #A", "__name__", "job", "instance", "site", "diskSerialNumber")))
    P.append(table("快照", 12, y, 12, 8,
                   [target("nas_zfs_snapshots", instant=True, fmt="table"),
                    target("(time() - nas_zfs_snapshot_newest_timestamp) / 86400", instant=True, fmt="table"),
                    target("nas_zfs_snapshot_used_bytes", instant=True, fmt="table")],
                   desc="每個資料集的快照數、最新快照距今天數、快照佔用。Day 17 的政策是每日快照，超過 2 天 NasSnapshotStale 會響",
                   overrides=[color_cell("Value #B", steps((None, "green"), (2, "orange"), (7, "red")), "d"),
                              {"matcher": {"id": "byName", "options": "Value #C"}, "properties": [{"id": "unit", "value": "bytes"}]}],
                   hide=("Time", "__name__", "job", "instance", "site")))
    y += 8
    P.append(ts("NAS 溫度", 0, y, 8, 6,
                [target("qnap_cpu_temperature_celsius", "cpu {{nas}}"), target("qnap_system_temperature_celsius", "system {{nas}}")],
                unit="celsius", desc="SNMP 的 CPU 與系統溫度，有快取，看趨勢不看絕對值（Day 19）"))
    P.append(ts("NAS 網路", 8, y, 8, 6,
                [target('rate(ifHCInOctets{ifName!~"lo.*"}[5m]) * 8', "in {{nas}} {{ifName}}"),
                 target('rate(ifHCOutOctets{ifName!~"lo.*"}[5m]) * 8', "out {{nas}} {{ifName}}")],
                unit="bps", desc="IF-MIB 計數。Day 14 量到的碟組上限約 666 MB/s，網路不是瓶頸時應低於它"))
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
                  desc="0 多半是 NAS 重開中，Virtualization Station 上的 qnetd 還沒回來"))
    P.append(stat("節點", 12, y, 6, 5, 'pve_up{id=~"node/.*"}', "{{id}}",
                  thresholds=steps((None, "red"), (1, "green")), decimals=0))
    P.append(stat("儲存", 18, y, 6, 5, 'pve_up{id=~"storage/.*"}', "{{id}}",
                  thresholds=steps((None, "red"), (1, "green")), decimals=0,
                  desc="NFS 資料存放區在 NAS 重開時會變 0，約 8 分鐘（Day 15 演練 1）"))
    y += 5
    P.append(table("HA 資源", 0, y, 12, 7,
                   [target("pve_ha_state == 1", instant=True, fmt="table")],
                   desc="每個 HA 管理的客體目前的狀態。started 以外的狀態都值得看一眼，error、fence、recovery 會響",
                   overrides=[color_cell("state", steps((None, "orange")))],
                   hide=("Time", "Value", "__name__", "job", "instance", "site")))
    P.append(ts("執行中的 VM 數", 12, y, 12, 7,
                [target('count(pve_up{id=~"qemu/.*|lxc/.*"} == 1)', "running"),
                 target('count(pve_up{id=~"qemu/.*|lxc/.*"})', "defined")],
                desc="pve-exporter 兩個節點都回整座叢集，這裡以 instance 去重前先 count，兩條線應各自是常數。pve-exporter 的目標若有兩個，加 instance 過濾（Day 19）",
                minv=0))
    y += 7
    P.append(ts("節點 CPU", 0, y, 8, 6, [target('pve_cpu_usage_ratio{id=~"node/.*"}', "{{id}}")], unit="percentunit", minv=0, maxv=1))
    P.append(ts("節點記憶體", 8, y, 8, 6,
                [target('pve_memory_usage_bytes{id=~"node/.*"} / pve_memory_size_bytes{id=~"node/.*"}', "{{id}}")],
                unit="percentunit", minv=0, maxv=1))
    P.append(ts("客體磁碟 I/O", 16, y, 8, 6,
                [target('sum(rate(pve_disk_read_bytes{id=~"qemu/.*"}[5m]))', "read"),
                 target('sum(rate(pve_disk_write_bytes{id=~"qemu/.*"}[5m]))', "write")],
                unit="Bps"))
    y += 6

    # ---- Row 4: drills ---------------------------------------------------
    P.append(row("還原演練（Day 16 至 18）", y)); y += 1
    P.append(stat("距上次成功還原（天）", 0, y, 24, 5,
                  "(time() - drill_last_success_timestamp) / 86400", "{{source}} {{label}}", unit="d", decimals=1,
                  thresholds=steps((None, "green"), (DRILL_STALE_DAYS, "orange"), (CLOUD_DRILL_STALE_DAYS, "red")),
                  desc=f"每個演練標籤一格。pve 與 nas 的政策是每月，{DRILL_STALE_DAYS} 天轉橘。cloud 是每季，{CLOUD_DRILL_STALE_DAYS} 天轉紅。這一排全綠才叫備份存在"))
    y += 5
    P.append(table("最近一次演練", 0, y, 14, 8,
                   [target("drill_last_result", instant=True, fmt="table"),
                    target("drill_last_rto_seconds", instant=True, fmt="table"),
                    target("drill_last_rpo_seconds", instant=True, fmt="table"),
                    target("drill_last_rate_mib_per_second", instant=True, fmt="table")],
                   desc=f"結果、RTO、RPO、速率。RTO 目標 {RTO_TARGET} s（Day 16 量到 569 s）",
                   overrides=[color_cell("Value #A", steps((None, "red"), (1, "green"))),
                              color_cell("Value #B", steps((None, "green"), (RTO_TARGET, "orange")), "s"),
                              {"matcher": {"id": "byName", "options": "Value #C"}, "properties": [{"id": "unit", "value": "s"}]},
                              {"matcher": {"id": "byName", "options": "Value #D"}, "properties": [{"id": "unit", "value": "MiBs"}]}],
                   hide=("Time", "__name__", "job", "instance", "site")))
    P.append(ts("RTO 走勢", 14, y, 10, 8, [target("drill_last_rto_seconds", "{{source}} {{label}}")],
                unit="s", lines=((RTO_TARGET, "orange"),), minv=0,
                desc="每次演練寫入 drills.jsonl 後 15 分鐘內更新。走勢往上就是還原在變慢"))
    y += 8

    # ---- Row 5: the pipeline itself -------------------------------------
    P.append(row("管線", y)); y += 1
    P.append(stat("抓取目標", 0, y, 12, 4, "up", "{{job}} {{instance}}",
                  thresholds=steps((None, "red"), (1, "green")), decimals=0, mode="name"))
    P.append(stat("textfile 距上次執行", 12, y, 12, 4,
                  'max by (job) (time() - {__name__=~".*_textfile_last_run_timestamp"})', "{{job}}", unit="s", decimals=0,
                  thresholds=steps((None, "green"), (120, "orange"), (600, "red")),
                  desc="Day 19 第四節。舊的 .prom 檔會讓數字看起來正常，這一格變橘就是靜默失效"))
    y += 4

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


def main():
    doc = json.dumps(build(), ensure_ascii=False, indent=1) + "\n"
    if "--check" in sys.argv:
        cur = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        if cur != doc:
            sys.exit(f"{OUT} is stale, run build-dashboard.py")
        print("dashboard up to date")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"wrote {OUT} ({len(build()['panels'])} panels)")


if __name__ == "__main__":
    main()
