param(
  [string]$RepoRoot = "D:\cursor",
  [string]$PlanFile = "EVOLUTION_PLAN.md",
  [string]$JournalRelativePath = "data/live_trade_journal.jsonl",
  [string]$FlagRelativePath = "data/live_dispatch_block.flag",
  [int]$BackupThresholdHours = 24,
  [string]$DateUtc = ""
)

$ErrorActionPreference = "Stop"

Set-Location $RepoRoot

$planPath = Join-Path $RepoRoot $PlanFile
if (-not (Test-Path -LiteralPath $planPath)) {
  throw "Plan file not found: $planPath"
}

if ([string]::IsNullOrWhiteSpace($DateUtc)) {
  $dateKey = [DateTime]::UtcNow.ToString("yyyy-MM-dd")
} else {
  $dateKey = $DateUtc
}

$flagPath = Join-Path $RepoRoot $FlagRelativePath

$now = [DateTime]::UtcNow
$fileInfo = Get-Item -LiteralPath $planPath
$ageHours = ($now - $fileInfo.LastWriteTimeUtc).TotalHours

$backupPath = $null
if ($ageHours -gt $BackupThresholdHours) {
  $stamp = $now.ToString("yyyyMMdd_HHmmss")
  $backupName = "EVOLUTION_PLAN.backup.$stamp.md"
  $backupPath = Join-Path $RepoRoot $backupName
  Copy-Item -LiteralPath $planPath -Destination $backupPath -Force
}

$reportNote = ""
$total = 0
$accepted = 0
$rejected = 0
$acknowledged = 0
$other = 0
$rejectionRate = 0.0

try {
  $tqArgs = @(
    (Join-Path $RepoRoot "scripts/trade_quality_report.py"),
    "--journal-path",
    $JournalRelativePath,
    "--date",
    $dateKey
  )
  $reportJson = & python @tqArgs 2>$null | Out-String
  if ([string]::IsNullOrWhiteSpace($reportJson)) {
    throw "empty stdout"
  }
  $report = $reportJson | ConvertFrom-Json
  $total = [int]$report.total
  $accepted = [int]$report.counts.accepted
  $rejected = [int]$report.counts.rejected
  $acknowledged = [int]$report.counts.acknowledged
  $other = [int]$report.counts.other
  $rejectionRate = [double]$report.rejection_rate
} catch {
  $reportNote = "（journal 统计不可用：$($_.Exception.Message)）"
}

$flagPresent = Test-Path -LiteralPath $flagPath
if ($flagPresent) {
  $runState = "阻断（live_dispatch_block.flag 存在）"
} elseif ($total -eq 0) {
  $runState = "稳定/静默（当日尚无 journal 记录）"
} elseif ($rejected -gt 0 -and $accepted -eq 0 -and $total -ge 2) {
  $runState = "告警（当日全部为 rejected / accepted 为 0）"
} elseif ($rejectionRate -ge 0.5 -and $total -ge 3) {
  $runState = "告警（拒单率偏高）"
} else {
  $runState = "稳定"
}

$updateStamp = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")

$lines = Get-Content -LiteralPath $planPath -Encoding UTF8
for ($i = 0; $i -lt $lines.Count; $i++) {
  $ln = $lines[$i]
  if ($null -ne $ln -and $ln.StartsWith("最后更新(UTC):")) {
    $lines[$i] = "最后更新(UTC): $updateStamp  "
    break
  }
}
$utf8Hdr = [System.Text.UTF8Encoding]::new($true)
[System.IO.File]::WriteAllLines($planPath, $lines, $utf8Hdr)

$statsLine = "接受=$accepted 拒绝=$rejected 确认=$acknowledged 其他=$other 合计=$total 拒单率=$([Math]::Round($rejectionRate, 6))"
if (-not [string]::IsNullOrWhiteSpace($reportNote)) {
  $statsLine = "$statsLine $reportNote"
}

$flagLabel = if ($flagPresent) { "存在" } else { "不存在" }

$block = @"

### Daily Update - $updateStamp（自动生成）

- 日期键(UTC): $dateKey
- 运行状态: $runState
- 核心统计: $statsLine
- live_dispatch_block.flag: $flagLabel
- 关键事件: <手动最多 3 条；可从 ops_logs / bridge_supervisor / p1_daily_run 摘抄>
- 根因与修复: <手动最多 3 条>
- 阶段进度: <Phase A/B/C 到达位置>
- 明日唯一优先事项: <1-3 条>
"@

$utf8Bom = [System.Text.UTF8Encoding]::new($true)
[System.IO.File]::AppendAllText($planPath, $block, $utf8Bom)

[ordered]@{
  plan_path                 = $planPath
  updated_at_utc            = $updateStamp
  date_key_utc              = $dateKey
  age_hours_before_update   = [Math]::Round($ageHours, 2)
  backup_created            = [bool]($null -ne $backupPath)
  backup_path               = $backupPath
  dispatch_flag_present     = $flagPresent
  counts_hint               = $statsLine
} | ConvertTo-Json -Depth 4
