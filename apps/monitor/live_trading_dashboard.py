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
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent

SCHEMA_VERSION = "live_trading_dashboard.v1"

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
body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; padding: 12px; min-width: 1100px; }
header { display: flex; align-items: center; justify-content: space-between; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 10px 16px; margin-bottom: 10px; }
header h1 { font-size: 17px; font-weight: 600; letter-spacing: 0.3px; }
header .ts { color: var(--muted); font-size: 13px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.grid-3-2 { display: grid; grid-template-columns: 3fr 2fr; gap: 10px; margin-bottom: 10px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; }
.card h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 8px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--dim); font-weight: 500; padding: 4px 8px; border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; }
td { padding: 4px 8px; border-bottom: 1px solid rgba(51,65,85,0.4); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600; }
.badge-green { background: rgba(34,197,94,0.15); color: var(--green); }
.badge-yellow { background: rgba(234,179,8,0.15); color: var(--yellow); }
.badge-red { background: rgba(239,68,68,0.15); color: var(--red); }
.badge-blue { background: rgba(59,130,246,0.15); color: var(--blue); }
.green { color: var(--green); }
.yellow { color: var(--yellow); }
.red { color: var(--red); }
.muted { color: var(--muted); }
.dim { color: var(--dim); font-size: 12px; }
.num { font-variant-numeric: tabular-nums; }
.stat-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid rgba(51,65,85,0.3); }
.stat-row:last-child { border-bottom: none; }
.stat-val { font-size: 20px; font-weight: 700; }
.brain-ok { border-left: 3px solid var(--green); }
.brain-err { border-left: 3px solid var(--red); }
.brain-warn { border-left: 3px solid var(--yellow); }
.consensus-long { color: var(--green); font-weight: 700; }
.consensus-short { color: var(--red); font-weight: 700; }
.consensus-neutral, .consensus-split { color: var(--yellow); font-weight: 700; }
.decision-card { display: flex; gap: 16px; }
.decision-card > div { flex: 1; padding: 8px 12px; border-radius: 4px; background: rgba(15,23,42,0.5); }
.error-block { color: var(--red); font-style: italic; font-size: 12px; padding: 8px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.slo-row { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid rgba(51,65,85,0.3); font-size: 13px; }
.slo-row:last-child { border-bottom: none; }
.slo-bar-wrap { width: 140px; height: 14px; background: rgba(15,23,42,0.6); border-radius: 7px; overflow: hidden; position: relative; }
.slo-bar-fill { height: 100%; border-radius: 7px; transition: width 0.5s; }
.slo-bar-budget { height: 100%; border-radius: 7px; position: absolute; top: 0; opacity: 0.3; }
.alert-critical { border-left: 3px solid var(--red); }
.alert-warning { border-left: 3px solid var(--yellow); }
.health-ok { color: var(--green); }
.health-warn { color: var(--yellow); }
.health-critical { color: var(--red); }

</style>
</head>
<body>
<header>
  <div><h1>QUANT OS — LIVE TRADING DASHBOARD</h1></div>
  <div class="ts">UTC <span id="hdr-date">—</span> &nbsp; Refresh: <span id="hdr-refresh">—</span> &nbsp; Next: <span id="hdr-countdown">—</span>s</div>
  <div id="hdr-badge"></div>
</header>

<div class="grid-3">
  <div class="card">
    <h2>System Status</h2>
    <div id="panel-status"><span class="muted">Loading...</span></div>
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

<div class="grid-3-2">
  <div class="card">
    <h2>Brain Panel</h2>
    <div id="panel-brains"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>Trade Statistics</h2>
    <div id="panel-stats"><span class="muted">Loading...</span></div>
  </div>
</div>

<div class="card" style="margin-bottom:10px">
  <h2>Alert History</h2>
  <div id="panel-alerts" style="max-height:200px;overflow-y:auto"><span class="muted">Loading...</span></div>
</div>

<div class="grid-2">
  <div class="card">
    <h2>Current Position</h2>
    <div id="panel-positions"><span class="muted">Loading...</span></div>
  </div>
  <div class="card">
    <h2>Live Decisions</h2>
    <div id="panel-decisions"><span class="muted">Loading...</span></div>
  </div>
</div>

<div class="card" style="margin-bottom:10px">
  <h2>Recent Trades</h2>
  <div id="panel-journal" style="max-height:200px;overflow-y:auto"><span class="muted">Loading...</span></div>
</div>

<script>
var REFRESH_SEC = 10;
var COUNTDOWN = REFRESH_SEC;
var CONSECUTIVE_FAILS = 0;

function fmtTm(iso) { if (!iso) return '--:--:--'; return iso.replace('T',' ').substring(0,19); }
function fmtTime(iso) { if (!iso) return '--:--'; return iso.substring(11,19); }
function fmtPn(v) { if (v == null) return '$0.00'; var n = Number(v); return (n>=0?'+$':'-$') + Math.abs(n).toFixed(2); }
function fmtPct(v) { if (v == null) return '—'; return (v*100).toFixed(1)+'%'; }

function badge(label, cls) { return '<span class="badge badge-'+cls+'">'+label+'</span>'; }

function renderStatus(data) {
  var j = data.journal || {};
  var f = data.dispatch_flag || {};
  var total = j.total || 0, acc = j.accepted || 0, rej = j.rejected || 0, ack = j.acknowledged || 0;
  var statusLabel, statusCls;
  if (f.active) { statusLabel = 'BLOCKED'; statusCls = 'red'; }
  else if (total > 0) { statusLabel = 'ACTIVE'; statusCls = 'green'; }
  else { statusLabel = 'IDLE'; statusCls = 'yellow'; }
  document.getElementById('hdr-badge').innerHTML = badge(statusLabel, statusCls);
  var h = '<div class="stat-row"><span>Run State</span><span>' + badge(statusLabel, statusCls) + '</span></div>';
  h += '<div class="stat-row"><span>Journal Entries (today)</span><span class="num">' + total + '</span></div>';
  h += '<div class="stat-row"><span>Accepted</span><span class="green num">' + acc + '</span></div>';
  h += '<div class="stat-row"><span>Rejected</span><span class="'+(rej>0?'red':'muted')+' num">' + rej + '</span></div>';
  h += '<div class="stat-row"><span>Acknowledged</span><span class="muted num">' + ack + '</span></div>';
  h += '<div class="stat-row"><span>Dispatch Flag</span><span>' + (f.active ? badge('BLOCKED','red')+' <span class="dim">'+((f.payload||{}).reason||'')+'</span>' : badge('CLEAR','green')) + '</span></div>';
  if (data.errors && data.errors.length) h += '<div class="error-block">'+data.errors.length+' collector error(s)</div>';
  document.getElementById('panel-status').innerHTML = h;
}

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
  var h = '<table><tr><th>Ticket</th><th>Symbol</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>P&amp;L</th></tr>';
  for (var i=0; i<data.positions.length; i++) {
    var p = data.positions[i];
    var sideCls = p.side === 'BUY' ? 'green' : 'red';
    var pnl = p.profit != null ? p.profit : 0;
    var pnlCls = pnl >= 0 ? 'green' : 'red';
    h += '<tr><td class="num">' + (p.ticket||'?') + '</td><td>' + (p.symbol||'?') + '</td><td class="'+sideCls+'">' + (p.side||'?') + '</td><td class="num">' + (p.price_open||'?') + '</td><td class="num dim">' + (p.sl||'—') + '</td><td class="num dim">' + (p.tp||'—') + '</td><td class="num '+pnlCls+'">' + fmtPn(pnl) + '</td></tr>';
  }
  h += '</table>';
  if (data.generated_at) h += '<div class="dim" style="margin-top:6px">Snapshot: ' + fmtTm(data.generated_at) + '</div>';
  document.getElementById('panel-positions').innerHTML = h;
}

