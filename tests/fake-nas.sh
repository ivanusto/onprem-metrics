#!/bin/sh
# nas-textfile.sh against fake zfs/zpool/getcfg, then promtool.
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"
now=$(date +%s)
cat > "$T/bin/zpool" <<EOT
#!/bin/sh
printf 'zpool1\t24855000000000\t6500000000000\t26%%\tONLINE\n'
EOT
cat > "$T/bin/zfs" <<EOT
#!/bin/sh
case "\$*" in
  *"-t snapshot"*)
    printf 'zpool1/zfs263@snapshot:init:\t$((now-8640000))\t0\n'
    printf 'zpool1/zfs263@GMT-2026.10.01-01.00.00\t$((now-7200))\t123456789\n'
    printf 'zpool1/zfs263@GMT-2026.10.01-02.00.00\t$((now-3600))\t2345678\n'
    printf 'zpool1/zfs264@snapshot:init:\t$((now-8640000))\t0\n' ;;
  *"-t filesystem,volume"*)
    printf 'zpool1/zfs263\t3640000000000\n'
    printf 'zpool1/zfs264\t24000000000\n' ;;
esac
EOT
cat > "$T/bin/getcfg" <<'EOT'
#!/bin/sh
[ "$1 $2" = "HybridBackup Version" ] && echo 26.4.4.788
EOT
chmod +x "$T"/bin/*
out=$(PATH="$T/bin:$PATH" NAS_TEXTFILE_LOCAL=1 "$HERE/textfile/nas-textfile.sh" primary)
printf '%s\n' "$out" > "$T/nas.prom"
pass=0; fail=0
check() { if printf '%s\n' "$out" | grep -q "$2"; then pass=$((pass+1)); echo "ok   $1"; else fail=$((fail+1)); echo "FAIL $1"; fi; }
check "nas_up" '^nas_up{nas="primary"} 1$'
check "pool capacity without percent sign" '^nas_zpool_capacity_percent{nas="primary",pool="zpool1"} 26$'
check "pool health label" '^nas_zpool_health{nas="primary",pool="zpool1",state="ONLINE"} 1$'
check "snapshot count excludes :init:" '^nas_zfs_snapshots{nas="primary",dataset="zpool1/zfs263"} 2$'
if printf '%s\n' "$out" | grep -q 'nas_zfs_snapshots{nas="primary",dataset="zpool1/zfs264"}'; then fail=$((fail+1)); echo "FAIL zfs264 count present"; else pass=$((pass+1)); echo "ok   zfs264 has no snapshot_count"; fi
check "snapshot used bytes summed" '^nas_zfs_snapshot_used_bytes{nas="primary",dataset="zpool1/zfs263"} 125802467$'
check "hbs installed" '^nas_hbs_installed{nas="primary"} 1$'
if printf '%s\n' "$out" | grep -q 'temp_celsius'; then fail=$((fail+1)); echo "FAIL temperature still in textfile (belongs to SNMP)"; else pass=$((pass+1)); echo "ok   no temperature in textfile"; fi
if command -v promtool >/dev/null 2>&1; then
  if promtool check metrics < "$T/nas.prom" >/dev/null 2>&1; then pass=$((pass+1)); echo "ok   promtool check metrics"; else fail=$((fail+1)); echo "FAIL promtool"; promtool check metrics < "$T/nas.prom"; fi
fi
# unreachable host path: ssh to a bogus host prints nas_up 0
out2=$("$HERE/textfile/nas-textfile.sh" primary=nobody@127.0.0.1 2>/dev/null || true)
if printf '%s\n' "$out2" | grep -q '^nas_up{nas="primary"} 0$'; then pass=$((pass+1)); echo "ok   unreachable host reports nas_up 0"; else fail=$((fail+1)); echo "FAIL unreachable"; fi
# two NAS through a fake ssh that runs the piped script locally: labels come
# from LABEL=, and every family is one contiguous block with one HELP line
cat > "$T/bin/ssh" <<'EOT'
#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; sh) shift 2; break ;; *) shift ;; esac; done
exec sh -s "$@"
EOT
chmod +x "$T/bin/ssh"
out3=$(PATH="$T/bin:$PATH" "$HERE/textfile/nas-textfile.sh" primary=claude@nas1 secondary=claude@nas2)
printf '%s\n' "$out3" > "$T/two.prom"
out=$out3
check "second NAS keeps its label" '^nas_up{nas="secondary"} 1$'
if [ "$(printf '%s\n' "$out3" | grep -c '^# HELP nas_up ')" = 1 ]; then pass=$((pass+1)); echo "ok   one HELP per family"; else fail=$((fail+1)); echo "FAIL HELP repeated"; fi
if printf '%s\n' "$out3" | grep -v '^#' | sed 's/[{ ].*//' | uniq | sort | uniq -d | grep -q .; then fail=$((fail+1)); echo "FAIL family not contiguous"; else pass=$((pass+1)); echo "ok   families contiguous"; fi
if command -v promtool >/dev/null 2>&1; then
  if promtool check metrics < "$T/two.prom" >/dev/null 2>&1; then pass=$((pass+1)); echo "ok   promtool two NAS"; else fail=$((fail+1)); echo "FAIL promtool two NAS"; promtool check metrics < "$T/two.prom"; fi
fi
if "$HERE/textfile/nas-textfile.sh" claude@nas1 >/dev/null 2>&1; then fail=$((fail+1)); echo "FAIL bare user@host accepted"; else pass=$((pass+1)); echo "ok   bare user@host rejected"; fi
printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
