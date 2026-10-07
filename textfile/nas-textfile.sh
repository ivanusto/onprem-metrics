#!/bin/sh
# nas-textfile: what the QTS MIB does not have (ZFS pools by name, datasets,
# snapshots, HBS 3) as node_exporter textfile metrics, collected over ssh.
#
# QNAP has no node_exporter, so one Linux host (the collector VM) runs this
# from cron for every NAS and exposes the result through its own
# node_exporter. Same approach as nas-audit.sh (Day 17): the script is piped
# to the remote sh, nothing is copied to the NAS, a non-root key is enough
# for zfs/zpool. Hardware (disk temperature, SMART, fans, pools by SNMP
# index) comes from snmp_exporter instead.
#
#   nas-textfile.sh LABEL=user@host [LABEL=user@host ...] > nas.prom
#   NAS_TEXTFILE_LOCAL=1 nas-textfile.sh LABEL              # on the NAS itself
#
# Use the same LABEL as the nas: label in targets/nas-snmp.yml so the SNMP
# and ssh series of one NAS join on nas="LABEL". Output goes to stdout;
# cron writes it through a temp file and mv (see systemd/collector.cron).
# Samples of every host are regrouped per metric family, because the text
# format wants one contiguous block per metric name.
#
# Metrics (all gauges unless noted)
#   nas_up{nas}                               1 when ssh and zfs answered, 0 otherwise
#   nas_zpool_size_bytes{nas,pool}            zpool list -p
#   nas_zpool_alloc_bytes{nas,pool}
#   nas_zpool_capacity_percent{nas,pool}
#   nas_zpool_health{nas,pool,state}          1 for the current state
#   nas_zfs_used_bytes{nas,dataset}           zfs list -p, filesystems and volumes
#   nas_zfs_snapshots{nas,dataset}           excluding :init:
#   nas_zfs_snapshot_newest_timestamp{nas,dataset}
#   nas_zfs_snapshot_oldest_timestamp{nas,dataset}
#   nas_zfs_snapshot_used_bytes{nas,dataset}
#   nas_hbs_installed{nas}                    1 when HybridBackup is in qpkg.conf
#   nas_buddyinfo_free_blocks{nas,node,zone,order}  /proc/buddyinfo, free blocks of 2^order pages
#   nas_memory_free_bytes{nas,zone}           free memory per zone, summed from buddyinfo
#   nas_memory_free_highorder_ratio{nas,zone} share of that free memory in blocks of order 9 (2 MiB) and up
#   nas_boot_time_seconds{nas}                btime from /proc/stat
#   nas_textfile_last_run_timestamp{nas}
#
# buddyinfo is here because a GPU on the NAS stops initialising once host
# memory is fragmented: the NVIDIA driver wants physically contiguous
# blocks, MemFree can be 15 GB while every high order is empty, and only a
# reboot brings them back. free(1) and the QTS MIB do not show it.
# The block counts alone fall whenever memory is in use, fragmented or not,
# so the alert reads the ratio: the share of free memory still in 2 MiB or
# larger blocks.
set -eu

# shellcheck disable=SC2016
remote='
set -u
L="$1"
now=$(date -u +%s)
esc() { printf "%s" "$1" | sed "s/\\\\/\\\\\\\\/g; s/\"/\\\\\"/g"; }
ok=0
if command -v zpool >/dev/null 2>&1 && zpool list -Hp -o name >/dev/null 2>&1; then ok=1; fi
printf "nas_up{nas=\"%s\"} %s\n" "$L" "$ok"

