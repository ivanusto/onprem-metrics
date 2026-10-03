#!/bin/sh
# verify: is every target up, how long do scrapes take, and does one
# number per source match the tool it came from. Prints Markdown.
#
#   PROM=http://192.168.2.49:9090 ./verify.sh [--gb10 user@ip] [--nas LABEL=user@ip] [--pve user@ip]
#
# --gb10 and --nas may be given more than once. Use the address that is in
# the target files: gb10 series carry node="<ip>" and SNMP series carry
# instance="<ip>", so the host part is how a row finds its own series.
# --nas takes the same LABEL as nas-textfile.sh (nas="LABEL").
#
# The cross-checks ssh to the source and run the original command, so the
# table in the article is "Prometheus says X, the box says Y" with both
# taken within a few seconds of each other. Any check you cannot run is
# printed as "-" rather than skipped, so the gaps are visible.
# shellcheck disable=SC2016
set -eu
PROM=${PROM:-http://localhost:9090}
gb10s=; nases=; pve=
while [ $# -gt 0 ]; do
  case "$1" in
    --gb10) gb10s="$gb10s $2"; shift 2 ;;
    --nas) nases="$nases $2"; shift 2 ;;
    --pve) pve=$2; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
done

# JSON from the query API is parsed by python3, never by sed: label values
# carry braces, commas and CJK text (drill labels), which sed gets wrong.
PARSE='
import json, sys
mode = sys.argv[1]
try:
    res = json.load(sys.stdin)["data"]["result"]
except Exception:
    res = []
if mode == "one":
    print(res[0]["value"][1] if res else "-")
else:
    keys = sys.argv[2:]
    for r in res:
        print("\t".join([r["metric"].get(k, "") for k in keys] + [r["value"][1]]))
