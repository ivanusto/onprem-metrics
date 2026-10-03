#!/bin/sh
# nas-textfile: QuTS hero pools, datasets, snapshots, HBS 3 jobs and disk
# temperatures as node_exporter textfile metrics, collected over ssh.
#
# QNAP has no node_exporter and SNMP is off in this lab, so one Linux host
# runs this from cron for every NAS and exposes the result through its own
# node_exporter. Same approach as nas-audit.sh (Day 17): the script is piped
# to the remote sh, nothing is copied to the NAS, a non-root key is enough
# for zfs/zpool. Hardware (disk temperature, SMART, fans, pools by SNMP
# index) comes from snmp_exporter instead; this script is only for what
# the QTS MIB does not have: ZFS datasets, snapshots and HBS 3.
#
#   nas-textfile.sh NAS_LABEL user@host [user@host2 ...] > /var/lib/node_exporter/textfile/nas.prom
#   NAS_TEXTFILE_LOCAL=1 nas-textfile.sh NAS_LABEL                    # on the NAS itself
#
# Each host's output is tagged nas="LABEL"; the label for the second and
# later hosts is taken from the host part of user@host. Output is written
# to stdout so cron can redirect it atomically through a temp file (see
# systemd/nas-textfile.sh wrapper).
#
# Metrics (all gauges unless noted)
#   nas_up{nas}                               1 when ssh and zfs answered, 0 otherwise
#   nas_zpool_size_bytes{nas,pool}            zpool list -p
#   nas_zpool_alloc_bytes{nas,pool}
#   nas_zpool_capacity_percent{nas,pool}
#   nas_zpool_health{nas,pool,state}          1 for the current state
#   nas_zfs_used_bytes{nas,dataset}           zfs list -p, filesystems and volumes
#   nas_zfs_snapshots{nas,dataset}       excluding :init:
#   nas_zfs_snapshot_newest_timestamp{nas,dataset}
#   nas_zfs_snapshot_oldest_timestamp{nas,dataset}
#   nas_zfs_snapshot_used_bytes{nas,dataset}
#   nas_hbs_installed{nas}                    1 when HybridBackup is in qpkg.conf
#   nas_hbs_job_last_result{nas,job,result}   1 for the last result (待查: log path)
#   nas_textfile_last_run_timestamp{nas}
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

header() {
  cat <<'EOF'
# HELP nas_up 1 when ssh and zfs answered
# TYPE nas_up gauge
# HELP nas_zpool_size_bytes Pool size
# TYPE nas_zpool_size_bytes gauge
# HELP nas_zpool_alloc_bytes Pool allocated bytes
# TYPE nas_zpool_alloc_bytes gauge
# HELP nas_zpool_capacity_percent Pool capacity used, percent
# TYPE nas_zpool_capacity_percent gauge
# HELP nas_zpool_health 1 for the current pool state
# TYPE nas_zpool_health gauge
# HELP nas_zfs_used_bytes Dataset used bytes
# TYPE nas_zfs_used_bytes gauge
# HELP nas_zfs_snapshots Snapshots per dataset, excluding :init:
# TYPE nas_zfs_snapshots gauge
# HELP nas_zfs_snapshot_used_bytes Space held by snapshots per dataset
# TYPE nas_zfs_snapshot_used_bytes gauge
# HELP nas_zfs_snapshot_newest_timestamp Creation time of the newest snapshot
# TYPE nas_zfs_snapshot_newest_timestamp gauge
# HELP nas_zfs_snapshot_oldest_timestamp Creation time of the oldest snapshot
# TYPE nas_zfs_snapshot_oldest_timestamp gauge
# HELP nas_hbs_installed 1 when HBS 3 is installed
# TYPE nas_hbs_installed gauge
# HELP nas_textfile_last_run_timestamp Unix time of the last run per NAS
# TYPE nas_textfile_last_run_timestamp gauge
EOF
}

[ $# -ge 1 ] || { sed -n '2,15p' "$0"; exit 1; }
label=$1; shift
header
if [ -n "${NAS_TEXTFILE_LOCAL:-}" ]; then
  sh -c "$remote" nas "$label"
  exit 0
fi
[ $# -ge 1 ] || { printf 'nas-textfile: need at least one user@host\n' >&2; exit 1; }
first=1
for h in "$@"; do
  if [ "$first" = 1 ]; then l=$label; first=0; else l=${h#*@}; fi
  if ! printf '%s\n' "$remote" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$h" sh -s "$l" 2>/dev/null; then
    printf 'nas_up{nas="%s"} 0\n' "$l"
  fi
done