# Memory state does not depend on zfs, so it is reported even when nas_up is 0.
B="${NAS_BUDDYINFO:-/proc/buddyinfo}"
ps=$(getconf PAGESIZE 2>/dev/null || echo 4096)
[ -r "$B" ] && awk -v L="$L" -v PS="${ps:-4096}" "
  \$1 == \"Node\" { n=\$2; sub(/,/, \"\", n); z=\$4
    for (i = 5; i <= NF; i++) {
      o = i - 5; p = \$i * 2^o
      printf \"nas_buddyinfo_free_blocks{nas=\\\"%s\\\",node=\\\"%s\\\",zone=\\\"%s\\\",order=\\\"%d\\\"} %s\n\", L, n, z, o, \$i
      tot[z] += p; if (o >= 9) hi[z] += p } }
  END { for (z in tot) {
    printf \"nas_memory_free_bytes{nas=\\\"%s\\\",zone=\\\"%s\\\"} %.0f\n\", L, z, tot[z] * PS
    printf \"nas_memory_free_highorder_ratio{nas=\\\"%s\\\",zone=\\\"%s\\\"} %.6f\n\", L, z, tot[z] ? hi[z] / tot[z] : 0 } }" "$B"
bt=$(awk "/^btime / { print \$2 }" "${NAS_PROC_STAT:-/proc/stat}" 2>/dev/null)
[ -n "$bt" ] && printf "nas_boot_time_seconds{nas=\"%s\"} %s\n" "$L" "$bt"

[ "$ok" = 1 ] || exit 0

zpool list -Hp -o name,size,alloc,cap,health 2>/dev/null | while IFS="	" read -r name size alloc cap health; do
  printf "nas_zpool_size_bytes{nas=\"%s\",pool=\"%s\"} %s\n" "$L" "$name" "$size"
  printf "nas_zpool_alloc_bytes{nas=\"%s\",pool=\"%s\"} %s\n" "$L" "$name" "$alloc"
  printf "nas_zpool_capacity_percent{nas=\"%s\",pool=\"%s\"} %s\n" "$L" "$name" "${cap%\%}"
  printf "nas_zpool_health{nas=\"%s\",pool=\"%s\",state=\"%s\"} 1\n" "$L" "$name" "$health"
done

zfs list -Hp -t filesystem,volume -o name,used 2>/dev/null | while IFS="	" read -r name used; do
  printf "nas_zfs_used_bytes{nas=\"%s\",dataset=\"%s\"} %s\n" "$L" "$(esc "$name")" "$used"
done

zfs list -Hp -t snapshot -o name,creation,used 2>/dev/null | awk -F"\t" -v L="$L" "
  { split(\$1, a, \"@\"); ds=a[1]; sn=a[2]
    if (sn ~ /:init:/) next
    n[ds]++; u[ds]+=\$3
    if (!(ds in old) || \$2<old[ds]) old[ds]=\$2
    if (\$2>new[ds]) new[ds]=\$2 }
  END { for (ds in n) {
    printf \"nas_zfs_snapshots{nas=\\\"%s\\\",dataset=\\\"%s\\\"} %d\n\", L, ds, n[ds]
    printf \"nas_zfs_snapshot_used_bytes{nas=\\\"%s\\\",dataset=\\\"%s\\\"} %d\n\", L, ds, u[ds]
    printf \"nas_zfs_snapshot_newest_timestamp{nas=\\\"%s\\\",dataset=\\\"%s\\\"} %d\n\", L, ds, new[ds]
    printf \"nas_zfs_snapshot_oldest_timestamp{nas=\\\"%s\\\",dataset=\\\"%s\\\"} %d\n\", L, ds, old[ds] } }"

hbs=0
[ -n "$(getcfg HybridBackup Version -f /etc/config/qpkg.conf 2>/dev/null)" ] && hbs=1
printf "nas_hbs_installed{nas=\"%s\"} %s\n" "$L" "$hbs"

printf "nas_textfile_last_run_timestamp{nas=\"%s\"} %s\n" "$L" "$now"
exit 0
'

# name|help, in output order
FAMILIES='nas_up|1 when ssh and zfs answered
nas_zpool_size_bytes|Pool size
nas_zpool_alloc_bytes|Pool allocated bytes
nas_zpool_capacity_percent|Pool capacity used, percent
nas_zpool_health|1 for the current pool state
nas_zfs_used_bytes|Dataset used bytes
nas_zfs_snapshots|Snapshots per dataset, excluding :init:
nas_zfs_snapshot_used_bytes|Space held by snapshots per dataset
nas_zfs_snapshot_newest_timestamp|Creation time of the newest snapshot
nas_zfs_snapshot_oldest_timestamp|Creation time of the oldest snapshot
nas_hbs_installed|1 when HBS 3 is installed
nas_buddyinfo_free_blocks|Free blocks of 2^order pages per zone, from /proc/buddyinfo
nas_memory_free_bytes|Free memory per zone, summed from /proc/buddyinfo
nas_memory_free_highorder_ratio|Share of free memory per zone in blocks of order 9 (2 MiB) and up
nas_boot_time_seconds|Unix time the NAS booted
nas_textfile_last_run_timestamp|Unix time of the last run per NAS'

group() { # stdin: samples in any order -> stdout: HELP/TYPE + contiguous family
  awk -v fams="$FAMILIES" '
    { name = $0; sub(/[{ ].*/, "", name); body[name] = body[name] $0 "\n" }
    END { n = split(fams, f, "\n")
      for (i = 1; i <= n; i++) { split(f[i], kv, "|")
        if (!(kv[1] in body)) continue
        printf "# HELP %s %s\n# TYPE %s gauge\n%s", kv[1], kv[2], kv[1], body[kv[1]] } }'
}

[ $# -ge 1 ] || { sed -n '2,20p' "$0"; exit 1; }
if [ -n "${NAS_TEXTFILE_LOCAL:-}" ]; then
  sh -c "$remote" nas "$1" | group
  exit 0
fi
for spec in "$@"; do
  case "$spec" in *=*@*) ;; *) printf 'nas-textfile: expected LABEL=user@host, got %s\n' "$spec" >&2; exit 1 ;; esac
done
for spec in "$@"; do
  l=${spec%%=*}; h=${spec#*=}
  if ! printf '%s\n' "$remote" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$h" sh -s "$l" 2>/dev/null; then
    printf 'nas_up{nas="%s"} 0\n' "$l"
  fi
done | group