'
api() { curl -fsS --get "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null || true; }
q() { api "$1" | python3 -c "$PARSE" one; }                 # q EXPR -> first value, or -
qrows() { e=$1; shift; api "$e" | python3 -c "$PARSE" rows "$@"; }  # qrows EXPR LABEL... -> TSV
rs() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$1" "$2" 2>/dev/null | head -1 | grep . || printf -- '-'; }

printf '# verify %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '## 目標\n\n| job | instance | up | 抓取秒數 | 樣本數 |\n|---|---|---|---|---|\n'
qrows 'up' job instance | sort | while IFS='	' read -r job inst v; do
  d=$(q "scrape_duration_seconds{job=\"$job\",instance=\"$inst\"}")
  n=$(q "scrape_samples_scraped{job=\"$job\",instance=\"$inst\"}")
  printf '| %s | %s | %s | %s | %s |\n' "$job" "$inst" "$v" "$d" "$n"
done

printf '\n## 對照\n\n| 來源 | 主機 | Prometheus | 原始工具 | 指令 |\n|---|---|---|---|---|\n'
for g in $gb10s; do
  host=${g#*@}
  p=$(q "gb10_gpu_temp_celsius{node=\"$host\"}")
  t=$(rs "$g" "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits")
  printf '| GPU 溫度 | %s | %s | %s | `nvidia-smi --query-gpu=temperature.gpu` |\n' "$host" "$p" "$t"
  p=$(q "gb10_zone_temp_celsius{node=\"$host\"}")
  t=$(rs "$g" "cat /sys/class/thermal/thermal_zone*/temp | sort -n | tail -1 | awk '{print int(\$1/1000)}'")
  printf '| 最熱 thermal zone | %s | %s | %s | `cat /sys/class/thermal/thermal_zone*/temp` |\n' "$host" "$p" "$t"
  p=$(q "gb10_mem_available_bytes{node=\"$host\"}")
  t=$(rs "$g" "awk '/MemAvailable/{print \$2*1024}' /proc/meminfo")
  printf '| MemAvailable | %s | %s | %s | `/proc/meminfo` |\n' "$host" "$p" "$t"
  p=$(q "gb10_soak_seconds{node=\"$host\"}")
  printf '| soak 秒數 | %s | %s | - | 看 Day 12 負載期間的時間序列 |\n' "$host" "$p"
done
for spec in $nases; do
  label=${spec%%=*}; n=${spec#*=}; ip=${n#*@}
  p=$(q "nas_zpool_capacity_percent{nas=\"$label\",pool=\"zpool1\"}")
  t=$(rs "$n" "zpool list -H -o cap zpool1 | tr -d %")
  printf '| 池使用率 zpool1 | %s | %s | %s | `zpool list -H -o cap zpool1` |\n' "$label" "$p" "$t"
  p=$(q "sum(nas_zfs_snapshots{nas=\"$label\"}) or vector(0)")
  t=$(rs "$n" "zfs list -H -t snapshot -o name | grep -vc ':init:'")
  printf '| 快照數（不含 :init:） | %s | %s | %s | `zfs list -t snapshot` |\n' "$label" "$p" "$t"
  p=$(q "qnap_disk_temperature_celsius{instance=\"$ip\",diskIndex=\"1\"}")
  t=$(rs "$n" "getsysinfo hdtmp 1 | awk '{print \$1}'")
  printf '| 磁碟 1 溫度（SNMP） | %s | %s | %s | `getsysinfo hdtmp 1` |\n' "$label" "$p" "$t"
  p=$(q "qnap_pool_capacity_bytes{instance=\"$ip\",storagepoolIndex=\"1\"}")
  t=$(rs "$n" "zpool list -Hp -o size zpool1")
  printf '| 池大小（SNMP 對 zpool） | %s | %s | %s | `zpool list -Hp -o size zpool1` |\n' "$label" "$p" "$t"
  p=$(q "qnap_cpu_temperature_celsius{instance=\"$ip\"}")
  t=$(rs "$n" "getsysinfo cputmp | awk '{print \$1}'")
  printf '| CPU 溫度（SNMP） | %s | %s | %s | `getsysinfo cputmp` |\n' "$label" "$p" "$t"
done
if [ -n "$pve" ]; then
  ip=${pve#*@}
  node=$(rs "$pve" hostname)
  # pve-exporter answers for the whole cluster from every target; pick one
  # instance so each guest is counted once, and one node to match qm list.
  p=$(q "count(pve_up{instance=\"$ip\",id=~\"qemu/.*\"} * on(id) group_left(node) pve_guest_info{instance=\"$ip\",node=\"$node\"} == 1) or vector(0)")
  t=$(rs "$pve" "qm list | grep -c running")
  printf '| 執行中 VM 數 | %s | %s | %s | `qm list` |\n' "$node" "$p" "$t"
  p=$(q "count(pve_ha_state{instance=\"$ip\",state=\"started\"} == 1) or vector(0)")
  t=$(rs "$pve" "ha-manager status | grep -c started")
  printf '| HA started 資源數 | %s | %s | %s | `ha-manager status` |\n' "$node" "$p" "$t"
  # pve-exporter has no quorate gauge; it is a label on pve_cluster_info
  p=$(qrows "pve_cluster_info{instance=\"$ip\"}" quorate | cut -f1 | head -1 | grep . || printf -- '-')
  t=$(rs "$pve" "pvecm status | awk '/Quorate:/{print \$2}'")
  printf '| 法定人數 | %s | %s | %s | `pvecm status` |\n' "$node" "$p" "$t"
fi
printf '\n## 演練\n\n| source | label | 上次 | 結果 | RTO | RPO | 距今天數 |\n|---|---|---|---|---|---|---|\n'
now=$(date -u +%s)
qrows 'drill_last_timestamp' source label | sort | while IFS='	' read -r s l v; do
  sel="source=\"$s\",label=\"$l\""
  r=$(q "drill_last_result{$sel}")
  rto=$(q "drill_last_rto_seconds{$sel}")
  rpo=$(q "drill_last_rpo_seconds{$sel}")
  age=$(( (now - ${v%.*}) / 86400 ))
  printf '| %s | %s | %s | %s | %s | %s | %s |\n' "$s" "$l" "$(date -u -d "@${v%.*}" +%Y-%m-%d)" "$r" "$rto" "$rpo" "$age"
done
