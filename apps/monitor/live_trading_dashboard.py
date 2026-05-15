"""Live trading dashboard server.

Single-file HTTP server serving an auto-refreshing HTML dashboard.
Zero new dependencies — uses stdlib http.server + existing project modules.

Usage:
  python apps/monitor/live_trading_dashboard.py
  python apps/monitor/live_trading_dashboard.py --port 8080 --base-dir data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time as time_module
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent

SCHEMA_VERSION = "live_trading_dashboard.v1"

# ── Performance data cache (avoid reloading 1.3MB JSON every 10s) ──
_PERF_CACHE: dict[str, Any] = {"ts": 0.0, "data": None, "mtime": 0.0}
_PERF_CACHE_TTL = 30.0

# ── HTML template (self-contained, dark theme, CSS Grid) ──

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QUANT OS — Live Trading Dashboard</title>
<style>
:root {
  --bg: #0f172a; --panel: #1e293b; --border: #334155;
  --text: #e2e8f0; --muted: #94a3b8; --dim: #64748b;
  --green: #22c55e; --yellow: #eab308; --red: #ef4444; --blue: #3b82f6;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; padding: 12px; min-width: 1300px; }
header { display: flex; align-items: center; justify-content: space-between; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 10px 16px; margin-bottom: 10px; }
header h1 { font-size: 17px; font-weight: 600; letter-spacing: 0.3px; }
header .ts { color: var(--muted); font-size: 13px; }
.header-badges { display: flex; gap: 8px; }

/* Grids */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.grid-4 { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 10px; margin-bottom: 10px; }

.card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; }
.card h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 8px; }
.card-full { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; margin-bottom: 10px; }
.card-full h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 8px; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--dim); font-weight: 500; padding: 4px 8px; border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; white-space: nowrap; }
td { padding: 3px 8px; border-bottom: 1px solid rgba(51,65,85,0.4); }
.dense-table { font-size: 11px; }
.dense-table th { font-size: 10px; padding: 3px 6px; cursor: pointer; user-select: none; }
.dense-table th:hover { color: var(--text); }
.dense-table th .sort-arrow { font-size: 9px; margin-left: 2px; }
.dense-table td { padding: 2px 6px; }
.scroll-y { max-height: 500px; overflow-y: auto; }

/* Badges */
.badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600; }
.badge-green { background: rgba(34,197,94,0.15); color: var(--green); }
.badge-yellow { background: rgba(234,179,8,0.15); color: var(--yellow); }
.badge-red { background: rgba(239,68,68,0.15); color: var(--red); }
.badge-blue { background: rgba(59,130,246,0.15); color: var(--blue); }

/* Colors */
.green { color: var(--green); }
.yellow { color: var(--yellow); }
.red { color: var(--red); }
.muted { color: var(--muted); }
.dim { color: var(--dim); font-size: 12px; }
.num { font-variant-numeric: tabular-nums; }

/* Cells */
.cell-green { color: var(--green); font-weight: 600; }
.cell-yellow { color: var(--yellow); font-weight: 600; }
.cell-red { color: var(--red); font-weight: 600; }
.pnl-positive { color: var(--green); }
.pnl-negative { color: var(--red); }

/* Module status */
.module-ok { border-left: 3px solid var(--green); }
.module-warn { border-left: 3px solid var(--yellow); }
.module-critical { border-left: 3px solid var(--red); }

/* Stat rows */
.stat-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid rgba(51,65,85,0.3); }
.stat-row:last-child { border-bottom: none; }
.stat-val { font-size: 20px; font-weight: 700; }
.stat-sm { font-size: 12px; }

/* SLO */
.slo-row { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid rgba(51,65,85,0.3); font-size: 13px; }
.slo-row:last-child { border-bottom: none; }
.slo-bar-wrap { width: 140px; height: 14px; background: rgba(15,23,42,0.6); border-radius: 7px; overflow: hidden; position: relative; }
.slo-bar-fill { height: 100%; border-radius: 7px; transition: width 0.5s; }
.slo-bar-budget { height: 100%; border-radius: 7px; position: absolute; top: 0; opacity: 0.3; }

/* Decisions */
.decision-card { display: flex; gap: 16px; }
.decision-card > div { flex: 1; padding: 8px 12px; border-radius: 4px; background: rgba(15,23,42,0.5); }
.consensus-long { color: var(--green); font-weight: 700; }
.consensus-short { color: var(--red); font-weight: 700; }
.consensus-neutral, .consensus-split { color: var(--yellow); font-weight: 700; }

/* Alerts */
.alert-critical { border-left: 3px solid var(--red); }
.alert-warning { border-left: 3px solid var(--yellow); }

/* Governance sub-panels */
.gov-section { margin-bottom: 10px; }
.gov-section h3 { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.4px; color: var(--dim); margin-bottom: 4px; }
.gov-item { display: flex; justify-content: space-between; align-items: center; padding: 3px 0; font-size: 12px; border-bottom: 1px solid rgba(51,65,85,0.2); }
.gov-item:last-child { border-bottom: none; }

/* Transition log */
.transition-log { font-size: 11px; max-height: 140px; overflow-y: auto; }
.transition-log .tl-entry { padding: 2px 0; border-bottom: 1px solid rgba(51,65,85,0.2); display: flex; justify-content: space-between; }

/* Error block */
.error-block { color: var(--red); font-style: italic; font-size: 12px; padding: 8px; }
</style>
</head>
<body>

<!-- HEADER -->
<header>
  <div><h1>QUANT OS — LIVE TRADING DASHBOARD</h1></div>
  <div class="ts">UTC <span id="hdr-date">—</span> &nbsp; Refresh: <span id="hdr-refresh">—</span> &nbsp; Next: <span id="hdr-countdown">—</span>s</div>
  <div class="header-badges">
    <span id="hdr-sys-badge"></span>
    <span id="hdr-alert-badge"></span>
  </div>
</header>

<!-- ROW 1: Status bar (4 columns) -->
<div class="grid-4">
  <div class="card">
    <h2>System Status</h2>
    <div id="panel-status"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>Module Health</h2>
    <div id="panel-modules"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>SLO Compliance</h2>
    <div id="panel-slo"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>Risk Gates</h2>
    <div id="panel-risk"><span class="muted">Loading...</span></div>
  </div>
</div>

<!-- ROW 2: Performance Matrix (full width) -->
<div class="card-full">
  <h2>Brain Performance Matrix &nbsp;<span class="dim" style="text-transform:none;font-weight:400">(click headers to sort, default: Sharpe desc)</span></h2>
  <div class="scroll-y" id="panel-performance"><span class="muted">Loading...</span></div>
</div>

<!-- ROW 3: Analytics & Governance (2 columns) -->
<div class="grid-2">
  <div class="card">
    <h2>Analytics &amp; Recommendations</h2>
    <div id="panel-analytics"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>Governance Operations</h2>
    <div id="panel-governance"><span class="muted">Loading...</span></div>
  </div>
</div>

<!-- ROW 4: Live Trading (3 columns) -->
<div class="grid-3">
  <div class="card">
    <h2>Brain Signals (Live)</h2>
    <div id="panel-brains"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>Live Decisions</h2>
    <div id="panel-decisions"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>Current MT5 Positions</h2>
    <div id="panel-positions"><span class="muted">Loading...</span></div>
  </div>
</div>

<!-- ROW 5: History (2 columns) -->
<div class="grid-2">
  <div class="card">
    <h2>Alert History</h2>
    <div id="panel-alerts" style="max-height:220px;overflow-y:auto"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>Recent Trades</h2>
    <div id="panel-journal" style="max-height:220px;overflow-y:auto"><span class="muted">Loading...</span></div>
  </div>
</div>

<script>
var REFRESH_SEC = 10;
var COUNTDOWN = REFRESH_SEC;
var CONSECUTIVE_FAILS = 0;
var PERF_DATA = null;  // cached for client-side sorting
var PERF_SORT = {col: 4, asc: false};  // default: Sharpe desc

// ── Utilities ──

function fmtTm(iso) { if (!iso) return '--:--:--'; return iso.replace('T',' ').substring(0,19); }
function fmtTime(iso) { if (!iso) return '--:--'; return iso.substring(11,19); }
function fmtPn(v) { if (v == null) return '$0.00'; var n = Number(v); return (n>=0?'+$':'-$') + Math.abs(n).toFixed(2); }
function fmtPct(v) { if (v == null) return '--'; return (v*100).toFixed(1)+'%'; }
function fmtNum(v, dec) { if (v == null) return '--'; dec = (dec == null ? 2 : dec); return Number(v).toFixed(dec); }
function badge(label, cls) { return '<span class="badge badge-'+cls+'">'+label+'</span>'; }
function timeAgo(iso) { if (!iso) return '--'; var diff = (Date.now() - Date.parse(iso))/1000; if (diff<60) return Math.floor(diff)+'s ago'; if (diff<3600) return Math.floor(diff/60)+'m ago'; if (diff<86400) return Math.floor(diff/3600)+'h ago'; return Math.floor(diff/86400)+'d ago'; }

// cellColor(val, greenThresh, redThresh, higherIsBetter)
function cellColor(val, gTh, rTh, hi) {
  if (val == null || isNaN(val)) return '';
  if (hi === false) {
    if (val <= gTh) return 'cell-green';
    if (val > rTh) return 'cell-red';
    return 'cell-yellow';
  }
  if (val >= gTh) return 'cell-green';
  if (val < rTh) return 'cell-red';
  return 'cell-yellow';
}

// ── Sortable table ──

function makeSortable(tableId) {
  var table = document.getElementById(tableId);
  if (!table) return;
  var headers = table.querySelectorAll('th[data-col]');
  for (var i = 0; i < headers.length; i++) {
    headers[i].onclick = (function(idx) {
      return function() {
        if (PERF_SORT.col === idx) { PERF_SORT.asc = !PERF_SORT.asc; }
        else { PERF_SORT.col = idx; PERF_SORT.asc = (idx === 0); }
        renderPerformance(PERF_DATA);
      };
    })(i);
  }
}

function sortArrow(colIdx) {
  if (PERF_SORT.col !== colIdx) return ' <span class="sort-arrow dim">-</span>';
  return PERF_SORT.asc ? ' <span class="sort-arrow">&#9650;</span>' : ' <span class="sort-arrow">&#9660;</span>';
}

// ── Render: System Status ──

function renderStatus(data) {
  var j = data.journal || {};
  var f = data.dispatch_flag || {};
  var total = j.total || 0, acc = j.accepted || 0, rej = j.rejected || 0, ack = j.acknowledged || 0;
  var statusLabel, statusCls;
  if (f.active) { statusLabel = 'BLOCKED'; statusCls = 'red'; }
  else if (total > 0) { statusLabel = 'ACTIVE'; statusCls = 'green'; }
  else { statusLabel = 'IDLE'; statusCls = 'yellow'; }
  document.getElementById('hdr-sys-badge').innerHTML = badge(statusLabel, statusCls);
  var h = '';
  h += '<div class="stat-row"><span>Run State</span><span>' + badge(statusLabel, statusCls) + '</span></div>';
  h += '<div class="stat-row"><span>Journal (today)</span><span class="num">' + total + '</span></div>';
  h += '<div class="stat-row"><span>Accepted / Rejected</span><span><span class="green num">'+acc+'</span> / <span class="'+(rej>0?'red':'muted')+' num">'+rej+'</span></span></div>';
  h += '<div class="stat-row"><span>Acknowledged</span><span class="muted num">' + ack + '</span></div>';
  h += '<div class="stat-row"><span>Dispatch Flag</span><span>' + (f.active ? badge('BLOCKED','red')+' <span class="dim">'+((f.payload||{}).reason||'')+'</span>' : badge('CLEAR','green')) + '</span></div>';
  if (data.errors && data.errors.length) h += '<div class="error-block">'+data.errors.length+' collector error(s)</div>';
  // Stats summary
  var labels = data.labels || {};
  if (labels.total != null) {
    var wr = labels.win_rate;
    var pnl = labels.total_pnl || 0;
    h += '<div class="stat-row" style="margin-top:4px"><span>P&amp;L / Win Rate</span><span><span class="'+(pnl>=0?'green':'red')+' num">'+fmtPn(pnl)+'</span> <span class="dim">'+fmtPct(wr)+'</span></span></div>';
  }
  document.getElementById('panel-status').innerHTML = h;
}

// ── Render: Module Health ──

function renderModules(data) {
  if (!data || !data.modules) {
    document.getElementById('panel-modules').innerHTML = '<span class="muted">No module data</span>';
    return;
  }
  var mods = data.modules;
  var modOrder = ['mt5_bridge','outbox','feature_store','brain_adapters','governance','dispatch','daily_ops'];
  var labels = {mt5_bridge:'MT5 Bridge',outbox:'Outbox',feature_store:'Feature Store',brain_adapters:'Brain Adapters',governance:'Governance',dispatch:'Dispatch',daily_ops:'Daily Ops'};
  var h = '';
  for (var i = 0; i < modOrder.length; i++) {
    var k = modOrder[i];
    var m = mods[k];
    if (!m) continue;
    var st = m.status || 'OK';
    var rowCls = st === 'CRITICAL' || st === 'ERROR' ? 'module-critical' : (st === 'WARNING' ? 'module-warn' : 'module-ok');
    var stCls = st === 'CRITICAL' || st === 'ERROR' ? 'red' : (st === 'WARNING' ? 'yellow' : 'green');
    var detail = '';
    if (k === 'mt5_bridge') detail = (m.connected ? 'connected' : 'disconnected') + (m.last_heartbeat ? ' '+timeAgo(m.last_heartbeat) : '');
    else if (k === 'outbox') detail = 'pending='+(m.pending||0)+' stale='+(m.stale||0);
    else if (k === 'feature_store') detail = m.freshness || (m.available ? 'ok' : 'missing');
    else if (k === 'brain_adapters') detail = (m.with_data||0)+'/'+(m.configured||0)+' with data';
    else if (k === 'governance') detail = 'live='+(m.live||0)+' frozen='+(m.frozen||0);
    else if (k === 'dispatch') detail = m.blocked ? 'BLOCKED' : 'clear';
    else if (k === 'daily_ops') detail = m.last_recap || '--';
    else detail = '';
    h += '<div class="stat-row '+rowCls+'" style="padding:4px 6px"><span style="font-size:12px">'+(labels[k]||k)+'</span><span><span class="'+stCls+'" style="font-size:12px">'+st+'</span>'+(detail?' <span class="dim" style="font-size:11px">'+detail+'</span>':'')+'</span></div>';
  }
  // Resources
  var res = data.resources || {};
  if (res.available) {
    h += '<div class="stat-row" style="padding:4px 6px;margin-top:4px;border-top:1px solid var(--border)"><span style="font-size:11px">CPU / Mem / Disk</span><span class="num dim" style="font-size:11px">'+(res.cpu_pct!=null?res.cpu_pct.toFixed(0)+'%':'?')+' / '+(res.memory_pct!=null?res.memory_pct.toFixed(0)+'%':'?')+' / '+(res.disk_pct!=null?res.disk_pct.toFixed(0)+'%':'?')+'</span></div>';
  }
  // Alert level badge
  var al = data.alert_level || 'OK';
  var alCls = al === 'CRITICAL' ? 'red' : (al === 'WARNING' ? 'yellow' : 'green');
  document.getElementById('hdr-alert-badge').innerHTML = badge(al, alCls);
  document.getElementById('panel-modules').innerHTML = h;
}

// ── Render: Performance Matrix ──

function renderPerformance(data) {
  if (!data || !data.brains) { document.getElementById('panel-performance').innerHTML = '<span class="muted">No performance data</span>'; return; }
  PERF_DATA = data;
  var brains = data.brains.slice();
  // Sort
  var col = PERF_SORT.col;
  var asc = PERF_SORT.asc;
  var keys = ['brain_id','governance_status','cumulative_pnl','win_rate','sharpe_ratio','profit_factor','max_drawdown','sample_count','health_signal','recommendation','long_win_rate','short_win_rate'];
  brains.sort(function(a, b) {
    var va = a[keys[col]], vb = b[keys[col]];
    if (va == null) va = (typeof vb === 'number') ? -Infinity : '';
    if (vb == null) vb = (typeof va === 'number') ? -Infinity : '';
    if (typeof va === 'number' && typeof vb === 'number') return asc ? va - vb : vb - va;
    va = String(va); vb = String(vb);
    return asc ? va.localeCompare(vb) : vb.localeCompare(va);
  });

  var h = '<table class="dense-table" id="perf-table"><thead><tr>';
  var headers = ['Brain ID','Gov','P&amp;L','Win%','Sharpe','PF','MaxDD','N','Health','Rec','LongWR','ShortWR'];
  for (var i = 0; i < headers.length; i++) {
    h += '<th data-col="'+i+'">'+headers[i]+sortArrow(i)+'</th>';
  }
  h += '</tr></thead><tbody>';
  for (var r = 0; r < brains.length; r++) {
    var b = brains[r];
    var govSt = b.governance_status || 'unknown';
    var govCls = {'live':'green','candidate':'yellow','probation':'yellow','frozen':'red'}[govSt] || 'muted';
    var hlCls = {'healthy':'green','stable':'green','degraded':'yellow','critical':'red','insufficient_data':'dim'}[b.health_signal] || 'dim';
    var rec = b.recommendation || 'observe';
    var recCls = rec === 'eligible_for_promotion' ? 'green' : (rec === 'freeze' || rec === 'demote_to_probation' ? 'red' : 'muted');
    var recLabel = {'eligible_for_promotion':'PROMOTE','demote_to_probation':'DEMOTE','freeze':'FREEZE','limit_exposure':'LIMIT','observe':'observe','':'observe'}[rec] || rec;
    h += '<tr>';
    h += '<td style="font-weight:600">'+(b.brain_id||'?')+'</td>';
    h += '<td>'+badge(govSt, govCls)+'</td>';
    h += '<td class="num '+cellColor(b.cumulative_pnl, 0.01, 0)+'">'+fmtNum(b.cumulative_pnl, 4)+'</td>';
    h += '<td class="num '+cellColor(b.win_rate, 0.55, 0.45)+'">'+fmtPct(b.win_rate)+'</td>';
    h += '<td class="num '+cellColor(b.sharpe_ratio, 1.0, 0.0)+'">'+fmtNum(b.sharpe_ratio, 2)+'</td>';
    h += '<td class="num '+cellColor(b.profit_factor, 1.5, 1.0)+'">'+fmtNum(b.profit_factor, 2)+'</td>';
    h += '<td class="num '+cellColor(b.max_drawdown, 5.0, 15.0, false)+'">'+fmtNum(b.max_drawdown, 2)+'</td>';
    h += '<td class="num dim">'+(b.sample_count||0)+'</td>';
    h += '<td>'+badge(b.health_signal||'?', hlCls)+'</td>';
    h += '<td class="'+recCls+'" style="font-size:10px;font-weight:600">'+recLabel+'</td>';
    h += '<td class="num '+cellColor(b.long_win_rate, 0.55, 0.45)+'">'+fmtPct(b.long_win_rate)+'</td>';
    h += '<td class="num '+cellColor(b.short_win_rate, 0.55, 0.45)+'">'+fmtPct(b.short_win_rate)+'</td>';
    h += '</tr>';
  }
  h += '</tbody></table>';
  if (data.errors && data.errors.length) h += '<div class="error-block" style="margin-top:4px">Errors: '+data.errors.join(', ')+'</div>';
  document.getElementById('panel-performance').innerHTML = h;
  makeSortable('perf-table');
}

// ── Render: Analytics ──

function renderAnalytics(data) {
  if (!data) { document.getElementById('panel-analytics').innerHTML = '<span class="muted">No analytics data</span>'; return; }
  var h = '';

  // Param suggestions
  var sug = data.param_suggestions || [];
  h += '<div class="gov-section"><h3>Parameter Suggestions ('+sug.length+')</h3>';
  if (!sug.length) { h += '<span class="dim" style="font-size:11px">No suggestions</span>'; }
  else {
    for (var i = 0; i < Math.min(sug.length, 5); i++) {
      var s = sug[i];
      h += '<div class="gov-item"><span style="font-size:11px">'+(s.brain_id||s.param||'?')+'</span><span class="dim" style="font-size:11px">'+(s.reason||s.suggestion||'')+'</span></div>';
    }
  }
  h += '</div>';

  // Degraded brains
  var degraded = data.degraded_brains || [];
  h += '<div class="gov-section"><h3>Degraded Brains ('+degraded.length+')</h3>';
  if (!degraded.length) { h += '<span class="dim" style="font-size:11px">None degraded</span>'; }
  else {
    for (var j = 0; j < degraded.length; j++) {
      var d = degraded[j];
      var dhl = d.health_signal === 'critical' ? 'red' : 'yellow';
      h += '<div class="gov-item"><span style="font-size:11px">'+d.brain_id+'</span><span class="'+dhl+'" style="font-size:11px">'+d.health_signal+'</span><span class="dim" style="font-size:10px">n='+d.sample_count+' rec='+(d.recommendation||'?')+'</span></div>';
    }
  }
  h += '</div>';

  // Retirement candidates
  var ret = data.retirement_candidates || [];
  h += '<div class="gov-section"><h3>Retirement Candidates ('+ret.length+')</h3>';
  if (!ret.length) { h += '<span class="dim" style="font-size:11px">None</span>'; }
  else {
    for (var k = 0; k < ret.length; k++) {
      var rt = ret[k];
      h += '<div class="gov-item"><span style="font-size:11px">'+rt.brain_id+'</span><span class="red" style="font-size:11px">'+rt.health_signal+'</span><span class="dim" style="font-size:10px">status='+(rt.current_status||'?')+'</span></div>';
    }
  }
  h += '</div>';

  if (data.errors && data.errors.length) h += '<div class="error-block">Errors: '+data.errors.join(', ')+'</div>';
  document.getElementById('panel-analytics').innerHTML = h;
}

// ── Render: Governance ──

function renderGovernance(data) {
  if (!data) { document.getElementById('panel-governance').innerHTML = '<span class="muted">No governance data</span>'; return; }
  var h = '';

  // Status summary counts
  var counts = data.status_counts || {};
  h += '<div class="gov-section"><h3>Status Distribution</h3><div style="display:flex;gap:12px;font-size:12px;flex-wrap:wrap">';
  var countKeys = Object.keys(counts);
  for (var i = 0; i < countKeys.length; i++) {
    var ck = countKeys[i];
    var cc = counts[ck];
    var cCls = ck === 'live' ? 'green' : (ck === 'frozen' ? 'red' : (ck === 'candidate'||ck==='probation' ? 'yellow' : 'dim'));
    h += '<span><span class="'+cCls+'">'+ck+'</span>: <span class="num">'+cc+'</span></span>';
  }
  h += '</div></div>';

  // Promotion queue
  var pq = data.promotion_queue || [];
  h += '<div class="gov-section"><h3>Promotion Queue ('+pq.length+')</h3>';
  if (!pq.length) h += '<span class="dim" style="font-size:11px">Empty</span>';
  else for (var p = 0; p < pq.length; p++) {
    var pi = pq[p];
    h += '<div class="gov-item"><span style="font-size:11px">'+pi.brain_id+'</span><span class="green" style="font-size:11px">score '+fmtNum(pi.composite_score,3)+'</span><span class="dim" style="font-size:10px">from '+pi.from_status+' n='+pi.sample_count+'</span></div>';
  }
  h += '</div>';

  // Demotion warnings
  var dw = data.demotion_warnings || [];
  h += '<div class="gov-section"><h3>Demotion Warnings ('+dw.length+')</h3>';
  if (!dw.length) h += '<span class="dim" style="font-size:11px">None</span>';
  else for (var w = 0; w < dw.length; w++) {
    var di = dw[w];
    var recLabel = {'demote_to_probation':'DEMOTE','freeze':'FREEZE','limit_exposure':'LIMIT'}[di.recommendation] || di.recommendation;
    h += '<div class="gov-item"><span style="font-size:11px">'+di.brain_id+'</span><span class="red" style="font-size:11px">'+recLabel+'</span><span class="dim" style="font-size:10px">score='+fmtNum(di.composite_score,3)+'</span></div>';
  }
  h += '</div>';

  // Freeze list
  var fl = data.freeze_list || [];
  h += '<div class="gov-section"><h3>Frozen Brains ('+fl.length+')</h3>';
  if (!fl.length) h += '<span class="dim" style="font-size:11px">None frozen</span>';
  else for (var f = 0; f < fl.length; f++) {
    var fi = fl[f];
    h += '<div class="gov-item"><span style="font-size:11px">'+fi.brain_id+'</span><span class="red" style="font-size:10px">x'+fi.freeze_count+'</span><span class="dim" style="font-size:10px">'+(fi.reason||'')+'</span></div>';
  }
  h += '</div>';

  // Recent transitions
  var tr = data.recent_transitions || [];
  h += '<div class="gov-section"><h3>Recent Transitions</h3><div class="transition-log">';
  if (!tr.length) h += '<span class="dim" style="font-size:11px">No transitions</span>';
  else for (var t = 0; t < tr.length; t++) {
    var tx = tr[t];
    var from = tx.from_status || tx.from || '?';
    var to = tx.to_status || tx.to || '?';
    var when = tx.transitioned_at || tx.at || tx.timestamp || '';
    var toCls = to === 'live' ? 'green' : (to === 'frozen' ? 'red' : 'yellow');
    h += '<div class="tl-entry"><span>'+tx.brain_id+'</span><span>'+from+' &rarr; <span class="'+toCls+'">'+to+'</span></span><span class="dim">'+timeAgo(when)+'</span></div>';
  }
  h += '</div></div>';

  if (data.errors && data.errors.length) h += '<div class="error-block">Errors: '+data.errors.join(', ')+'</div>';
  document.getElementById('panel-governance').innerHTML = h;
}

// ── Render: Positions ──

function renderPositions(data) {
  if (!data.connected) {
    document.getElementById('panel-positions').innerHTML = '<span class="yellow">MT5 not connected</span>';
    return;
  }
  if (!data.positions || data.positions.length === 0) {
    var age = data.generated_at ? (' <span class="dim">(snapshot '+fmtTime(data.generated_at)+')</span>') : '';
    document.getElementById('panel-positions').innerHTML = '<span class="muted">No open positions</span>' + age;
    return;
  }
  var src = data._source ? ' <span class="dim" style="font-size:10px">['+data._source+']</span>' : '';
  var h = '<table><tr><th>Ticket</th><th>Symbol</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>P&amp;L</th></tr>';
  for (var i=0; i<data.positions.length; i++) {
    var p = data.positions[i];
    var sideCls = p.side === 'BUY' ? 'green' : 'red';
    var pnl = p.profit != null ? p.profit : 0;
    var pnlCls = pnl >= 0 ? 'green' : 'red';
    h += '<tr><td class="num">' + (p.ticket||'?') + '</td><td>' + (p.symbol||'?') + '</td><td class="'+sideCls+'">' + (p.side||'?') + '</td><td class="num">' + (p.price_open||'?') + '</td><td class="num dim">' + (p.sl||'--') + '</td><td class="num dim">' + (p.tp||'--') + '</td><td class="num '+pnlCls+'">' + fmtPn(pnl) + '</td></tr>';
  }
  h += '</table>';
  if (data.generated_at) h += '<div class="dim" style="margin-top:4px">'+fmtTm(data.generated_at)+src+'</div>';
  document.getElementById('panel-positions').innerHTML = h;
}

// ── Render: Brain Signals (enhanced with PnL badges) ──

function renderBrains(data) {
  var brains = data.brains || [];
  if (!brains.length) { document.getElementById('panel-brains').innerHTML = '<span class="muted">No brain data</span>'; return; }
  var h = '<table class="dense-table"><tr><th>Brain ID</th><th>St</th><th>Health</th><th>Dir</th><th>Conf</th><th>PnL</th><th>Sharpe</th><th>N</th></tr>';
  for (var i=0; i<brains.length; i++) {
    var b = brains[i];
    var stCls = {'live':'green','candidate':'yellow','probation':'yellow','frozen':'red'}[b.status] || 'muted';
    var hlCls = {'healthy':'green','stable':'green','degraded':'yellow','critical':'red','insufficient_data':'dim'}[b.health] || 'dim';
    var dirCls = b.last_direction === 'LONG' ? 'green' : b.last_direction === 'SHORT' ? 'red' : 'yellow';
    var conf = b.confidence != null ? (b.confidence*100).toFixed(0)+'%' : '--';
    var pnl = b.pnl_total != null ? b.pnl_total : 0;
    var pnlStr = pnl === 0 && b.pnl_samples == null ? '--' : (pnl>=0?'+':'')+pnl.toFixed(3);
    var pnlCls = pnl > 0 ? 'green' : (pnl < 0 ? 'red' : 'muted');
    var sharpe = b.sharpe_ratio != null ? b.sharpe_ratio.toFixed(2) : '--';
    var n = b.sample_count || b.pnl_samples || 0;
    var rowCls = b.health === 'critical' || b.status === 'frozen' ? 'module-critical' : (b.health === 'degraded' ? 'module-warn' : '');
    h += '<tr class="'+rowCls+'"><td style="font-weight:500">'+(b.brain_id||'?')+'</td><td>'+badge(b.status||'?', stCls)+'</td><td>'+badge(b.health||'?', hlCls)+'</td><td class="'+dirCls+'">'+(b.last_direction||'?')+'</td><td class="num">'+conf+'</td><td class="num '+pnlCls+'">'+pnlStr+'</td><td class="num">'+sharpe+'</td><td class="num dim">'+n+'</td></tr>';
  }
  h += '</table>';
  document.getElementById('panel-brains').innerHTML = h;
}

// ── Render: Decisions ──

function renderDecisions(data) {
  var h = '<div class="decision-card">';
  h += _renderDecisionCard('Shadow Ensemble', data.shadow);
  h += _renderDecisionCard('Live Dispatch', data.live);
  h += '</div>';
  document.getElementById('panel-decisions').innerHTML = h;
}
function _renderDecisionCard(title, d) {
  if (!d) return '<div><span class="dim">'+title+'</span><br><span class="muted">No data</span></div>';
  var c = d.consensus || 'no_results';
  var cls = 'consensus-'+c;
  var att = d.brains || {};
  var sup = (att.supporting||[]).join(', ') || '--';
  var opp = (att.opposing||[]).join(', ') || '--';
  return '<div><span class="dim">'+title+'</span><br><span class="'+cls+'" style="font-size:16px">' + c.toUpperCase() + '</span><br><span class="muted">'+ (d.decision_action||'?') + ' &middot; ' + (d.decision_side||'?') + '</span><br><span class="dim" style="font-size:11px">'+fmtTm(d.event_time)+'</span><br><span class="dim">Agreement: '+((d.agreement_score||d.consensus_score||0)*100).toFixed(1)+'%</span><br><span class="green">Supp: '+sup+'</span><br><span class="red">Opp: '+opp+'</span></div>';
}

// ── Render: SLO ──

function renderSLO(data) {
  var objs = data.objectives || {};
  var names = Object.keys(objs);
  if (!names.length) { document.getElementById('panel-slo').innerHTML = '<span class="muted">No SLO data</span>'; return; }
  var labels = {
    decision_success_rate: 'Decision Rate',
    dispatch_success_rate: 'Dispatch Rate',
    reconciliation_match_rate: 'Recon Match',
    throttle_rate: 'Throttle',
    circuit_open_rate: 'Circuit Open'
  };
  var h = '';
  for (var i=0; i<names.length; i++) {
    var o = objs[names[i]];
    var val = o.value != null ? o.value : 0;
    var tgt = o.target != null ? o.target : 1;
    var met = o.met;
    var budget = o.error_budget_remaining_pct != null ? o.error_budget_remaining_pct : 100;
    var displayVal = (val*100).toFixed(1)+'%';
    var barW = Math.min(100, Math.max(0, (val/tgt)*100));
    var barCls = met ? 'var(--green)' : 'var(--red)';
    var budgetCls = budget > 20 ? 'rgba(34,197,94,0.3)' : (budget > 0 ? 'rgba(234,179,8,0.5)' : 'rgba(239,68,68,0.6)');
    h += '<div class="slo-row"><span style="width:90px;font-size:12px">'+ (labels[names[i]]||names[i]) +'</span>';
    h += '<span class="num" style="width:50px;font-size:11px">'+displayVal+'</span>';
    h += '<div class="slo-bar-wrap" style="width:100px"><div class="slo-bar-fill" style="width:'+barW+'%;background:'+barCls+'"></div><div class="slo-bar-budget" style="width:'+budget+'%;background:'+budgetCls+'"></div></div>';
    h += '<span class="num dim" style="width:35px;font-size:10px">'+budget.toFixed(0)+'%</span></div>';
  }
  var statusCls = data.status === 'healthy' ? 'green' : 'red';
  h += '<div style="margin-top:4px;font-size:11px">Status: <span class="'+statusCls+'">' + (data.status||'?').toUpperCase() + '</span>';
  if (data.failed_objectives && data.failed_objectives.length) {
    h += ' &middot; <span class="red">'+data.failed_objectives.length+' breaching</span>';
  }
  h += '</div>';
  document.getElementById('panel-slo').innerHTML = h;
}

// ── Render: Risk Gates ──

function renderRisk(data) {
  var policies = data.policies || [];
  var h = '';
  var overall = data.overall || 'PASS';
  var overallCls = overall === 'BLOCK' ? 'red' : (overall === 'WARN' ? 'yellow' : 'green');
  h += '<div class="stat-row"><span>Overall</span><span>' + badge(overall, overallCls) + '</span></div>';
  for (var i=0; i<policies.length; i++) {
    var p = policies[i];
    h += '<div class="stat-row"><span style="font-size:12px">' + (p.name||'?') + '</span><span>' + badge(p.passed?'PASS':'BLOCK', p.passed?'green':'red') + '</span></div>';
    if (p.detail) h += '<div class="dim" style="font-size:11px;padding-left:8px">' + p.detail + '</div>';
  }
  if (data.flag_active) h += '<div class="stat-row"><span>Dispatch Flag</span><span>' + badge('ACTIVE','red') + '</span></div>';
  document.getElementById('panel-risk').innerHTML = h;
}

// ── Render: Alerts ──

function renderAlerts(data) {
  var alerts = data.alerts || [];
  if (!alerts.length) { document.getElementById('panel-alerts').innerHTML = '<span class="muted">No alerts fired</span>'; return; }
  var h = '<table><tr><th>Time</th><th>Sev</th><th>Rule</th><th>Context</th></tr>';
  for (var i=0; i<Math.min(alerts.length, 30); i++) {
    var a = alerts[i];
    var sevCls = {'critical':'red','error':'red','warning':'yellow'}[a.severity] || 'muted';
    var ctx = a.context_snapshot || {};
    var ctxStr = Object.keys(ctx).slice(0,3).map(function(k){return k+'='+ctx[k]}).join(' ');
    h += '<tr class="'+(a.severity==='critical'?'alert-critical':(a.severity==='warning'?'alert-warning':''))+'"><td class="dim num">' + fmtTime(a.fired_at) + '</td><td>' + badge(a.severity||'?', sevCls) + '</td><td style="font-size:12px">' + (a.rule_name||'?') + '</td><td class="dim" style="font-size:11px">' + (ctxStr||'--') + '</td></tr>';
  }
  h += '</table>';
  document.getElementById('panel-alerts').innerHTML = h;
}

// ── Render: Journal (Recent Trades) ──

function renderJournal(data) {
  var entries = data.entries || [];
  if (!entries.length) { document.getElementById('panel-journal').innerHTML = '<span class="muted">No trades today</span>'; return; }
  var h = '<table><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Side</th><th>Status</th><th>SL</th><th>TP</th></tr>';
  for (var i=0; i<entries.length; i++) {
    var e = entries[i];
    var stCls = {'accepted':'green','rejected':'red','acknowledged':'blue','closed':'dim'}[e.ack_status] || 'muted';
    var sideCls = e.side === 'long' ? 'green' : e.side === 'short' ? 'red' : '';
    h += '<tr><td class="dim num">' + fmtTime(e.recorded_at) + '</td><td>' + (e.symbol||'?') + '</td><td>' + (e.action||'?') + '</td><td class="'+sideCls+'">' + (e.side||'?') + '</td><td>' + badge(e.ack_status||'?', stCls) + '</td><td class="num dim">' + (e.sl||'--') + '</td><td class="num dim">' + (e.tp||'--') + '</td></tr>';
  }
  h += '</table>';
  document.getElementById('panel-journal').innerHTML = h;
}

// ── Timestamp ──

function updateTimestamp() {
  var now = new Date().toISOString();
  document.getElementById('hdr-date').textContent = now.substring(0,10);
  document.getElementById('hdr-refresh').textContent = now.substring(11,19);
}

function doFetch(url, renderFn) {
  return fetch(url, {signal: AbortSignal.timeout(5000)})
    .then(function(r) { if (!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(data) { renderFn(data); return true; })
    .catch(function(err) { console.warn('Fetch failed: '+url+' '+err.message); return false; });
}

// ── Refresh all ──

function refreshAll() {
  updateTimestamp();
  var promises = [
    doFetch('/api/dashboard', renderStatus),
    doFetch('/api/modules', renderModules),
    doFetch('/api/performance', renderPerformance),
    doFetch('/api/governance', renderGovernance),
    doFetch('/api/analytics', renderAnalytics),
    doFetch('/api/positions', renderPositions),
    doFetch('/api/brains', renderBrains),
    doFetch('/api/journal', renderJournal),
    doFetch('/api/decisions', renderDecisions),
    doFetch('/api/slo', renderSLO),
    doFetch('/api/alerts', renderAlerts),
    doFetch('/api/risk', renderRisk),
  ];
  Promise.allSettled(promises).then(function(results) {
    var allOk = results.every(function(r) { return r.value === true; });
    if (allOk) { CONSECUTIVE_FAILS = 0; }
    else { CONSECUTIVE_FAILS++; }
    COUNTDOWN = REFRESH_SEC;
    document.getElementById('hdr-countdown').textContent = COUNTDOWN;
  });
}

function countdownTick() {
  COUNTDOWN--;
  if (COUNTDOWN < 0) COUNTDOWN = 0;
  document.getElementById('hdr-countdown').textContent = COUNTDOWN;
}

refreshAll();
setInterval(refreshAll, REFRESH_SEC * 1000);
setInterval(countdownTick, 1000);
</script>
</body>
</html>"""


