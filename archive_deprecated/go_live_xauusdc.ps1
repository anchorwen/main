# DEPRECATED — replaced by python main.py live (one-command live trading).
param(
  [string]$BaseDir = "data",
  [string]$Mt5TerminalPath = "D:\MetaTrader 5\terminal64.exe",
  [string]$Symbol = "XAUUSDc",
  [int]$MaxOpenPositions = 1,
  [double]$MaxNotionalExposure = 5000,
  [double]$DefaultVolume = 0.01,
  [int]$Deviation = 20,
  [int]$Magic = 90001,
  [switch]$SkipPipInstall,
  [switch]$StartBridgeOnly
)

$ErrorActionPreference = "Stop"

function Step([string]$Message) {
  Write-Host ""
  Write-Host "==== $Message ====" -ForegroundColor Cyan
}

Set-Location "D:\cursor"

Step "Install MetaTrader5 Python package"
if (-not $SkipPipInstall) {
  python -m pip install MetaTrader5
}

Step "Preflight read_only"
python scripts/live_read_only_preflight.py `
  --mode read_only `
  --base-dir $BaseDir `
  --mt5-terminal-path $Mt5TerminalPath `
  --output "$BaseDir\reports\preflight_read_only.json"

Step "Preflight micro_live"
python scripts/live_read_only_preflight.py `
  --mode micro_live `
  --base-dir $BaseDir `
  --mt5-terminal-path $Mt5TerminalPath `
  --symbol $Symbol `
  --max-open-positions $MaxOpenPositions `
  --max-notional-exposure $MaxNotionalExposure `
  --output "$BaseDir\reports\preflight_micro_live.json"

if ($StartBridgeOnly) {
  Step "Start MT5 bridge worker only"
  python scripts/mt5_bridge_worker.py `
    --outbox-dir "$BaseDir/mt5_outbox" `
    --receipt-dir "$BaseDir/receipts" `
    --archive-dir "$BaseDir/mt5_outbox_processed" `
    --default-volume $DefaultVolume `
    --deviation $Deviation `
    --magic $Magic `
    --poll-seconds 1.0
  exit 0
}

Step "Send one open/long MT5 handoff message"
python -c "from datetime import datetime,timedelta; from core.contracts.domain.communication_envelope import CommunicationEnvelope; from core.contracts.enums import CommunicationMessageType,CommunicationPriority; from core.deployment.environment_config import EnvironmentConfig; from core.deployment.service_container import ServiceContainer; from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE; cfg=EnvironmentConfig.production(base_dir='$BaseDir',adapter_name='mt5',live_dispatch_enabled=True,live_allowed_symbols=('$Symbol',),extensions={'mt5_terminal_path':r'$Mt5TerminalPath'}); c=ServiceContainer(cfg).build(); env=CommunicationEnvelope(schema_version=SCHEMA_COMMUNICATION_ENVELOPE,message_id='go_live_open_001',correlation_id='go_live_corr_001',causation_id=None,event_time=datetime.utcnow(),producer='decision_engine',target='exec_bridge',message_type=CommunicationMessageType.DECISION_INTENT,priority=CommunicationPriority.NORMAL,payload={'intent_id':'go_live_open_001','symbol':'$Symbol','action':'open','side':'long'},deadline_at=datetime.utcnow()+timedelta(seconds=30)); r=c.dispatcher.dispatch(env); print({'adapter':r.adapter_name,'status':str(r.status),'transport':r.transport_metadata})"

Step "Run bridge once to consume pending message"
python scripts/mt5_bridge_worker.py `
  --outbox-dir "$BaseDir/mt5_outbox" `
  --receipt-dir "$BaseDir/receipts" `
  --archive-dir "$BaseDir/mt5_outbox_processed" `
  --default-volume $DefaultVolume `
  --deviation $Deviation `
  --magic $Magic `
  --once

Step "Bridge health check"
python scripts/mt5_bridge_healthcheck.py `
  --outbox-dir "$BaseDir/mt5_outbox" `
  --receipt-dir "$BaseDir/receipts" `
  --max-pending 10 `
  --max-rejected 0 `
  --output "$BaseDir\reports\mt5_bridge_health.json"

Step "DONE"
Write-Host "go_live_xauusdc completed." -ForegroundColor Green
Write-Host "If you need continuous processing, run:" -ForegroundColor Yellow
Write-Host "python scripts/mt5_bridge_worker.py --outbox-dir $BaseDir/mt5_outbox --receipt-dir $BaseDir/receipts --archive-dir $BaseDir/mt5_outbox_processed --default-volume $DefaultVolume --deviation $Deviation --magic $Magic --poll-seconds 1.0" -ForegroundColor Yellow
