# onprem-metrics

[English](README.md) | 繁體中文

一套 Prometheus 收一個小型地端 AI 機房的指標，兩台 NVIDIA DGX Spark（GB10）、兩台 QuTS hero 的 QNAP NAS、兩節點加 QDevice 的 Proxmox VE，以及備份那幾天留下的還原演練紀錄。標準 exporter 看得到的交給標準 exporter，看不到的三件事以 node_exporter 的 textfile collector 補上，再加上把目標接在一起的 Prometheus 設定。

| 路徑 | 跑在哪 | 做什麼 |
|---|---|---|
| `textfile/gb10-textfile.py` | 每台 DGX Spark，systemd timer 每 10 秒 | 最熱的 thermal zone、GPU 溫度與功耗與使用率、**熱 soak 秒數**（88 度以上連續秒數）、dmesg 的 `NV_ERR_NO_MEMORY` 計數、MemAvailable、gb10-ops `hw-sample.py` 的 HOT 旗標（只有 hw-sample 以 `--hot-flag /run/gb10/HOT` 在跑時才會出現）。soak 狀態跨次保存，timer 斷掉就歸零 |
| `prometheus/snmp-build.py` 與 `prometheus/snmp.yml` | 收集端 VM | 不靠 generator，直接從 QNAP 的 QTS-MIB（enterprises 55062，QuTS hero）解析 OID 產生 snmp_exporter 模組。磁碟與溫度、SMART 屬性、RAID、儲存池、共享資料夾（WORM、壓縮、去重）、風扇、CPU 與系統溫度、CPU 與記憶體、UPS、韌體、對外服務、已安裝套件，加上 IF-MIB 的網卡計數。只用 SNMP v3，範例是 authPriv（SHA、AES），`security_level` 與協定要跟 NAS 上實際生效的一致，認證放 `snmp-auth.yml`（不入庫） |
| `textfile/nas-textfile.sh` | 收集端主機，cron 每分鐘 | 參數是 `LABEL=user@host`，`nas` 標籤與 SNMP 目標的 `nas` 對得起來。經 ssh 到每台 NAS，只收 MIB 沒有的東西，依名稱列的 ZFS 池、資料集已用、每資料集快照數與最新最舊時間與佔用、HBS 3 是否安裝。NAS 上不裝任何東西 |
| `textfile/drills-textfile.py` | 收集端主機，cron 每 15 分鐘 | 讀 pve-backup-drill、nas-backup-drill、cloud-offload-drill 的 `drills.jsonl`，每個演練標籤只留最近一次，時間、結果、RTO、RPO、速率、最近一次成功 |
| `prometheus/` | 收集端 VM | `prometheus.yml` 三種間隔（GPU 節點 15 秒，NAS 與 PVE 60 秒），file_sd 目標檔（`.example` 入庫，真實檔忽略），pve-exporter 的 `pve.yml.example`，`rules/staleness.yml` 的管線存活告警 |
| `docker-compose.yml` | 收集端 VM | Prometheus 3.5 保留 90 天，加 pve-exporter 與 snmp-exporter，映像以 digest 釘住。兩個 exporter 只發佈到 127.0.0.1，方便在主機上 curl 除錯 |
| `systemd/` | DGX Spark 與收集端 | node_exporter 單元、gb10-textfile 的 service 與 timer、收集端的 cron.d |
| `install-node.sh` | 每台 DGX Spark；`--collector` 給收集端 | 下載 node_exporter（arm64 或 amd64），對 release 的 sha256 校驗，安裝單元。DGX Spark 模式另裝 gb10-textfile 與 `/run/gb10`（tmpfiles.d）；`--collector` 只裝 node_exporter 與 textfile 目錄 |
| `verify.sh` | 任何有 curl、ssh 與 python3 的機器 | 每個目標的 `up`、抓取秒數與樣本數，每個來源一個 Prometheus 的值與原始工具的值並列，演練表。NAS 端只用一般帳號讀得到的來源（`getsysinfo` 的溫度要 root） |
| `tests/` | CI | 假的 sysfs、nvidia-smi、dmesg、zfs、zpool、getsysinfo，每份輸出都過 `promtool check metrics` |

## 為什麼硬體走 SNMP、ZFS 走 ssh

收集端持有的 ssh 金鑰是 NAS 上的一個 shell。SNMP v3 唯讀是窄得多的介面，QTS MIB 答得出來的都走它，ssh 縮到 `zfs list` 與 HBS 3 狀態這兩件 MIB 沒有的事。`snmp-build.py` 讀 QNAP 附的 MIB 產生模組，`snmp_exporter --dry-run` 是測試。

## 為什麼是 textfile

soak 計數要記住上一次的狀態，NAS 沒有套件管理器，演練紀錄是三個 repo 裡的檔案。textfile 是最簡單的契約，原子地寫一個檔，node_exporter 端出去，Prometheus 不需要知道差別。每個檔都帶 `*_last_run_timestamp`，timer 死掉會變成過期的指標，而不會變成看起來正常的凍結數值。

## 快速開始

```sh
# 收集端 VM
cp prometheus/targets/gb10.yml.example prometheus/targets/gb10.yml      # 改位址
cp prometheus/targets/collector.yml.example prometheus/targets/collector.yml
cp prometheus/targets/pve.yml.example prometheus/targets/pve.yml
cp prometheus/targets/nas-snmp.yml.example prometheus/targets/nas-snmp.yml
cp prometheus/pve.yml.example prometheus/pve.yml                           # 填 token
cp prometheus/snmp-auth.yml.example prometheus/snmp-auth.yml               # 填 SNMP v3 密語（至少 8 字元）
sudo chown root:101 prometheus/pve.yml && sudo chmod 640 prometheus/pve.yml               # pve-exporter 以 uid/gid 101 執行
sudo chown root:65534 prometheus/snmp-auth.yml && sudo chmod 640 prometheus/snmp-auth.yml # snmp-exporter 以 nobody 執行
promtool check config prometheus/prometheus.yml
docker compose up -d
sudo ./install-node.sh --collector
sudo install -m 0644 systemd/collector.cron /etc/cron.d/onprem-metrics    # 改主機與路徑

# 第一次 SNMP 抓取，看實機有哪些 OID 回值
curl 'http://localhost:9116/snmp?module=qnap&auth=nas_v3&target=192.168.2.2'

# 每台 DGX Spark
sudo ./install-node.sh
sudo ufw allow proto tcp from 192.168.2.49 to any port 9100   # 只放收集端
curl -s localhost:9100/metrics | grep ^gb10_

# 然後
PROM=http://192.168.2.49:9090 ./verify.sh \
  --gb10 user@192.168.2.131 --gb10 user@192.168.2.141 \
  --nas primary=claude@192.168.2.2 --nas secondary=claude@192.168.2.22 \
  --pve root@192.168.2.9 > verify.md
```

## 測試

```sh
python3 -m unittest discover -s tests
sh tests/fake-nas.sh
shellcheck textfile/nas-textfile.sh verify.sh install-node.sh tests/fake-nas.sh
promtool check config prometheus/prometheus.yml && promtool check rules prometheus/rules/staleness.yml
snmp_exporter --config.file=prometheus/snmp.yml --config.file=prometheus/snmp-auth.yml.example --dry-run
```

## 授權

Apache-2.0