function renderBrains(data) {
  var brains = data.brains || [];
  if (!brains.length) { document.getElementById('panel-brains').innerHTML = '<span class="muted">No brain data</span>'; return; }
  var h = '<table><tr><th>Brain ID</th><th>Status</th><th>Health</th><th>Direction</th><th>Confidence</th><th>Samples</th></tr>';
  for (var i=0; i<brains.length; i++) {
    var b = brains[i];
    var stCls = {'live':'green','candidate':'yellow','probation':'yellow','frozen':'red'}[b.status] || 'muted';
    var hlCls = {'healthy':'green','stable':'green','degraded':'yellow','critical':'red','insufficient_data':'dim'}[b.health] || 'dim';
    var dirCls = b.last_direction === 'LONG' ? 'green' : b.last_direction === 'SHORT' ? 'red' : 'yellow';
    var conf = b.confidence != null ? (b.confidence*100).toFixed(1)+'%' : '—';
    var rowCls = b.health === 'critical' || b.status === 'frozen' ? 'brain-err' : (b.health === 'degraded' ? 'brain-warn' : 'brain-ok');
    h += '<tr class="'+rowCls+'"><td>' + (b.brain_id||'?') + '</td><td>' + badge(b.status||'?', stCls) + '</td><td>' + badge(b.health||'?', hlCls) + '</td><td class="'+dirCls+'">' + (b.last_direction||'NEUTRAL') + '</td><td class="num">' + conf + '</td><td class="num dim">' + (b.sample_count||0) + '</td></tr>';
  }
  h += '</table>';
  document.getElementById('panel-brains').innerHTML = h;
}

