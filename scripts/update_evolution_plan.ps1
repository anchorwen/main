param(
  [string]$RepoRoot = "D:\cursor",
  [string]$PlanFile = "EVOLUTION_PLAN.md",
  [int]$BackupThresholdHours = 24
)

$ErrorActionPreference = "Stop"

Set-Location $RepoRoot

$planPath = Join-Path $RepoRoot $PlanFile
if (-not (Test-Path -LiteralPath $planPath)) {
  throw "Plan file not found: $planPath"
}

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

$updateStamp = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")
$block = @"

### Daily Update - $updateStamp

- 运行状态: <稳定/告警/阻断>
- 核心统计: <accepted x / rejected y / rejection_rate z>
- 关键事件: <最多3条>
- 根因与修复: <最多3条>
- 阶段进度: <Phase A/B/C 到达位置>
- 明日唯一优先事项: <1-3条>
"@

# UTF-8 BOM avoids garbled Chinese when PowerShell appends to an UTF-8 file on Windows.
$utf8Bom = [System.Text.UTF8Encoding]::new($true)
[System.IO.File]::AppendAllText($planPath, $block, $utf8Bom)

$result = [ordered]@{
  plan_path = $planPath
  updated_at_utc = $updateStamp
  age_hours_before_update = [Math]::Round($ageHours, 2)
  backup_created = [bool]($null -ne $backupPath)
  backup_path = $backupPath
}

$result | ConvertTo-Json -Depth 3