# ── Data helpers ──


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _today_key() -> str:
    return datetime.now(UTC).replace(tzinfo=None).date().isoformat()


def _read_last_line(path: Path) -> str | None:
    """Read the last non-empty line from a file efficiently."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.strip()]
        return lines[-1] if lines else None
    except (OSError, UnicodeDecodeError):
        return None


def _read_last_n_lines(path: Path, n: int = 20) -> list[str]:
    """Read last N non-empty lines from a file."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.strip()]
        return lines[-n:]
    except (OSError, UnicodeDecodeError):
        return []


def _collect_system_resources() -> dict[str, Any]:
    """Collect system resource usage (CPU, memory, disk for data/ volume).

    Returns a dict with keys: available, cpu_pct, memory_pct, disk_pct, disk_path.
    Uses psutil if installed, otherwise falls back to available=False.
    """
    try:
        import psutil  # type: ignore[import-not-found]

        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        data_disk = psutil.disk_usage(str(Path("data").resolve()))
        return {
            "available": True,
            "cpu_pct": cpu,
            "memory_pct": mem,
            "disk_pct": round(data_disk.percent, 1),
            "disk_path": str(data_disk.path),
        }
    except Exception:
        import shutil

        try:
            usage = shutil.disk_usage(str(Path("data").resolve()))
            disk_pct = round((usage.used / usage.total) * 100, 1) if usage.total > 0 else 0
            return {
                "available": True,
                "cpu_pct": None,
                "memory_pct": None,
                "disk_pct": disk_pct,
                "disk_path": str(Path("data").resolve()),
            }
        except Exception:
            return {"available": False}


