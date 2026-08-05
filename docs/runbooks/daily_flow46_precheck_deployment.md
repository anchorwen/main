# Daily Flow46 Battle-Readiness Precheck — 部署文档 (FIX-20260805-004)

> IC 2026-08-05 裁决: 每天北京时间凌晨 4 点左右（人类休息时段）自动检查评估
> （周末停盘除外），若发现问题，人类起床后在此对话窗口即可看到并及时解决 —
> 而非最后一天才发现。通道: Windows 计划任务 (schtasks) 物理执行 + Claude 定时任务
> 对话呈现 + 异常 DingTalk 推送（正常日静默）。

## 1. 职责边界 (与哨兵的分工)

| 组件 | 时间 | 职责 |
|:---|:---|:---|
| `gate2_sentinel.py` (FIX-20260805-003) | 每日 12:30 | OFI 积累专用监测: 停滞/回落/临近/就绪 → 自有 `gate2` channel 告警 |
| **`daily_flow46_precheck.py` (本) (FIX-20260805-004)** | **工作日 04:03** | **综合战役健康报告**: 哨兵活性 + OFI 新鲜度 + hash-lock + 桥接 + 倒计时 |

**预检不重复哨兵告警** (stall/ready/near-deadline 属 `gate2` channel，由哨兵独占)。
预检只**读取呈现**哨兵 verdict，并新增 4 项哨兵没有的检查。

## 2. 检查项与阈值

| 检查 | 证据源 | 阈值 | 严重度 |
|:---|:---|:---|:---|
| gate2_progress | `inspect_ofi_history.inspect()` | 信息性 (呈现窗口数/ETA) | — |
| sentinel_liveness | `data_btc/state/gate2_sentinel.json` | last_run 距今 >36h 或缺失 | Sev1 |
| ofi_freshness | `inspect()` last_ts | 距今 >4h (覆盖每日 ~1h 休市) | Sev1 |
| hash_lock | `git status --porcelain` | 脏 tracked `.py/.yaml/.yml/.json` 非 data/ | Sev1 |
| bridge_health | `data_btc/reports/mt5_bridge_health.json` | 断连 或 heartbeat >10min | Sev2 |

- **hash_lock 过滤器精确复制** `train_btc_expected_r_institutional.py:92-124`
  `_enforce_hash_lock` — 预检与 8/19 训练用同一判定，杜绝"预检说干净、训练却拒绝"。
- **时区卫生 (IC Timezone Hygiene)**: 所有时间戳经 `_parse_ts` 归一为
  timezone-aware UTC 后再相减 — 杜绝本地 (UTC+8) 与 naive UTC 直减的 8h 恒定偏差。
- 聚合: any Sev1 → Sev1; else any Sev2 → Sev2; else OK。
- **退出码 = 哨兵约定**: 0=OK (静默), 1=Sev1, 2=Sev2。
- **DingTalk 仅异常时推送**; OK 日静默。

## 3. 部署

### 3.1 计划任务 (schtasks, 工作日 04:03)

```bash
MSYS_NO_PATHCONV=1 schtasks /Create /TN "Future\DailyFlow46Precheck" \
  /TR "cmd /c cd /d D:\future && C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe D:\future\scripts\daily_flow46_precheck.py --data-dir data_btc >> data_btc\state\daily_precheck.log 2>&1" \
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 04:03 /F
```

- `/SC WEEKLY /D MON,TUE,WED,THU,FRI` = 工作日限定 (周末停盘除外)。
- `MSYS_NO_PATHCONV=1` 仅 Git Bash 需要; python/脚本路径**不加引号** (无空格)。
- `cd /d D:\future` 保证 `configs/live.yaml` webhook 回退解析。
- 04:03 北京 = 前日 20:03 UTC; 哨兵 12:30 北京 = 04:30 UTC → 两任务永不竞争。

### 3.2 Claude 定时任务 (对话呈现层, 04:10)

- 每日呈现: `CronCreate` durable recurring `10 4 * * 1-5` → 读取今日报告并输出摘要。
- 7 天自动过期 → 8/11 09:00 one-shot 重建提醒 (覆盖 8/12-8/18)。

## 4. 人工触发与验证

```bash
# 预览 (不写报告不告警)
python scripts/daily_flow46_precheck.py --data-dir data_btc --dry-run
# 正常跑 (写报告 data_btc/state/daily_precheck/<今日>.md)
python scripts/daily_flow46_precheck.py --data-dir data_btc
# 计划任务触发 + 查询
schtasks /Run /TN "Future\DailyFlow46Precheck"
schtasks /Query /TN "Future\DailyFlow46Precheck" /V /FO LIST   # 期望 上次结果 = 0
# 回滚
schtasks /Delete /TN "Future\DailyFlow46Precheck" /F
```

验收 #1 — 正常日: 报告 5 段全 OK, `[STATUS] OK`, exit 0, 无 DingTalk。
验收 #2 — 异常演练: 临时 `echo '# drill' >> configs/training/btc_flow_46_transfer.yaml`
→ 预检报 `hash_lock: Sev1` exit 1 → `git checkout --` 还原。
验收 #3 — 周末守卫: 周六手动运行 → `[weekend] skipped`, exit 0, 不写报告。

## 5. 产物

| 产物 | 路径 |
|:---|:---|
| 每日报告 | `data_btc/state/daily_precheck/YYYY-MM-DD.md` (gitignored) |
| 运行日志 | `data_btc/state/daily_precheck.log` (schtasks `>>`) |
| 告警冷却 | `data/state/alert_cooling.json` (dispatch_alert 内置) |

## 6. 已知事项 (待 IC 裁决)

1. **DingTalk 机器人安全关键词**: 钉钉群机器人在管理端配置了自定义安全关键词,
   现有全部告警文案 (哨兵/live/预检) 均不含该关键词 → API 返回
   `errcode=310000 关键词不匹配`。`dispatch_alert` 正确上报 `sent=False`
   (fail-open 不崩溃); 但 `DingTalkAlertChannel.send` 只查 HTTP 200 不查 errcode
   → live 系统"发送成功"实为机器人静默拒收 (潜在基础设施盲区)。
   **待用户提供机器人关键词** → 加入告警文案即可送达。
   无 DingTalk 时, 报告 + Claude 对话呈现仍完整工作 (核心需求不受影响)。
2. **live.yaml CRLF 复发根因**: `brain_lifecycle_manager._save_live_yaml`
   (governance 持久化 live.yaml) 经 `atomic_write_text` → Windows `Path.write_text`
   文本模式 `\n`→`\r\n` → 工作副本 CRLF → git 伪差异 → 8/19 hash-lock 拒绝。
   预检每日可捕获 (正确); 根治待 DQAF: 写入侧 LF 化 (`newline="\n"`)。
