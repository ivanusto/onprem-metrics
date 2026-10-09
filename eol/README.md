# 資產生命週期（Day 25）

`assets.tsv` 是這個場域裡「什麼東西有一個會到期的日子」的清單，一列一個（資產、元件、里程碑）。欄位說明在檔頭的註解。`textfile/eol-textfile.py` 每天讀它一次，寫成 `asset_eol_timestamp_seconds`，Prometheus 的 `rules/eol.yml` 在 90 天、30 天、到期當天各響一次（授權每台裝置合併成一條），Grafana 多一列「資產生命週期」。

## 怎麼維護

- **加一列**，別只記在腦子裡。日期不知道就寫 `?`，`basis` 寫 `unknown`，`note` 寫去哪裡查。它會變成 `asset_eol_unknown`，在儀表板上以一個數字提醒有幾件事還沒查。
- **日期的依據要寫清楚**。`vendor` 是廠商自己公布的。`estimate` 是推的，例如 Proxmox VE 9 的期限用底層 Debian 13 的安全支援終止日，`note` 要寫推法。`licence` 是裝置上讀的，TSV 裡那一列只是占位。
- **改日期要有理由**。`--refresh` 發現 endoflife.date 的日期與 TSV 不同時，寫一行 log 並出 `asset_eol_source_mismatch`，不自動改 TSV。人去看兩邊，改 TSV 的日期與 `note`，commit 訊息寫出處（Fortinet 在 2026 年 3 月把 FortiOS 7.4 與 7.6 的日期各延一年，就是這種情況）。
- **到期之後**。`AssetEolPassed` 會一直響，直到升級、換掉，或在 `note` 寫明接受風險並把那一列的 `milestone` 改成下一個（例如 Debian 12 從 `security_support_end` 改看 `lts_end`）。

## 這一版的來源

讀取日 2026-10-09。endoflife.date 的 v1 API 有三個欄位：`eoasFrom`（積極支援結束）、`eolFrom`（生命週期結束）、`eoesFrom`（延伸支援結束）。三者在各產品的意義不同，`eol-textfile.py` 的 `EOL_FIELD` 依此對應：

| 產品 | eoasFrom | eolFrom | eoesFrom | 對應到 TSV |
|---|---|---|---|---|
| Debian | 一般安全支援結束 | LTS 結束 | ELTS 結束 | `security_support_end`、`lts_end`、`extended_support_end` |
| Ubuntu LTS | 標準支援結束 | 同左 | ESM 結束（要 Pro 訂閱） | `security_support_end` |
| FortiOS | End of Engineering Support | End of Support | 無 | `end_of_engineering`、`end_of_support` |
| Node.js | 積極維護結束 | 終止 | 無 | `end_of_support` |

| 列 | 來源 | 讀到的日期 | 說明 |
|---|---|---|---|
| FortiOS 7.6 | [endoflife.date/fortios](https://endoflife.date/fortios)，[Fortinet CSB-260330-1](https://community.fortinet.com/fortigate-3/technical-tip-fortios-end-of-support-change-for-fortios-v7-4-and-v7-6-224876) | EOES 2028-07-25，EOS 2030-01-25 | 2026 年 3 月各延一年 |
| FortiGate 60F 硬體 | Fortinet 支援入口的 Product Life Cycle | ? | 截至 2026 年 10 月尚未公告 End of Order。Fortinet 至少提前 90 天公告，硬體支援到 End of Order 後 60 個月（第三方整理，待查官方頁面） |
| FortiGuard 授權 | `GET /api/v2/monitor/license/status` | 裝置即時 | 2026-10-09 讀到 20 項 FortiGuard 服務在 2025-09-20 同日到期（含 Web Filtering 與韌體更新），FortiCare 沒有支援合約資料 |
| FortiAP 221E | 第三方追蹤站轉引 Fortinet 的生命週期紀錄，例如 [Uniqcli](https://getuniqcli.com/tools/eol/fortinet/fortiap-221e) | End of Order 2025-12-30，End of Support 2030-12-30 | 已擁有的設備停售只影響加購，所以清單看 End of Support；End of Order 寫在 note。最後一次延長服務 2029-12-30。官方入口要登入，待確認 |
| DGX OS 7 | [NVIDIA DGX OS 7 Release Notes](https://docs.nvidia.com/dgx/dgx-os-7-user-guide/release_notes.html) | ? | 沒有公布終止日。DGX OS 8 已存在，7.6.0 是升到 8 的必經版本 |
| Ubuntu 24.04 | [endoflife.date/ubuntu](https://endoflife.date/ubuntu) | 2029-05-31 | DGX OS 7 的底層，當作下限；也是收集端 VM 的系統 |
| Proxmox VE 9 | [endoflife.date/proxmox-ve](https://endoflife.date/proxmox-ve) | 推算 2028-08-09 | Proxmox 說至少與對應的 Debian 一樣長，PVE 8 在 2026-08-31 結束。endoflife.date 對 9 還沒有日期，`eol_ref` 指向它，一公布就會出現不一致 |
| Debian 13 | [endoflife.date/debian](https://endoflife.date/debian) | 安全支援 2028-08-09，LTS 2030-06-30 | PVE 9 的底層 |
| Debian 12 | 同上 | 安全支援 2026-07-11，LTS 2028-06-30 | Day 15 的 QDevice VM，SSH 橫幅確認仍是 bookworm，已過安全支援期 |
| QuTS hero、TS-464 | [QNAP 產品支援狀態](https://www.qnap.com/en/product/eol-product) | ? | QNAP 以機型公布支援狀態，官方頁面待查。第三方的 [endoflife.ai](https://endoflife.ai/qnap-nas/ts-464) 列 TS-464 為支援中、終止日未定。endoflife.date 沒有 QNAP 的產品 |
| Node.js 22 | [endoflife.date/nodejs](https://endoflife.date/nodejs) | 2027-04-30 | viewer 的建置映像。Node 20 已於 2026-04-30 結束 |

endoflife.date 是社群維護的彙整，日期來自各廠商的公告，`--refresh` 拿它當第二個眼睛，不當唯一來源。