function renderStats(data) {
  var labels = data.labels || {};
  var journal = data.journal || {};
  var lst = labels;
  var wins = lst.wins || 0, losses = lst.losses || 0, total = lst.total || 0, pnl = lst.total_pnl || 0, wr = lst.win_rate;
  var rej = journal.rejected || 0, acc = journal.accepted || 0;
  var h = '';
  h += '<div class="stat-row"><span>Closed Trades</span><span class="stat-val num">' + total + '</span></div>';
  h += '<div class="stat-row"><span>Win Rate</span><span class="stat-val num '+(wr!=null?(wr>=0.5?'green':'red'):'dim')+'">' + fmtPct(wr) + '</span></div>';
  h += '<div class="stat-row"><span>Wins / Losses</span><span><span class="green num">'+wins+'</span> / <span class="red num">'+losses+'</span></span></div>';
  h += '<div class="stat-row"><span>Total P&amp;L</span><span class="stat-val num '+(pnl>=0?'green':'red')+'">' + fmtPn(pnl) + '</span></div>';
  h += '<div class="stat-row"><span>Accepted / Rejected</span><span><span class="green num">'+acc+'</span> / <span class="'+(rej>0?'red':'muted')+' num">'+rej+'</span></span></div>';
  document.getElementById('panel-stats').innerHTML = h;
}

