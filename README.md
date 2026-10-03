# onprem-metrics

English | [繁體中文](README.zh-TW.md)

One Prometheus for a small on-prem AI lab: two NVIDIA DGX Spark (GB10) nodes, two QNAP NAS on QuTS hero, a three-node Proxmox VE cluster, and the restore drills from the backup days. Standard exporters cover most of it. This repo adds the three things they do not see, as node_exporter textfile collectors, plus the Prometheus config that ties the targets together.

| Path | Runs on | What it does |
|---|---|---|
| `textfile/gb10-textfile.py` | each DGX Spark, systemd timer every 10 s | Hottest thermal zone, GPU temp/power/util, **thermal soak seconds** (continuous time at or above 88 C), `NV_ERR_NO_MEMORY` count from dmesg, MemAvailable, the HOT flag from gb10-ops. Soak state survives between runs; a timer gap resets it |
| `prometheus/snmp-build.py` and `prometheus/snmp.yml` | collector VM | Builds the snmp_exporter module from QNAP's QTS-MIB (enterprises 55062, QuTS hero) without the generator: disks and temperature, SMART attributes, RAID, pools, shared folders (WORM, compression, dedup), fans, CPU and system temperature, CPU and memory, UPS, firmware, exposed services, installed apps, plus IF-MIB interface counters. SNMP v3 authPriv only, auth in `snmp-auth.yml` (gitignored) |
| `textfile/nas-textfile.sh` | the collector host, cron every minute | Over ssh to each NAS, only what the MIB does not have: ZFS pool list by name, dataset used, snapshots per dataset (count, newest, oldest, bytes), HBS 3 installed. Nothing is installed on the NAS |
| `textfile/drills-textfile.py` | the collector host, cron every 15 min | Reads `drills.jsonl` from pve-backup-drill, nas-backup-drill and cloud-offload-drill, exposes the latest drill per label: timestamp, result, RTO, RPO, rate, last success |
| `prometheus/` | collector VM | `prometheus.yml` with three intervals (15 s GPU nodes, 60 s NAS and PVE), file_sd target files (`.example` committed, real ones ignored), `pve.yml.example` for prometheus-pve-exporter, `rules/staleness.yml` for pipeline-alive alerts |
| `docker-compose.yml` | collector VM | Prometheus 3.5 with 90 d retention plus pve-exporter; snmp-exporter commented out until SNMP is on |
| `systemd/` | DGX Spark and collector | node_exporter unit, gb10-textfile service and timer, cron.d lines for the collector |
| `install-node.sh` | each DGX Spark | Downloads node_exporter (arm64 or amd64), verifies sha256 against the release, installs the units |
| `verify.sh` | anywhere with curl and ssh | `up` and scrape duration per target, one Prometheus value next to the original tool's value per source, the drill table |
| `tests/` | CI | Fake sysfs, nvidia-smi, dmesg, zfs, zpool, getsysinfo; every output is checked with `promtool check metrics` |

## Why SNMP for hardware and ssh for ZFS

The ssh key the collector holds is a shell on the NAS. SNMP v3 read-only is a much narrower interface, so everything the QTS MIB can answer goes that way and the ssh path shrinks to `zfs list` and the HBS 3 state, which the MIB does not expose. `snmp-build.py` reads the MIB QNAP ships and writes the module; `snmp_exporter --dry-run` is the test.

## Why textfile and not another exporter

The soak counter needs memory between samples, the NAS has no package manager and the drill logs are files in three repos. A textfile is the simplest contract: write a file atomically, node_exporter serves it, Prometheus never knows the difference. Each file also carries a `*_last_run_timestamp` so a dead timer shows up as a stale metric, not as a frozen value that looks fine.

## Quick start

```sh
# collector VM
cp prometheus/targets/gb10.yml.example prometheus/targets/gb10.yml      # edit addresses
cp prometheus/targets/collector.yml.example prometheus/targets/collector.yml
cp prometheus/targets/pve.yml.example prometheus/targets/pve.yml
cp prometheus/targets/nas-snmp.yml.example prometheus/targets/nas-snmp.yml
cp prometheus/pve.yml.example prometheus/pve.yml                           # token
cp prometheus/snmp-auth.yml.example prometheus/snmp-auth.yml               # SNMP v3 passphrases
promtool check config prometheus/prometheus.yml
docker compose up -d
sudo install -m 0644 systemd/collector.cron /etc/cron.d/onprem-metrics    # edit hosts and paths

# each DGX Spark
sudo ./install-node.sh

# then
PROM=http://collector:9090 ./verify.sh --gb10 user@spark1 --nas claude@nas1 --pve root@pve1 > verify.md
```

## Metrics added

`gb10_zone_temp_celsius`, `gb10_gpu_temp_celsius`, `gb10_gpu_power_watts`, `gb10_gpu_utilization_percent`, `gb10_soak_seconds`, `gb10_soak_threshold_celsius`, `gb10_soak_budget_seconds`, `gb10_hot`, `gb10_mem_available_bytes`, `gb10_nv_err_no_memory_total`, `gb10_textfile_errors`, `gb10_textfile_last_run_timestamp`

`nas_up`, `nas_zpool_size_bytes`, `nas_zpool_alloc_bytes`, `nas_zpool_capacity_percent`, `nas_zpool_health`, `nas_zfs_used_bytes`, `nas_zfs_snapshots`, `nas_zfs_snapshot_used_bytes`, `nas_zfs_snapshot_newest_timestamp`, `nas_zfs_snapshot_oldest_timestamp`, `nas_hbs_installed`, `nas_textfile_last_run_timestamp`

SNMP (`qnap_*`, 48 metrics, see `prometheus/snmp.yml`): `qnap_disk_temperature_celsius`, `qnap_disk_status`, `qnap_smart_attribute_*`, `qnap_raid_*`, `qnap_pool_*`, `qnap_folder_*`, `qnap_fan_speed_rpm`, `qnap_cpu_temperature_celsius`, `qnap_system_temperature_celsius`, `qnap_cpu_usage_percent`, `qnap_memory_*`, `qnap_ups_*`, `qnap_firmware_*`, `qnap_service_*_enabled`, `qnap_app_info`, `ifHCInOctets`, `ifHCOutOctets`

`drill_last_timestamp`, `drill_last_result`, `drill_last_rto_seconds`, `drill_last_rpo_seconds`, `drill_last_rate_mib_per_second`, `drill_last_success_timestamp`, `drill_count_total`, `drills_textfile_last_run_timestamp`

## Tests

```sh
python3 -m unittest discover -s tests
sh tests/fake-nas.sh
shellcheck textfile/nas-textfile.sh verify.sh install-node.sh tests/fake-nas.sh
promtool check config prometheus/prometheus.yml && promtool check rules prometheus/rules/staleness.yml
snmp_exporter --config.file=prometheus/snmp.yml --config.file=prometheus/snmp-auth.yml.example --dry-run
```

## License

Apache-2.0