# ── Live MT5 positions fetch (thread-timeout guarded) ──


def _fetch_live_mt5_positions(
    mt5_terminal_path: str,
    symbol: str = "XAUUSDc",
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Call MT5 positions_get() in a daemon thread with a hard timeout.

    Returns the same shape as mt5_positions_snapshot.build_snapshot().
    Never blocks the caller for more than *timeout* seconds.
    """
    import threading

    result: list[dict[str, Any] | None] = [None]
    exc_info: list[Exception | None] = [None]

    def _target() -> None:
        try:
            from scripts.mt5_positions_snapshot import build_snapshot

            snap = build_snapshot(mt5_terminal_path=mt5_terminal_path, symbol=symbol)
            result[0] = snap
        except Exception as exc:
            exc_info[0] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        return {
            "connected": False,
            "position_count": 0,
            "positions": [],
            "generated_at": _utc_now_iso(),
            "error": f"mt5_call_timeout_after_{timeout}s",
        }
    if exc_info[0] is not None:
        return {
            "connected": False,
            "position_count": 0,
            "positions": [],
            "generated_at": _utc_now_iso(),
            "error": f"{type(exc_info[0]).__name__}: {exc_info[0]}",
        }
    return (
        result[0]
        if result[0] is not None
        else {
            "connected": False,
            "position_count": 0,
            "positions": [],
            "generated_at": _utc_now_iso(),
            "error": "mt5_returned_none",
        }
    )


# ── HTTP handler ──


class LiveDashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the live trading dashboard."""

    # Set by run_dashboard_server before serving
    BASE_DIR: Path = Path("data")
    MT5_TERMINAL_PATH: str | None = None  # e.g. D:\\MetaTrader 5\\terminal64.exe

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002
        """Suppress default access logging to stderr (keep it clean)."""
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path == "/":
                self._serve_html()
            elif path == "/api/dashboard":
                self._serve_api_dashboard()
            elif path == "/api/positions":
                self._serve_api_positions()
            elif path == "/api/brains":
                self._serve_api_brains()
            elif path == "/api/journal":
                self._serve_api_journal()
            elif path == "/api/decisions":
                self._serve_api_decisions()
            elif path == "/api/slo":
                self._serve_api_slo()
            elif path == "/api/alerts":
                self._serve_api_alerts()
            elif path == "/api/health":
                self._serve_api_health()
            elif path == "/api/risk":
                self._serve_api_risk()
            elif path == "/api/performance":
                self._serve_api_performance()
            elif path == "/api/governance":
                self._serve_api_governance()
            elif path == "/api/analytics":
                self._serve_api_analytics()
            elif path == "/api/modules":
                self._serve_api_modules()
            else:
                self._serve_json({"error": "not_found"}, 404)
        except Exception:
            self._serve_json({"error": "internal_error"}, 500)

    def _serve_html(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

    def _serve_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    # ── API endpoints ──

    def _serve_api_dashboard(self) -> None:
        date = _today_key()
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from scripts.live_dashboard import build_dashboard  # type: ignore[import-not-found]

            report = build_dashboard(base_dir=str(self.BASE_DIR), date_key=date)
            # Strip pre-rendered text blob
            report.pop("text", None)
            report["generated_at"] = _utc_now_iso()
            self._serve_json(report)
        except Exception as exc:
            self._serve_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "generated_at": _utc_now_iso(),
                    "date_key": date,
                    "error": f"dashboard_collect_failed: {exc}",
                    "journal": {"total": 0, "accepted": 0, "rejected": 0, "acknowledged": 0},
                    "labels": {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0},
                    "dispatch_flag": {"active": False},
                    "errors": [str(exc)[:500]],
                }
            )

    def _serve_api_positions(self) -> None:
        # 1. Try live MT5 via thread-timeout guard (if terminal path configured)
        if self.MT5_TERMINAL_PATH:
            data = _fetch_live_mt5_positions(
                mt5_terminal_path=self.MT5_TERMINAL_PATH,
                symbol="XAUUSDc",
                timeout=4.0,
            )
            if not data.get("error"):
                # Live fetch succeeded — also write to snapshot cache for other consumers
                snap_path = self.BASE_DIR / "reports" / "mt5_positions_live_now.json"
                try:
                    snap_path.parent.mkdir(parents=True, exist_ok=True)
                    snap_path.write_text(
                        json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8"
                    )
                except OSError:
                    pass
                for p in data.get("positions", []):
                    pos_type = p.get("type", -1)
                    p["side"] = "BUY" if pos_type == 0 else "SELL" if pos_type == 1 else "UNKNOWN"
                data["_source"] = "live_mt5"
                self._serve_json(data)
                return
            # Live MT5 failed — fall through to snapshot

        # 2. Fallback: read cached snapshot file
        snap_path = self.BASE_DIR / "reports" / "mt5_positions_live_now.json"
        if not snap_path.exists():
            self._serve_json(
                {
                    "connected": False,
                    "position_count": 0,
                    "positions": [],
                    "generated_at": None,
                    "error": "no_mt5_terminal_and_snapshot_missing",
                    "_source": "none",
                }
            )
            return
        try:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self._serve_json(
                {
                    "connected": False,
                    "position_count": 0,
                    "positions": [],
                    "generated_at": None,
                    "error": f"snapshot_parse_failed: {exc}",
                }
            )
            return
        for p in data.get("positions", []):
            pos_type = p.get("type", -1)
            p["side"] = "BUY" if pos_type == 0 else "SELL" if pos_type == 1 else "UNKNOWN"
        data["_source"] = "snapshot_cache"
        self._serve_json(data)

    def _serve_api_brains(self) -> None:
        """Merge tracker health + governance status + last direction per brain."""
        brains: dict[str, dict[str, Any]] = {}

        # 1. Performance tracker health
        tracker_path = self.BASE_DIR / "brain_performance.json"
        if tracker_path.exists():
            try:
                from core.feedback.brain_performance_tracker import (
                    BrainPerformanceTracker,
                )

                tracker = BrainPerformanceTracker.load(tracker_path)
                for s in tracker.get_all_summaries():
                    bid = s.get("brain_id", "")
                    brains[bid] = {
                        "brain_id": bid,
                        "health": s.get("health_signal", "insufficient_data"),
                        "confidence": round(s.get("composite_mean", 0), 4),
                        "sample_count": s.get("sample_count", 0),
                        "recommendation": s.get("recommendation", "observe"),
                        "status": "unknown",
                        "last_direction": "UNKNOWN",
                    }
            except Exception:
                pass

        # 2. Governance status
        gov_path = self.BASE_DIR / "governance_state.json"
        if gov_path.exists():
            try:
                from core.governance.governance_service import GovernanceService

                gov = GovernanceService.load(gov_path)
                for bid, state in gov.get_all_states().items():
                    if bid in brains:
                        brains[bid]["status"] = state.get("status", "unknown")
                        brains[bid]["freeze_count"] = state.get("freeze_count", 0)
                    else:
                        brains[bid] = {
                            "brain_id": bid,
                            "health": "unknown",
                            "confidence": 0,
                            "sample_count": 0,
                            "recommendation": "observe",
                            "status": state.get("status", "unknown"),
                            "freeze_count": state.get("freeze_count", 0),
                            "last_direction": "UNKNOWN",
                        }
            except Exception:
                pass

        # 2.5 PnL data from counterfactual ledger
        pnl_path = self.BASE_DIR / "brain_pnl_ledger.json"
        if pnl_path.exists():
            try:
                from core.feedback.brain_pnl_ledger import BrainPnLStore

                store = BrainPnLStore.load(pnl_path)
                for bid, metrics in store.get_all_metrics().items():
                    m = metrics.to_dict()
                    if bid in brains:
                        brains[bid]["pnl_total"] = m.get("cumulative_pnl", 0)
                        brains[bid]["pnl_win_rate"] = m.get("win_rate", 0)
                        brains[bid]["sharpe_ratio"] = m.get("sharpe_ratio", 0)
                        brains[bid]["pnl_samples"] = m.get("sample_count", 0)
            except Exception:
                pass

        # 3. Last direction from today's shadow decisions
        date = _today_key()
        dec_path = self.BASE_DIR / "decisions" / date / "XAUUSDc.decisions.jsonl"
        last_line = _read_last_line(dec_path)
        if last_line:
            try:
                rec = json.loads(last_line)
                attr = rec.get("attribution", {})
                consensus_dir = (
                    attr.get("consensus", {}).get("consensus", "neutral")
                    if isinstance(attr.get("consensus"), dict)
                    else "neutral"
                )
                # Normalize consensus to a canonical direction label
                dir_label = consensus_dir.upper()
                if dir_label in ("SPLIT", "NEUTRAL", "NO_RESULTS", "UNKNOWN"):
                    dir_label = "NEUTRAL"
                for bid in attr.get("supporting_brains", []):
                    if bid in brains:
                        brains[bid]["last_direction"] = dir_label
                for bid in attr.get("opposing_brains", []):
                    if bid in brains:
                        opp_dir = (
                            "SHORT"
                            if dir_label == "LONG"
                            else "LONG"
                            if dir_label == "SHORT"
                            else "NEUTRAL"
                        )
                        brains[bid]["last_direction"] = opp_dir
            except (json.JSONDecodeError, KeyError):
                pass

        self._serve_json({"brains": list(brains.values())})

    def _serve_api_journal(self) -> None:
        journal_path = self.BASE_DIR / "live_trade_journal.jsonl"
        date_prefix = _today_key()
        lines = _read_last_n_lines(journal_path, n=100)
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec_at = str(rec.get("recorded_at", ""))
            if rec_at.startswith(date_prefix):
                entries.append(rec)
            if len(entries) >= 20:
                break
        self._serve_json({"count": len(entries), "entries": entries})

    def _serve_api_decisions(self) -> None:
        date = _today_key()
        dec_dir = self.BASE_DIR / "decisions" / date

        shadow_path = dec_dir / "XAUUSDc.decisions.jsonl"
        live_path = dec_dir / "XAUUSDc.decisions.jsonl"

        def _parse_last(p: Path) -> dict[str, Any] | None:
            line = _read_last_line(p)
            if not line:
                return None
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return None
            attr = rec.get("attribution", {})
            consensus = attr.get("consensus", {})
            return {
                "event_time": rec.get("event_time", ""),
                "consensus": consensus.get(
                    "consensus", consensus.get("aggregated_bias", "unknown")
                ),
                "decision_action": rec.get("labels", {}).get("decision_action", "?"),
                "decision_side": rec.get("labels", {}).get("decision_side", "?"),
                "agreement_score": consensus.get("agreement_score"),
                "consensus_score": consensus.get("consensus_score"),
                "brains": {
                    "supporting": attr.get("supporting_brains", []),
                    "opposing": attr.get("opposing_brains", []),
                },
            }

        self._serve_json(
            {
                "shadow": _parse_last(shadow_path),
                "live": _parse_last(live_path),
            }
        )

    def _serve_api_slo(self) -> None:
        """Return SLO compliance data for 5 objectives with error budgets."""
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from core.observability.slo_service import SloService  # type: ignore[import-not-found]

            slo = SloService()
            report = slo.evaluate()
            self._serve_json(report)
        except Exception as exc:
            self._serve_json(
                {
                    "status": "unavailable",
                    "error": str(exc)[:200],
                    "objectives": {},
                    "generated_at": _utc_now_iso(),
                }
            )

    def _serve_api_alerts(self) -> None:
        """Return recent alert history from audit log + AlertService fired history."""
        alerts: list[dict[str, Any]] = []

        # 1. AlertService fired history (in-memory, most recent)
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from core.observability.alert_service import (
                AlertService,  # type: ignore[import-not-found]
            )

            fired = AlertService.with_default_rules().get_fired_history(limit=50)
            alerts.extend(fired)
        except Exception:
            pass

        # 2. Audit log entries with severity >= warning from today
        try:
            from core.observability.audit_log import (  # type: ignore[import-not-found]
                StructuredAuditLog,
            )

            audit = StructuredAuditLog(str(self.BASE_DIR))
            entries = audit.read_entries(date_key=_today_key())
            for e in entries:
                if e.get("severity") in ("warning", "error", "critical"):
                    alerts.append(
                        {
                            "rule_name": e.get("event_type", "audit"),
                            "severity": e.get("severity", "info"),
                            "fired_at": e.get("timestamp", ""),
                            "context_snapshot": e.get("detail", {}),
                            "source": "audit_log",
                        }
                    )
        except Exception:
            pass

        # Sort by fired_at descending, take last 50
        alerts.sort(key=lambda a: str(a.get("fired_at", "")), reverse=True)
        self._serve_json({"count": len(alerts[-50:]), "alerts": alerts[-50:]})

    def _serve_api_health(self) -> None:
        """Return unified health snapshot merging auto_healthcheck + diagnostics + resources."""
        report: dict[str, Any] = {
            "generated_at": _utc_now_iso(),
            "alert_level": "OK",
            "subsystems": {},
            "resources": _collect_system_resources(),
        }

        # 1. Auto healthcheck
        try:
            from scripts.live_auto_healthcheck import (  # type: ignore[import-not-found]
                build_report,
            )

            hc = build_report(
                base_dir=self.BASE_DIR,
                symbol="XAUUSDc",
                outbox_max_age_minutes=10,
            )
            report["alert_level"] = hc.get("alert_level", "OK")
            report["subsystems"]["bridge"] = {
                "status": "OK"
                if hc.get("flags", {}).get("bridge_supervisor", {}).get("fresh")
                else "WARNING",
                "detail": hc.get("flags", {}).get("bridge_supervisor", {}),
            }
            report["subsystems"]["outbox"] = {
                "status": "WARNING"
                if hc.get("flags", {}).get("outbox_staleness", {}).get("stale_count", 0) > 0
                else "OK",
                "detail": {
                    "pending": hc.get("flags", {}).get("outbox_pending", 0),
                    "stale": hc.get("flags", {}).get("outbox_staleness", {}).get("stale_count", 0),
                },
            }
            report["subsystems"]["dispatch_flag"] = {
                "status": "BLOCKED" if hc.get("flags", {}).get("flag", {}).get("exists") else "OK",
                "detail": hc.get("flags", {}).get("flag", {}),
            }
            report["primary_codes"] = hc.get("primary_codes", [])
        except Exception as exc:
            report["subsystems"]["healthcheck"] = {"status": "ERROR", "detail": str(exc)[:200]}

        # 2. Brain health summary
        try:
            from core.feedback.brain_performance_tracker import (
                BrainPerformanceTracker,  # type: ignore[import-not-found]
            )

            tracker_path = self.BASE_DIR / "brain_performance.json"
            if tracker_path.exists():
                tracker = BrainPerformanceTracker.load(tracker_path)
                summaries = tracker.get_all_summaries()
                healthy = sum(
                    1 for s in summaries if s.get("health_signal") in ("healthy", "stable")
                )
                degraded = sum(
                    1 for s in summaries if s.get("health_signal") in ("degraded", "critical")
                )
                insufficient = sum(
                    1 for s in summaries if s.get("health_signal") == "insufficient_data"
                )
                report["subsystems"]["brains"] = {
                    "status": "CRITICAL"
                    if degraded > 0
                    else ("WARNING" if insufficient > 0 else "OK"),
                    "detail": {
                        "total": len(summaries),
                        "healthy": healthy,
                        "degraded": degraded,
                        "insufficient_data": insufficient,
                    },
                }
        except Exception:
            pass

        # 3. Governance summary
        try:
            from core.governance.governance_service import (  # type: ignore[import-not-found]
                GovernanceService,
            )

            gov_path = self.BASE_DIR / "governance_state.json"
            if gov_path.exists():
                gov = GovernanceService.load(gov_path)
                states = gov.get_all_states()
                frozen = sum(1 for s in states.values() if s.get("status") == "frozen")
                live = sum(1 for s in states.values() if s.get("status") == "live")
                report["subsystems"]["governance"] = {
                    "status": "WARNING" if frozen > 0 else "OK",
                    "detail": {
                        "total": len(states),
                        "live": live,
                        "frozen": frozen,
                    },
                }
        except Exception:
            pass

        self._serve_json(report)

    def _serve_api_risk(self) -> None:
        """Return risk gate status: 5-policy summary + dispatch flag."""
        policies: list[dict[str, Any]] = []
        overall = "PASS"

        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from scripts.live_dispatch_policy import (
                build_parser as policy_parser,
            )
            from scripts.live_dispatch_policy import (  # type: ignore[import-not-found]
                load_gate_policy_config,
                run_policy,
            )

            flag = str(self.BASE_DIR / "live_dispatch_block.flag")
            p_args = policy_parser().parse_args(
                [
                    "--base-dir",
                    str(self.BASE_DIR),
                    "--symbol",
                    "XAUUSDc",
                    "--eval-only",
                    "--flag-path",
                    flag,
                ]
            )
            config = load_gate_policy_config(None)
            _code, result = run_policy(p_args, gate_config=config)

            if result.get("dispatch_blocked"):
                overall = "BLOCK"
            elif result.get("dispatch_warnings"):
                overall = "WARN"

            # Extract policy checks from sources (market_calendar, journal_quality, spread)
            sources = result.get("sources", {})
            for src_key, src in sources.items():
                src_blocked = src.get("blocked", False)
                policies.append(
                    {
                        "name": src_key,
                        "passed": not src_blocked,
                        "detail": "; ".join(src.get("reasons", [])) if src_blocked else "OK",
                        "source": src_key,
                    }
                )
        except Exception:
            # Fallback: read flag file directly
            flag_path = self.BASE_DIR / "live_dispatch_block.flag"
            if flag_path.exists():
                try:
                    raw = json.loads(flag_path.read_text(encoding="utf-8"))
                    overall = "BLOCK" if raw.get("blocked", True) else "WARN"
                except (json.JSONDecodeError, OSError):
                    overall = "WARN"
            policies = [
                {
                    "name": "DrawdownPolicy",
                    "passed": True,
                    "detail": "fallback — unable to evaluate live",
                },
                {"name": "PositionLimitPolicy", "passed": True, "detail": "fallback"},
                {"name": "ConcentrationPolicy", "passed": True, "detail": "fallback"},
                {"name": "ExposurePolicy", "passed": True, "detail": "fallback"},
                {"name": "ModePolicy", "passed": True, "detail": "fallback"},
            ]

        self._serve_json(
            {
                "generated_at": _utc_now_iso(),
                "overall": overall,
                "policies": policies,
                "flag_active": (self.BASE_DIR / "live_dispatch_block.flag").exists(),
            }
        )

    # ── NEW API: performance matrix ──

    def _serve_api_performance(self) -> None:
        """Return per-brain performance matrix (PnL, Sharpe, win rate, etc.)."""
        global _PERF_CACHE
        pnl_path = self.BASE_DIR / "brain_pnl_ledger.json"
        tracker_path = self.BASE_DIR / "brain_performance.json"
        gov_path = self.BASE_DIR / "governance_state.json"

        # Check cache
        now = time_module.time()
        pnl_mtime = pnl_path.stat().st_mtime if pnl_path.exists() else 0
        if (
            _PERF_CACHE["data"] is not None
            and (now - _PERF_CACHE["ts"]) < _PERF_CACHE_TTL
            and pnl_mtime == _PERF_CACHE["mtime"]
        ):
            self._serve_json(_PERF_CACHE["data"])
            return

        brains: list[dict[str, Any]] = []
        errors: list[str] = []

        # 1. Load PnL ledger (counterfactual performance)
        pnl_table: list[dict[str, Any]] = []
        if pnl_path.exists():
            try:
                from core.feedback.brain_pnl_ledger import BrainPnLStore

                store = BrainPnLStore.load(pnl_path)
                pnl_table = store.get_summary_table()
            except Exception as exc:
                errors.append(f"pnl_ledger: {exc}")

        # 2. Load BrainPerformanceTracker (composite scores, recommendations)
        tracker: dict[str, dict[str, Any]] = {}
        if tracker_path.exists():
            try:
                from core.feedback.brain_performance_tracker import (
                    BrainPerformanceTracker,
                )

                t = BrainPerformanceTracker.load(tracker_path)
                for s in t.get_all_summaries():
                    tracker[s["brain_id"]] = s
            except Exception as exc:
                errors.append(f"tracker: {exc}")

        # 3. Load GovernanceService (lifecycle status)
        gov_states: dict[str, dict[str, Any]] = {}
        if gov_path.exists():
            try:
                from core.governance.governance_service import GovernanceService

                g = GovernanceService.load(gov_path)
                gov_states = g.get_all_states()
            except Exception as exc:
                errors.append(f"governance: {exc}")

        # 4. Merge
        for pnl in pnl_table:
            bid = pnl.get("brain_id", "")
            entry = dict(pnl)
            entry["governance_status"] = gov_states.get(bid, {}).get("status", "unknown")
            entry["freeze_count"] = gov_states.get(bid, {}).get("freeze_count", 0)
            tinfo = tracker.get(bid, {})
            entry["composite_score"] = round(tinfo.get("composite_mean", 0), 4)
            entry["recommendation"] = tinfo.get("recommendation", "observe")
            brains.append(entry)

        # Add brains that exist in tracker/governance but not in PnL ledger
        seen = {b["brain_id"] for b in brains}
        for bid, tinfo in tracker.items():
            if bid not in seen:
                gs = gov_states.get(bid, {})
                brains.append(
                    {
                        "brain_id": bid,
                        "governance_status": gs.get("status", "unknown"),
                        "sample_count": tinfo.get("sample_count", 0),
                        "cumulative_pnl": 0.0,
                        "win_rate": 0.0,
                        "sharpe_ratio": 0.0,
                        "profit_factor": 0.0,
                        "max_drawdown": 0.0,
                        "avg_return": 0.0,
                        "recent_pnl_20": 0.0,
                        "long_win_rate": 0.0,
                        "short_win_rate": 0.0,
                        "long_count": 0,
                        "short_count": 0,
                        "health_signal": tinfo.get("health_signal", "insufficient_data"),
                        "composite_score": round(tinfo.get("composite_mean", 0), 4),
                        "recommendation": tinfo.get("recommendation", "observe"),
                        "freeze_count": gs.get("freeze_count", 0),
                    }
                )
                seen.add(bid)

        # Sort by Sharpe descending
        brains.sort(key=lambda b: b.get("sharpe_ratio", 0) or 0, reverse=True)

        result = {
            "generated_at": _utc_now_iso(),
            "schema_version": SCHEMA_VERSION,
            "brain_count": len(brains),
            "brains": brains,
            "errors": errors,
        }
        _PERF_CACHE = {"ts": now, "data": result, "mtime": pnl_mtime}
        self._serve_json(result)

    # ── NEW API: governance dashboard ──

    def _serve_api_governance(self) -> None:
        """Return governance actions: promotion queue, demotion warnings, freeze list, transitions."""
        tracker_path = self.BASE_DIR / "brain_performance.json"
        gov_path = self.BASE_DIR / "governance_state.json"
        errors: list[str] = []

        status_counts: dict[str, int] = {}
        promotion_queue: list[dict[str, Any]] = []
        demotion_warnings: list[dict[str, Any]] = []
        freeze_list: list[dict[str, Any]] = []
        recent_transitions: list[dict[str, Any]] = []

        # Governance states
        gov_states: dict[str, dict[str, Any]] = {}
        if gov_path.exists():
            try:
                from core.governance.governance_service import GovernanceService

                g = GovernanceService.load(gov_path)
                gov_states = g.get_all_states()
                # Status counts
                for s in gov_states.values():
                    st = s.get("status", "unknown")
                    status_counts[st] = status_counts.get(st, 0) + 1
                # Recent transitions
                tlog = g.get_transition_log()
                recent_transitions = list(tlog[-20:])
                recent_transitions.reverse()
            except Exception as exc:
                errors.append(f"governance: {exc}")

        # Tracker recommendations
        if tracker_path.exists():
            try:
                from core.feedback.brain_performance_tracker import (
                    BrainPerformanceTracker,
                )

                t = BrainPerformanceTracker.load(tracker_path)
                for s in t.get_all_summaries():
                    bid = s["brain_id"]
                    rec = s.get("recommendation", "")
                    gs = gov_states.get(bid, {})
                    entry = {
                        "brain_id": bid,
                        "recommendation": rec,
                        "composite_score": round(s.get("composite_mean", 0), 4),
                        "from_status": gs.get("status", "unknown"),
                        "sample_count": s.get("sample_count", 0),
                    }
                    if rec == "eligible_for_promotion":
                        promotion_queue.append(entry)
                    elif rec in ("demote_to_probation", "freeze", "limit_exposure"):
                        demotion_warnings.append(entry)
            except Exception as exc:
                errors.append(f"tracker: {exc}")

        # Freeze list from governance states
        for bid, gs in gov_states.items():
            if gs.get("status") == "frozen":
                freeze_list.append(
                    {
                        "brain_id": bid,
                        "status": "frozen",
                        "freeze_count": gs.get("freeze_count", 0),
                        "reason": gs.get("last_transition_reason", ""),
                    }
                )

        self._serve_json(
            {
                "generated_at": _utc_now_iso(),
                "status_counts": status_counts,
                "promotion_queue": promotion_queue,
                "demotion_warnings": demotion_warnings,
                "freeze_list": freeze_list,
                "recent_transitions": recent_transitions[:10],
                "errors": errors,
            }
        )

    # ── NEW API: analytics recommendations ──

    def _serve_api_analytics(self) -> None:
        """Return parameter tuning suggestions, retirement candidates, degraded brains."""
        suggestions_path = self.BASE_DIR / "reports" / "param_suggestions.json"
        tracker_path = self.BASE_DIR / "brain_performance.json"
        gov_path = self.BASE_DIR / "governance_state.json"
        errors: list[str] = []

        param_suggestions: list[dict[str, Any]] = []
        if suggestions_path.exists():
            try:
                data = json.loads(suggestions_path.read_text(encoding="utf-8"))
                param_suggestions = data.get("suggestions", [])
            except (json.JSONDecodeError, OSError) as exc:
                errors.append(f"param_suggestions: {exc}")

        retirement_candidates: list[dict[str, Any]] = []
        degraded_brains: list[dict[str, Any]] = []

        gov_states: dict[str, dict[str, Any]] = {}
        if gov_path.exists():
            try:
                from core.governance.governance_service import GovernanceService

                gov_states = GovernanceService.load(gov_path).get_all_states()
            except Exception:
                pass

        if tracker_path.exists():
            try:
                from core.feedback.brain_performance_tracker import (
                    BrainPerformanceTracker,
                )

                t = BrainPerformanceTracker.load(tracker_path)
                for s in t.get_all_summaries():
                    health = s.get("health_signal", "")
                    rec = s.get("recommendation", "")
                    n = s.get("sample_count", 0)
                    gs = gov_states.get(s["brain_id"], {})
                    current_status = gs.get("status", "unknown")
                    entry = {
                        "brain_id": s["brain_id"],
                        "health_signal": health,
                        "recommendation": rec,
                        "sample_count": n,
                        "composite_score": round(s.get("composite_mean", 0), 4),
                        "current_status": current_status,
                    }
                    if (
                        health in ("critical", "degraded")
                        and n >= 10
                        and current_status not in ("frozen", "retired")
                    ):
                        if rec == "freeze":
                            retirement_candidates.append(entry)
                        else:
                            degraded_brains.append(entry)
            except Exception as exc:
                errors.append(f"tracker: {exc}")

        self._serve_json(
            {
                "generated_at": _utc_now_iso(),
                "param_suggestions": param_suggestions,
                "retirement_candidates": retirement_candidates,
                "degraded_brains": degraded_brains,
                "errors": errors,
            }
        )

    # ── NEW API: module health ──

    def _serve_api_modules(self) -> None:
        """Return per-module health status with data freshness indicators."""
        errors: list[str] = []
        modules: dict[str, dict[str, Any]] = {}
        now = time_module.time()

        # 1. MT5 Bridge
        try:
            bridge_health_path = self.BASE_DIR / "reports" / "mt5_bridge_health.json"
            if bridge_health_path.exists():
                bh = json.loads(bridge_health_path.read_text(encoding="utf-8"))
                last_ts = bh.get("last_heartbeat_utc", "")
                connected = bh.get("connected", False)
                modules["mt5_bridge"] = {
                    "status": "OK" if connected else "WARNING",
                    "connected": connected,
                    "detail": bh.get("status", "unknown"),
                    "last_heartbeat": last_ts,
                }
            else:
                modules["mt5_bridge"] = {
                    "status": "WARNING",
                    "connected": False,
                    "detail": "no health file",
                }
        except Exception as exc:
            modules["mt5_bridge"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 2. Outbox
        try:
            outbox_dir = self.BASE_DIR / "mt5_outbox"
            pending = 0
            stale = 0
            if outbox_dir.exists():
                cutoff = now - 600  # 10 minutes stale
                for root, _dirs, files in os.walk(str(outbox_dir)):
                    for fn in files:
                        if fn.endswith(".json"):
                            pending += 1
                            fp = Path(root) / fn
                            if fp.stat().st_mtime < cutoff:
                                stale += 1
            modules["outbox"] = {
                "status": "CRITICAL" if stale > 3 else ("WARNING" if pending > 10 else "OK"),
                "pending": pending,
                "stale": stale,
            }
        except Exception as exc:
            modules["outbox"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 3. Feature Store
        try:
            fs_dir = self.BASE_DIR / "feature_store"
            if fs_dir.exists():
                max_mtime = 0
                for root, _dirs, files in os.walk(str(fs_dir)):
                    for fn in files:
                        fp = Path(root) / fn
                        if fp.stat().st_mtime > max_mtime:
                            max_mtime = fp.stat().st_mtime
                age_sec = now - max_mtime if max_mtime > 0 else 9999
                age_min = int(age_sec / 60)
                if age_min < 5:
                    fs_status = "OK"
                elif age_min < 30:
                    fs_status = "WARNING"
                else:
                    fs_status = "CRITICAL"
                modules["feature_store"] = {
                    "status": fs_status,
                    "available": True,
                    "freshness": f"{age_min}m ago" if age_min > 0 else "now",
                }
            else:
                modules["feature_store"] = {
                    "status": "WARNING",
                    "available": False,
                    "freshness": "not found",
                }
        except Exception as exc:
            modules["feature_store"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 4. Brain Adapters
        try:
            configs_dir = Path("configs/brains")
            configured = (
                len([f for f in configs_dir.glob("*.json") if "normalization" not in f.name])
                if configs_dir.exists()
                else 0
            )
            tracker_path = self.BASE_DIR / "brain_performance.json"
            with_data = 0
            if tracker_path.exists():
                t_data = json.loads(tracker_path.read_text(encoding="utf-8"))
                records = t_data.get("records", {})
                with_data = sum(1 for v in records.values() if v and len(v) > 0)
            active_count = with_data
            if configured > 0 and with_data == 0:
                adapter_status = "WARNING"
            elif configured > 0 and with_data < configured:
                adapter_status = "WARNING"
            else:
                adapter_status = "OK"
            modules["brain_adapters"] = {
                "status": adapter_status,
                "configured": configured,
                "with_data": with_data,
                "active": active_count,
            }
        except Exception as exc:
            modules["brain_adapters"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 5. Governance
        try:
            gov_path = self.BASE_DIR / "governance_state.json"
            if gov_path.exists():
                from core.governance.governance_service import GovernanceService

                g = GovernanceService.load(gov_path)
                states = g.get_all_states()
                live = sum(1 for s in states.values() if s.get("status") == "live")
                frozen = sum(1 for s in states.values() if s.get("status") == "frozen")
                modules["governance"] = {
                    "status": "WARNING" if frozen > 0 else "OK",
                    "live": live,
                    "frozen": frozen,
                    "total": len(states),
                }
            else:
                modules["governance"] = {"status": "WARNING", "detail": "not initialized"}
        except Exception as exc:
            modules["governance"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 6. Dispatch
        flag_path = self.BASE_DIR / "live_dispatch_block.flag"
        blocked = flag_path.exists()
        modules["dispatch"] = {
            "status": "CRITICAL" if blocked else "OK",
            "blocked": blocked,
        }

        # 7. Daily Ops
        try:
            recap_path = self.BASE_DIR / "reports" / "daily_recap.json"
            if recap_path.exists():
                age_h = (now - recap_path.stat().st_mtime) / 3600
                do_status = "OK" if age_h < 24 else "WARNING"
                modules["daily_ops"] = {
                    "status": do_status,
                    "last_recap": f"{age_h:.0f}h ago" if age_h > 0.5 else "recent",
                }
            else:
                modules["daily_ops"] = {"status": "WARNING", "last_recap": "never"}
        except Exception as exc:
            modules["daily_ops"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 8. Resources
        resources = _collect_system_resources()

        # Determine overall alert level
        statuses = [m.get("status", "OK") for m in modules.values()]
        if "CRITICAL" in statuses:
            alert_level = "CRITICAL"
        elif "ERROR" in statuses or statuses.count("WARNING") >= 2:
            alert_level = "WARNING"
        else:
            alert_level = "OK"

        primary_codes = [k for k, v in modules.items() if v.get("status") in ("CRITICAL", "ERROR")]

        self._serve_json(
            {
                "generated_at": _utc_now_iso(),
                "alert_level": alert_level,
                "modules": modules,
                "resources": resources,
                "primary_codes": primary_codes,
                "errors": errors,
            }
        )


# ── Server entry point ──


def run_dashboard_server(
    base_dir: str = "data",
    host: str = "127.0.0.1",
    port: int = 8080,
    mt5_terminal_path: str | None = None,
) -> HTTPServer:
    """Create and return the dashboard HTTP server.

    Args:
        base_dir: Base data directory.
        host: Bind address (default 127.0.0.1).
        port: TCP port (default 8080).
        mt5_terminal_path: Optional MT5 terminal path (e.g. D:\\MetaTrader 5\\terminal64.exe).
            When set, /api/positions will pull live data from MT5 with a thread timeout.
    """
    LiveDashboardHandler.BASE_DIR = Path(base_dir)
    LiveDashboardHandler.MT5_TERMINAL_PATH = mt5_terminal_path
    server = HTTPServer((host, port), LiveDashboardHandler)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live_trading_dashboard")
    parser.add_argument("--base-dir", default="data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--mt5-terminal-path",
        default=None,
        help="MT5 terminal path for live position fetching (e.g. D:\\MetaTrader 5\\terminal64.exe)",
    )
    args = parser.parse_args(argv)

    # Ensure project root is on sys.path for imports
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    server = run_dashboard_server(
        base_dir=args.base_dir,
        host=args.host,
        port=args.port,
        mt5_terminal_path=args.mt5_terminal_path,
    )
    url = f"http://{args.host}:{args.port}"
    source = "live MT5" if args.mt5_terminal_path else "cached snapshot"
    print(f"Dashboard: {url}")
    print(f"Position source: {source}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