function renderJournal(data) {
  var entries = data.entries || [];
  if (!entries.length) { document.getElementById('panel-journal').innerHTML = '<span class="muted">No trades today</span>'; return; }
  var h = '<table><tr><th>Time</th><th>Symbol</th><th>Action</th><th>Side</th><th>Status</th><th>SL</th><th>TP</th></tr>';
  for (var i=0; i<entries.length; i++) {
    var e = entries[i];
    var stCls = {'accepted':'green','rejected':'red','acknowledged':'blue','closed':'dim'}[e.ack_status] || 'muted';
    var sideCls = e.side === 'long' ? 'green' : e.side === 'short' ? 'red' : '';
    h += '<tr><td class="dim num">' + fmtTime(e.recorded_at) + '</td><td>' + (e.symbol||'?') + '</td><td>' + (e.action||'?') + '</td><td class="'+sideCls+'">' + (e.side||'?') + '</td><td>' + badge(e.ack_status||'?', stCls) + '</td><td class="num dim">' + (e.sl||'—') + '</td><td class="num dim">' + (e.tp||'—') + '</td></tr>';
  }
  h += '</table>';
  document.getElementById('panel-journal').innerHTML = h;
}

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
  var sup = (att.supporting||[]).join(', ') || '—';
  var opp = (att.opposing||[]).join(', ') || '—';
  return '<div><span class="dim">'+title+'</span><br><span class="'+cls+'" style="font-size:16px">' + c.toUpperCase() + '</span><br><span class="muted">'+ (d.decision_action||'?') + ' &middot; ' + (d.decision_side||'?') + '</span><br><span class="dim" style="font-size:11px">'+fmtTm(d.event_time)+'</span><br><span class="dim">Agreement: '+((d.agreement_score||d.consensus_score||0)*100).toFixed(1)+'%</span><br><span class="green">Supp: '+sup+'</span><br><span class="red">Opp: '+opp+'</span></div>';
}

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
    var isAbove = o.direction !== 'below';
    var displayVal = (val*100).toFixed(1)+'%';
    var displayTgt = (tgt*100).toFixed(1)+'%';
    var barW = Math.min(100, Math.max(0, (val/tgt)*100));
    var barCls = met ? 'var(--green)' : 'var(--red)';
    var budgetCls = budget > 20 ? 'rgba(34,197,94,0.3)' : (budget > 0 ? 'rgba(234,179,8,0.5)' : 'rgba(239,68,68,0.6)');
    h += '<div class="slo-row"><span style="width:110px;font-size:12px">'+ (labels[names[i]]||names[i]) +'</span>';
    h += '<span class="num" style="width:55px;font-size:12px">'+displayVal+'</span>';
    h += '<div class="slo-bar-wrap"><div class="slo-bar-fill" style="width:'+barW+'%;background:'+barCls+'"></div><div class="slo-bar-budget" style="width:'+budget+'%;background:'+budgetCls+'"></div></div>';
    h += '<span class="num dim" style="width:40px;font-size:11px">'+budget.toFixed(0)+'%</span></div>';
  }
  var statusCls = data.status === 'healthy' ? 'green' : 'red';
  h += '<div style="margin-top:6px;font-size:12px">Status: <span class="'+statusCls+'">' + (data.status||'?').toUpperCase() + '</span>';
  if (data.failed_objectives && data.failed_objectives.length) {
    h += ' &middot; <span class="red">'+data.failed_objectives.length+' breaching</span>';
  }
  h += '</div>';
  document.getElementById('panel-slo').innerHTML = h;
}

function renderRisk(data) {
  var policies = data.policies || [];
  var h = '';
  h += '<div class="stat-row"><span>Overall</span><span>' + badge(data.overall||'PASS', data.overall==='BLOCK'?'red':(data.overall==='WARN'?'yellow':'green')) + '</span></div>';
  for (var i=0; i<policies.length; i++) {
    var p = policies[i];
    h += '<div class="stat-row"><span style="font-size:12px">' + (p.name||'?') + '</span><span>' + badge(p.passed?'PASS':'BLOCK', p.passed?'green':'red') + '</span></div>';
    if (p.detail) h += '<div class="dim" style="font-size:11px;padding-left:8px">' + p.detail + '</div>';
  }
  if (data.flag_active) h += '<div class="stat-row"><span>Dispatch Flag</span><span>' + badge('ACTIVE','red') + '</span></div>';
  document.getElementById('panel-risk').innerHTML = h;
}

function renderAlerts(data) {
  var alerts = data.alerts || [];
  if (!alerts.length) { document.getElementById('panel-alerts').innerHTML = '<span class="muted">No alerts fired</span>'; return; }
  var h = '<table><tr><th>Time</th><th>Severity</th><th>Rule</th><th>Context</th></tr>';
  for (var i=0; i<Math.min(alerts.length, 30); i++) {
    var a = alerts[i];
    var sevCls = {'critical':'red','error':'red','warning':'yellow'}[a.severity] || 'muted';
    var ctx = a.context_snapshot || {};
    var ctxStr = Object.keys(ctx).slice(0,3).map(function(k){return k+'='+ctx[k]}).join(' ');
    h += '<tr class="'+(a.severity==='critical'?'alert-critical':(a.severity==='warning'?'alert-warning':''))+'"><td class="dim num">' + fmtTime(a.fired_at) + '</td><td>' + badge(a.severity||'?', sevCls) + '</td><td style="font-size:12px">' + (a.rule_name||'?') + '</td><td class="dim" style="font-size:11px">' + (ctxStr||'—') + '</td></tr>';
  }
  h += '</table>';
  document.getElementById('panel-alerts').innerHTML = h;
}

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

