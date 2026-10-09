# 資產生命週期（Day 25）

`assets.tsv` 是這個場域裡「什麼東西有一個會到期的日子」的清單，一列一個（資產、元件、里程碑）。欄位說明在檔頭的註解。`textfile/eol-textfile.py` 每天讀它一次，寫成 `asset_eol_timestamp_seconds`，Prometheus 的 `rules/eol.yml` 在 90 天、30 天、到期當天各響一次，Grafana 多一列「資產生命週期」。

## 怎麼維護

- **加一列**，別只記在腦子裡。日期不知道就寫 `?`，`basis` 寫 `unknown`，`note` 寫去哪裡查。它會變成 `asset_eol_unknown`，在儀表板上以一個數字提醒有幾件事還沒查。
- **日期的依據要寫清楚**。`vendor` 是廠商自己公布的。`estimate` 是推的，例如 Proxmox VE 9 的期限用底層 Debian 13 的安全支援終止日，`note` 要寫推法。`licence` 是裝置上讀的，TSV 裡那一列只是占位。
- **改日期要有理由**。`--refresh` 發現 endoflife.date 的日期與 TSV 不同時，寫一行 log 並出 `asset_eol_source_mismatch`，不自動改 TSV。人去看兩邊，改 TSV 的日期與 `note`，commit 訊息寫出處（Fortinet 在 2026 年 3 月把 FortiOS 7.4 與 7.6 的日期各延一年，就是這種情況）。
- **到期之後**。`AssetEolPassed` 會一直響，直到升級、換掉，或在 `note` 寫明接受風險並把那一列的 `milestone` 改成下一個（例如 Debian 12 從 `security_support_end` 改看 `lts_end`）。

## 這一版的來源

| 列 | 來源 | 讀到的日期 | 說明 |
|---|---|---|---|
| FortiOS 7.6 | [endoflife.date/fortios](https://endoflife.date/fortios)，[Fortinet CSB-260330-1](https://community.fortinet.com/fortigate-3/technical-tip-fortios-end-of-support-change-for-fortios-v7-4-and-v7-6-224876) | EOES 2028-07-25，EOS 2030-01-25 | 2026 年 3 月各延一年 |
| FortiGate 60F 硬體 | Fortinet 支援入口的 Product Life Cycle | ? | F 系列尚未公告 End of Order，公告後再加 5 年支援（社群討論的說法，待查官方頁面） |
| FortiGuard 授權 | `GET /api/v2/monitor/license/status` | 裝置即時 | Web Filtering 已過期（Day 24） |
| DGX OS 7 | [NVIDIA DGX OS 7 Release Notes](https://docs.nvidia.com/dgx/dgx-os-7-user-guide/release_notes.html) | ? | 沒有公布終止日。DGX OS 8 已存在，7.6.0 是升到 8 的必經版本 |
| Ubuntu 24.04 | [endoflife.date/ubuntu](https://endoflife.date/ubuntu) | 2029-05-31 | DGX OS 7 的底層，當作下限 |
| Proxmox VE 9 | [endoflife.date/proxmox-ve](https://endoflife.date/proxmox-ve) | 推算 2028-08-09 | Proxmox 說至少與對應的 Debian 一樣長，PVE 8 在 2026-08-31 結束 |
| Debian 13 | [endoflife.date/debian](https://endoflife.date/debian) | 安全支援 2028-08-09，LTS 2030-06-30 | PVE 9 的底層 |
| Debian 12 | 同上 | 安全支援 2026-07-11，LTS 2028-06-30 | Day 15 的 QDevice VM，已過安全支援期，待查實機版本 |
| QuTS hero、TS-464 | [endoflife.date/qnap-nas](https://endoflife.date/qnap-nas)，QNAP 產品支援狀態頁 | ? | QNAP 以機型公布支援狀態，TS-464 目前支援中、終止日未定 |
| Node.js 22 | [endoflife.date/nodejs](https://endoflife.date/nodejs) | 2027-04-30 | viewer 的建置映像。Node 20 已於 2026-04-30 結束 |

endoflife.date 是社群維護的彙整，日期來自各廠商的公告，`--refresh` 拿它當第二個眼睛，不當唯一來源。
