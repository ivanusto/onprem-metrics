#!/bin/sh
# verify: is every target up, how long do scrapes take, and does one
# number per source match the tool it came from. Prints Markdown.
#
#   PROM=http://192.168.2.9:9090 ./verify.sh [--gb10 user@node] [--nas user@nas] [--pve user@pve]
#
# The cross-checks ssh to the source and run the original command, so the
# table in the article is "Prometheus says X, the box says Y" with both
# taken within a few seconds of each other. Any check you cannot run is
# printed as "-" rather than skipped, so the gaps are visible.
# shellcheck disable=SC2016
set -eu
PROM=${PROM:-http://localhost:9090}
gb10=; nas=; pve=
while [ $# -gt 0 ]; do
  case "$1" in
    --gb10) gb10=$2; shift 2 ;;
    --nas) nas=$2; shift 2 ;;
    --pve) pve=$2; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
done

q() { # q EXPR -> first value, or -
  curl -fsS --get "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null \
    | sed -n 's/.*"value":\[[0-9.]*,"\([^"]*\)".*/\1/p' | head -1 | grep . || printf -- '-'
}
qrows() { # qrows EXPR -> "labels value" per series
  curl -fsS --get "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null \
    | tr '{' '\n' | sed -n 's/^"metric":{\([^}]*\)},"value":\[[0-9.]*,"\([^"]*\)".*/\1 \2/p'
}
rs() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$1" "$2" 2>/dev/null || printf -- '-'; }

printf '# verify %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '## 目標\n\n| job | instance | up | 抓取秒數 | 樣本數 |\n|---|---|---|---|---|\n'
qrows 'up' | while read -r labels v; do
  job=$(printf '%s' "$labels" | sed -n 's/.*"job":"\([^"]*\)".*/\1/p')
  inst=$(printf '%s' "$labels" | sed -n 's/.*"instance":"\([^"]*\)".*/\1/p')
  d=$(q "scrape_duration_seconds{job=\"$job\",instance=\"$inst\"}")
  n=$(q "scrape_samples_scraped{job=\"$job\",instance=\"$inst\"}")
  printf '| %s | %s | %s | %s | %s |\n' "$job" "$inst" "$v" "$d" "$n"
done

printf '\n## 對照\n\n| 來源 | Prometheus | 原始工具 | 指令 |\n|---|---|---|---|\n'
if [ -n "$gb10" ]; then
  host=${gb10#*@}
  p=$(q "gb10_gpu_temp_celsius{node=\"$host\"}")
  t=$(rs "$gb10" "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits")
  printf '| GPU 溫度 | %s | %s | `nvidia-smi --query-gpu=temperature.gpu` |\n' "$p" "$t"
  p=$(q "gb10_zone_temp_celsius{node=\"$host\"}")
  t=$(rs "$gb10" "cat /sys/class/thermal/thermal_zone*/temp | sort -n | tail -1 | awk '{print int(\$1/1000)}'")
  printf '| 最熱 thermal zone | %s | %s | `cat /sys/class/thermal/thermal_zone*/temp` |\n' "$p" "$t"
  p=$(q "gb10_mem_available_bytes{node=\"$host\"}")
  t=$(rs "$gb10" "awk '/MemAvailable/{print \$2*1024}' /proc/meminfo")
  printf '| MemAvailable | %s | %s | `/proc/meminfo` |\n' "$p" "$t"
  p=$(q "gb10_soak_seconds{node=\"$host\"}")
  printf '| soak 秒數 | %s | - | 看 Day 12 負載期間的時間序列 |\n' "$p"
fi
if [ -n "$nas" ]; then
  p=$(q 'nas_zpool_capacity_percent{pool="zpool1"}')
  t=$(rs "$nas" "zpool list -H -o cap zpool1")
  printf '| 池使用率 zpool1 | %s | %s | `zpool list -H -o cap zpool1` |\n' "$p" "$t"
  p=$(q 'sum(nas_zfs_snapshots)')
  t=$(rs "$nas" "zfs list -H -t snapshot -o name | grep -vc ':init:'")
  printf '| 快照數（不含 :init:） | %s | %s | `zfs list -t snapshot` |\n' "$p" "$t"
  p=$(q 'qnap_disk_temperature_celsius{diskIndex="1"}')
  t=$(rs "$nas" "getsysinfo hdtmp 1 | awk '{print \$1}'")
  printf '| 磁碟 1 溫度（SNMP） | %s | %s | `getsysinfo hdtmp 1` |\n' "$p" "$t"
  p=$(q 'qnap_pool_capacity_bytes{storagepoolIndex="1"}')
  t=$(rs "$nas" "zpool list -Hp -o size zpool1")
  printf '| 池大小（SNMP 對 zpool） | %s | %s | `zpool list -Hp -o size zpool1` |\n' "$p" "$t"
  p=$(q 'qnap_cpu_temperature_celsius')
  t=$(rs "$nas" "getsysinfo cputmp | awk '{print \$1}'")
  printf '| CPU 溫度（SNMP） | %s | %s | `getsysinfo cputmp` |\n' "$p" "$t"
fi
if [ -n "$pve" ]; then
  p=$(q 'count(pve_up{id=~"qemu/.*"} == 1)')
  t=$(rs "$pve" "qm list | grep -c running")
  printf '| 執行中 VM 數 | %s | %s | `qm list` |\n' "$p" "$t"
  p=$(q 'count(pve_ha_state{state="started"}) or vector(0)')
  t=$(rs "$pve" "ha-manager status | grep -c started")
  printf '| HA started 資源數 | %s | %s | `ha-manager status` |\n' "$p" "$t"
  p=$(q 'pve_cluster_quorate')
  t=$(rs "$pve" "pvecm status | awk '/Quorate:/{print \$2}'")
  printf '| 法定人數 | %s | %s | `pvecm status` |\n' "$p" "$t"
fi
printf '\n## 演練\n\n| source | label | 上次 | 結果 | RTO | RPO | 距今天數 |\n|---|---|---|---|---|---|---|\n'
qrows 'drill_last_timestamp' | while read -r labels v; do
  s=$(printf '%s' "$labels" | sed -n 's/.*"source":"\([^"]*\)".*/\1/p')
  l=$(printf '%s' "$labels" | sed -n 's/.*"label":"\([^"]*\)".*/\1/p')
  r=$(q "drill_last_result{source=\"$s\",label=\"$l\"}")
  rto=$(q "drill_last_rto_seconds{source=\"$s\",label=\"$l\"}")
  rpo=$(q "drill_last_rpo_seconds{source=\"$s\",label=\"$l\"}")
  age=$(( ( $(date -u +%s) - ${v%.*} ) / 86400 ))
  printf '| %s | %s | %s | %s | %s | %s | %s |\n' "$s" "$l" "$(date -u -d "@${v%.*}" +%Y-%m-%d)" "$r" "$rto" "$rpo" "$age"
done