var HEALTH_CACHE = null;

function mergeHealth(healthData) {
  HEALTH_CACHE = healthData;
  // Append health rows to the existing status panel
  var panel = document.getElementById('panel-status');
  if (!panel || !healthData) return healthData;
  return healthData;
}

function appendHealthExtras() {
  if (!HEALTH_CACHE) return;
  var panel = document.getElementById('panel-status');
  if (!panel) return;
  var h = panel.innerHTML;
  // Remove any previously appended health extras
  var marker = '<!-- health-extras -->';
  var idx = h.indexOf(marker);
  if (idx >= 0) h = h.substring(0, idx);
  var extra = marker;
  var subs = HEALTH_CACHE.subsystems || {};
  var res = HEALTH_CACHE.resources || {};
  var alertLvl = HEALTH_CACHE.alert_level || 'OK';
  var alertCls = alertLvl === 'CRITICAL' ? 'red' : (alertLvl === 'WARNING' ? 'yellow' : 'green');
  extra += '<div class="stat-row"><span>Health Check</span><span>' + badge(alertLvl, alertCls) + '</span></div>';
  if (subs.bridge) extra += '<div class="stat-row"><span>Bridge</span><span class="'+(subs.bridge.status==='OK'?'green':'yellow')+'">'+(subs.bridge.status||'?')+'</span></div>';
  if (subs.outbox) extra += '<div class="stat-row"><span>Outbox (pending/stale)</span><span class="num">'+(subs.outbox.detail.pending||0)+' / '+(subs.outbox.detail.stale||0)+'</span></div>';
  if (subs.brains) {
    var b = subs.brains.detail || {};
    extra += '<div class="stat-row"><span>Brain Health</span><span><span class="green">'+b.healthy+'</span> / <span class="red">'+b.degraded+'</span> / <span class="dim">'+b.insufficient_data+'</span></span></div>';
  }
  if (subs.governance) {
    var g = subs.governance.detail || {};
    extra += '<div class="stat-row"><span>Gov (live/frozen)</span><span><span class="green">'+g.live+'</span> / <span class="red">'+g.frozen+'</span></span></div>';
  }
  if (res.available) {
    extra += '<div class="stat-row"><span>CPU / Mem / Disk</span><span class="num dim">'+(res.cpu_pct!=null?res.cpu_pct.toFixed(0)+'%':'?')+' / '+(res.memory_pct!=null?res.memory_pct.toFixed(0)+'%':'?')+' / '+(res.disk_pct!=null?res.disk_pct.toFixed(0)+'%':'?')+'</span></div>';
  }
  if (HEALTH_CACHE.primary_codes && HEALTH_CACHE.primary_codes.length) {
    extra += '<div class="stat-row"><span>Codes</span><span class="dim" style="font-size:11px">'+HEALTH_CACHE.primary_codes.join(', ')+'</span></div>';
  }
  panel.innerHTML = h + extra;
}

function refreshAll() {
  updateTimestamp();
  var promises = [
    doFetch('/api/dashboard', function(data) {
      renderStatus(data);
      renderStats(data);
      appendHealthExtras();
    }),
    doFetch('/api/positions', renderPositions),
    doFetch('/api/brains', renderBrains),
    doFetch('/api/journal', renderJournal),
    doFetch('/api/decisions', renderDecisions),
    doFetch('/api/slo', renderSLO),
    doFetch('/api/alerts', renderAlerts),
    doFetch('/api/health', function(data) {
      HEALTH_CACHE = data;
      appendHealthExtras();
    }),
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
