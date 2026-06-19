"""Live trading dashboard server — 量化交易实盘监控面板

Single-file HTTP server serving an auto-refreshing HTML dashboard.
Zero new dependencies — uses stdlib http.server + existing project modules.

Usage:
  python apps/monitor/live_trading_dashboard.py
  python apps/monitor/live_trading_dashboard.py --port 8080 --base-dir data
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time as time_module
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent

SCHEMA_VERSION = "live_trading_dashboard.v2"
logger = logging.getLogger("live_trading_dashboard")

# ── Performance data cache ──
_PERF_CACHE: dict[str, Any] = {"ts": 0.0, "data": None, "mtime": 0.0}
_PERF_CACHE_TTL = 30.0

# ── Unified health cache ──
_UNIFIED_HEALTH_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_UNIFIED_HEALTH_CACHE_TTL = 10.0

# ═══════════════════════════════════════════════════════════════════════════════
# HTML TEMPLATE — 中文界面 / 整洁布局 / 模型详情
# ═══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>量化交易系统 — 实盘监控面板</title>
<style>
:root {
  --bg: #0f172a; --panel: #1e293b; --border: #334155;
  --text: #e2e8f0; --muted: #94a3b8; --dim: #64748b;
  --green: #22c55e; --yellow: #eab308; --red: #ef4444; --blue: #3b82f6;
  --accent: #6366f1;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Microsoft YaHei','PingFang SC','Noto Sans SC','Segoe UI',system-ui,sans-serif; padding: 10px; min-width: 1340px; }
header { display: flex; align-items: center; justify-content: space-between; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 18px; margin-bottom: 10px; }
header h1 { font-size: 17px; font-weight: 600; letter-spacing: 0.3px; }
header .ts { color: var(--muted); font-size: 13px; display: flex; gap: 16px; align-items: center; }
.header-badges { display: flex; gap: 8px; }

/* Grids */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.grid-4 { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 10px; margin-bottom: 10px; }

.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.card h2 { font-size: 13px; font-weight: 600; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 8px; }
.card-full { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
.card-full h2 { font-size: 13px; font-weight: 600; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 8px; }

/* Tabs */
.tab-bar { display: flex; gap: 2px; margin-bottom: 10px; border-bottom: 1px solid var(--border); padding-bottom: 0; }
.tab-btn { padding: 6px 16px; font-size: 12px; font-weight: 500; border: 1px solid transparent; border-radius: 6px 6px 0 0; cursor: pointer; background: transparent; color: var(--muted); font-family: inherit; }
.tab-btn:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.tab-btn.active { color: var(--text); background: var(--panel); border-color: var(--border); border-bottom-color: var(--panel); margin-bottom: -1px; }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--dim); font-weight: 500; padding: 4px 8px; border-bottom: 1px solid var(--border); font-size: 12px; white-space: nowrap; cursor: pointer; user-select: none; }
th:hover { color: var(--text); }
th .sort-arrow { font-size: 10px; margin-left: 2px; }
td { padding: 3px 8px; border-bottom: 1px solid rgba(51,65,85,0.4); }
.dense-table { font-size: 12px; }
.dense-table th { font-size: 11px; padding: 3px 7px; }
.dense-table td { padding: 3px 7px; }
.clickable-row { cursor: pointer; }
.clickable-row:hover { background: rgba(99,102,241,0.08); }
.clickable-row.selected { background: rgba(99,102,241,0.15); border-left: 3px solid var(--accent); }
.scroll-y { max-height: 520px; overflow-y: auto; }

/* Badges */
.badge { display: inline-block; padding: 3px 10px; border-radius: 9999px; font-size: 11px; font-weight: 600; }
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
.slo-bar-wrap { width: 120px; height: 14px; background: rgba(15,23,42,0.6); border-radius: 7px; overflow: hidden; position: relative; }
.slo-bar-fill { height: 100%; border-radius: 7px; transition: width 0.5s; }
.slo-bar-budget { height: 100%; border-radius: 7px; position: absolute; top: 0; opacity: 0.3; }

/* Decisions */
.decision-card { display: flex; gap: 16px; }
.decision-card > div { flex: 1; padding: 8px 12px; border-radius: 6px; background: rgba(15,23,42,0.5); }
.consensus-long { color: var(--green); font-weight: 700; }
.consensus-short { color: var(--red); font-weight: 700; }
.consensus-neutral, .consensus-split { color: var(--yellow); font-weight: 700; }

/* Alerts */
.alert-critical { border-left: 3px solid var(--red); }
.alert-warning { border-left: 3px solid var(--yellow); }

/* Governance */
.gov-section { margin-bottom: 10px; }
.gov-section h3 { font-size: 12px; font-weight: 600; letter-spacing: 0.4px; color: var(--dim); margin-bottom: 4px; }
.gov-item { display: flex; justify-content: space-between; align-items: center; padding: 3px 0; font-size: 12px; border-bottom: 1px solid rgba(51,65,85,0.2); }
.gov-item:last-child { border-bottom: none; }
.transition-log { font-size: 11px; max-height: 160px; overflow-y: auto; }
.transition-log .tl-entry { padding: 2px 0; border-bottom: 1px solid rgba(51,65,85,0.2); display: flex; justify-content: space-between; }

/* Brain Detail */
.brain-detail-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 10px; }
.brain-detail-grid .metric-box { background: rgba(15,23,42,0.5); border-radius: 6px; padding: 10px 12px; }
.brain-detail-grid .metric-box h4 { font-size: 11px; font-weight: 500; color: var(--dim); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.3px; }
.brain-detail-grid .metric-box .metric-val { font-size: 18px; font-weight: 700; }
.sparkline-wrap { display: flex; align-items: flex-end; gap: 2px; height: 50px; padding: 4px 0; }
.sparkline-bar { flex: 1; min-width: 3px; border-radius: 1px; transition: height 0.3s; }
.direction-bar { display: flex; height: 20px; border-radius: 4px; overflow: hidden; margin-top: 4px; }
.direction-bar .bar-seg { height: 100%; transition: width 0.3s; }
.direction-bar .bar-long { background: var(--green); }
.direction-bar .bar-short { background: var(--red); }
.direction-bar .bar-neutral { background: var(--dim); }
.detail-empty { color: var(--dim); font-style: italic; padding: 20px; text-align: center; }

/* Error */
.error-block { color: var(--red); font-style: italic; font-size: 12px; padding: 8px; }

/* Dense inner tabs for bottom panel */
.inner-tabs { display: flex; gap: 2px; margin-bottom: 6px; }
.inner-tab { padding: 3px 12px; font-size: 11px; border: 1px solid var(--border); border-radius: 4px; cursor: pointer; background: transparent; color: var(--muted); font-family: inherit; }
.inner-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.inner-panel { display: none; }
.inner-panel.active { display: block; }
</style>
</head>
<body>

<!-- HEADER -->
<header>
  <div><h1>📊 量化交易系统 — 实盘监控面板</h1></div>
  <div class="ts">
    <span>UTC <span id="hdr-date">—</span></span>
    <span>刷新: <span id="hdr-refresh">—</span></span>
    <span>下次: <span id="hdr-countdown">—</span>秒</span>
  </div>
  <div class="header-badges">
    <span id="hdr-sys-badge"></span>
    <span id="hdr-alert-badge"></span>
  </div>
</header>

<!-- ROW 1: 系统状态概览 (4列) -->
<div class="grid-4">
  <div class="card"><h2>系统状态</h2><div id="panel-status"><span class="muted">加载中...</span></div></div>
  <div class="card"><h2>模块健康</h2><div id="panel-modules"><span class="muted">加载中...</span></div></div>
  <div class="card"><h2>SLO 合规</h2><div id="panel-slo"><span class="muted">加载中...</span></div></div>
  <div class="card"><h2>风控关卡</h2><div id="panel-risk"><span class="muted">加载中...</span></div></div>
</div>

<!-- ROW 2: 模型绩效矩阵 + 模型详情 (tab切换) -->
<div class="card-full">
  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchMainTab('perf')">📋 模型绩效矩阵</button>
    <button class="tab-btn" onclick="switchMainTab('detail')" id="tab-detail-btn">🔍 模型详情 <span id="detail-brain-label" class="dim"></span></button>
  </div>
  <div id="tab-perf" class="tab-content active">
    <div class="scroll-y" id="panel-performance"><span class="muted">加载中...</span></div>
  </div>
  <div id="tab-detail" class="tab-content">
    <div id="panel-brain-detail"><span class="detail-empty">👈 请先在绩效矩阵中点击选择一个模型</span></div>
  </div>
</div>

<!-- ROW 3: 实时交易 (3列) -->
<div class="grid-3">
  <div class="card"><h2>大脑信号 (实时)</h2><div id="panel-brains"><span class="muted">加载中...</span></div></div>
  <div class="card"><h2>实盘决策</h2><div id="panel-decisions"><span class="muted">加载中...</span></div></div>
  <div class="card"><h2>当前持仓</h2><div id="panel-positions"><span class="muted">加载中...</span></div></div>
</div>

<!-- ROW 4: 治理 + 分析 + 告警/日志 (tab切换) -->
<div class="grid-3">
  <div class="card"><h2>治理操作</h2><div id="panel-governance"><span class="muted">加载中...</span></div></div>
  <div class="card"><h2>分析与建议</h2><div id="panel-analytics"><span class="muted">加载中...</span></div></div>
  <div class="card">
    <div class="inner-tabs">
      <button class="inner-tab active" onclick="switchInnerTab('alerts')">告警</button>
      <button class="inner-tab" onclick="switchInnerTab('journal')">交易日志</button>
    </div>
    <div id="inner-alerts" class="inner-panel active" style="max-height:300px;overflow-y:auto">
      <div id="panel-alerts"><span class="muted">加载中...</span></div>
    </div>
    <div id="inner-journal" class="inner-panel" style="max-height:300px;overflow-y:auto">
      <div id="panel-journal"><span class="muted">加载中...</span></div>
    </div>
  </div>
</div>

<script>
var REFRESH_SEC = 10;
var COUNTDOWN = REFRESH_SEC;
var CONSECUTIVE_FAILS = 0;
var PERF_DATA = null;
var PERF_SORT = {col: 4, asc: false};  // default: Sharpe desc
var SELECTED_BRAIN = null;

// ── Utilities ──

function fmtTm(iso) { if (!iso) return '--:--:--'; return iso.replace('T',' ').substring(0,19); }
function fmtTime(iso) { if (!iso) return '--:--'; return iso.substring(11,19); }
function fmtPn(v) { if (v == null) return '$0.00'; var n = Number(v); return (n>=0?'+$':'-$') + Math.abs(n).toFixed(2); }
function fmtPct(v) { if (v == null) return '--'; return (v*100).toFixed(1)+'%'; }
function fmtNum(v, dec) { if (v == null) return '--'; dec = (dec == null ? 2 : dec); return Number(v).toFixed(dec); }
function badge(label, cls) { return '<span class="badge badge-'+cls+'">'+label+'</span>'; }
function timeAgo(iso) { if (!iso) return '--'; var diff = (Date.now() - Date.parse(iso))/1000; if (diff<60) return Math.floor(diff)+'秒前'; if (diff<3600) return Math.floor(diff/60)+'分钟前'; if (diff<86400) return Math.floor(diff/3600)+'小时前'; return Math.floor(diff/86400)+'天前'; }

// Status/health labels in Chinese
var STATUS_CN = {
  ACTIVE:'运行中', IDLE:'空闲', BLOCKED:'已阻断', CLEAR:'放行',
  live:'在线', frozen:'冻结', candidate:'候选', probation:'观察', shadow:'影子', retired:'退役', unknown:'未知',
  healthy:'健康', stable:'稳定', degraded:'降级', critical:'严重', insufficient_data:'数据不足',
  eligible_for_promotion:'可晋升', demote_to_probation:'降为观察', freeze:'冻结', limit_exposure:'限制敞口', observe:'观察',
  OK:'正常', WARNING:'警告', ERROR:'错误', CRITICAL:'严重', PASS:'通过', WARN:'警告', BLOCK:'阻断'
};
var STATUS_CLS = {
  ACTIVE:'green', IDLE:'yellow', BLOCKED:'red', CLEAR:'green',
  live:'green', frozen:'red', candidate:'yellow', probation:'yellow', shadow:'dim', retired:'dim',
  healthy:'green', stable:'green', degraded:'yellow', critical:'red', insufficient_data:'dim',
  eligible_for_promotion:'green', demote_to_probation:'red', freeze:'red', limit_exposure:'yellow', observe:'muted',
  OK:'green', WARNING:'yellow', ERROR:'red', CRITICAL:'red', PASS:'green', WARN:'yellow', BLOCK:'red'
};

function cn(v) { return STATUS_CN[v] || v; }
function statusCls(v) { return STATUS_CLS[v] || 'muted'; }

function cellColor(val, gTh, rTh, hi) {
  if (val == null || isNaN(val)) return '';
  if (hi === false) { if (val <= gTh) return 'cell-green'; if (val > rTh) return 'cell-red'; return 'cell-yellow'; }
  if (val >= gTh) return 'cell-green'; if (val < rTh) return 'cell-red'; return 'cell-yellow';
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
  return PERF_SORT.asc ? ' <span class="sort-arrow">▲</span>' : ' <span class="sort-arrow">▼</span>';
}

// ── Tab switching ──

function switchMainTab(tab) {
  document.querySelectorAll('#tab-perf, #tab-detail').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.tab-btn').forEach(function(el) { el.classList.remove('active'); });
  document.getElementById('tab-'+tab).classList.add('active');
  if (tab === 'perf') document.querySelector('.tab-btn:nth-child(1)').classList.add('active');
  else document.querySelector('.tab-btn:nth-child(2)').classList.add('active');
}

function switchInnerTab(tab) {
  document.querySelectorAll('#inner-alerts, #inner-journal').forEach(function(el) { el.classList.remove('active'); });
  document.querySelectorAll('.inner-tab').forEach(function(el) { el.classList.remove('active'); });
  document.getElementById('inner-'+tab).classList.add('active');
  document.querySelector('.inner-tab:nth-child(' + (tab==='alerts'?1:2) + ')').classList.add('active');
}

function selectBrain(brainId) {
  SELECTED_BRAIN = brainId;
  document.getElementById('detail-brain-label').textContent = '— ' + brainId;
  document.getElementById('tab-detail-btn').classList.add('active');
  // Highlight row
  var rows = document.querySelectorAll('#perf-table tbody tr');
  for (var i = 0; i < rows.length; i++) {
    rows[i].classList.remove('selected');
    if (rows[i].getAttribute('data-brain') === brainId) rows[i].classList.add('selected');
  }
  // Fetch detail
  fetch('/api/brain/' + encodeURIComponent(brainId), {signal: AbortSignal.timeout(8000)})
    .then(function(r) { if (!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(function(data) { renderBrainDetail(data); switchMainTab('detail'); })
    .catch(function(err) { document.getElementById('panel-brain-detail').innerHTML = '<span class="red">加载失败: '+err.message+'</span>'; switchMainTab('detail'); });
}

// ── Sparkline ──

function drawSparkline(values, width, height, positive) {
  if (!values || values.length < 2) return '<span class="dim" style="font-size:11px">数据不足</span>';
  var min = Math.min.apply(null, values.concat([0]));
  var max = Math.max.apply(null, values.concat([0]));
  var range = max - min || 1;
  var pts = [];
  var w = width || 120, h = height || 40;
  var pad = 2;
  for (var i = 0; i < values.length; i++) {
    var x = pad + (i / (values.length - 1)) * (w - pad * 2);
    var y = pad + (1 - (values[i] - min) / range) * (h - pad * 2);
    pts.push(x + ',' + y);
  }
  var stroke = positive !== false ? 'var(--green)' : 'var(--red)';
  if (positive === undefined) {
    var last = values[values.length-1], first = values[0];
    stroke = last >= first ? 'var(--green)' : 'var(--red)';
  }
  return '<svg width="'+w+'" height="'+h+'" style="display:block"><polyline points="'+pts.join(' ')+'" fill="none" stroke="'+stroke+'" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
}

// ══════════════════════ RENDER FUNCTIONS ══════════════════════

function renderStatus(data) {
  var j = data.journal || {};
  var f = data.dispatch_flag || {};
  var total = j.total || 0, acc = j.accepted || 0, rej = j.rejected || 0, ack = j.acknowledged || 0;
  var statusLabel, statusCls;
  if (f.active) { statusLabel = 'BLOCKED'; statusCls = 'red'; }
  else if (total > 0) { statusLabel = 'ACTIVE'; statusCls = 'green'; }
  else { statusLabel = 'IDLE'; statusCls = 'yellow'; }
  document.getElementById('hdr-sys-badge').innerHTML = badge(cn(statusLabel), statusCls);
  var h = '';
  h += '<div class="stat-row"><span>运行状态</span><span>' + badge(cn(statusLabel), statusCls) + '</span></div>';
  h += '<div class="stat-row"><span>今日日志</span><span class="num">' + total + '</span></div>';
  h += '<div class="stat-row"><span>已接受 / 已拒绝</span><span><span class="green num">'+acc+'</span> / <span class="'+(rej>0?'red':'muted')+' num">'+rej+'</span></span></div>';
  h += '<div class="stat-row"><span>已确认</span><span class="muted num">' + ack + '</span></div>';
  h += '<div class="stat-row"><span>调度开关</span><span>' + (f.active ? badge(cn('BLOCKED'),'red')+' <span class="dim">'+((f.payload||{}).reason||'')+'</span>' : badge(cn('CLEAR'),'green')) + '</span></div>';
  if (data.errors && data.errors.length) h += '<div class="error-block">'+data.errors.length+' 个采集错误</div>';
  var labels = data.labels || {};
  if (labels.total != null) {
    var wr = labels.win_rate;
    var pnl = labels.total_pnl || 0;
    h += '<div class="stat-row" style="margin-top:4px"><span>盈亏 / 胜率</span><span><span class="'+(pnl>=0?'green':'red')+' num">'+fmtPn(pnl)+'</span> <span class="dim">'+fmtPct(wr)+'</span></span></div>';
  }
  document.getElementById('panel-status').innerHTML = h;
}

function renderModules(data) {
  if (!data || !data.modules) {
    document.getElementById('panel-modules').innerHTML = '<span class="muted">无模块数据</span>';
    return;
  }
  var mods = data.modules;
  var modOrder = ['mt5_bridge','outbox','feature_store','brain_adapters','governance','dispatch','daily_ops'];
  var labels = {mt5_bridge:'MT5 桥接',outbox:'发件箱',feature_store:'特征库',brain_adapters:'大脑适配器',governance:'治理',dispatch:'调度',daily_ops:'每日运维'};
  var h = '';
  for (var i = 0; i < modOrder.length; i++) {
    var k = modOrder[i];
    var m = mods[k];
    if (!m) continue;
    var st = m.status || 'OK';
    var rowCls = st === 'CRITICAL' || st === 'ERROR' ? 'module-critical' : (st === 'WARNING' ? 'module-warn' : 'module-ok');
    var detail = '';
    if (k === 'mt5_bridge') detail = (m.connected ? '已连接' : '未连接') + (m.last_heartbeat ? ' '+timeAgo(m.last_heartbeat) : '');
    else if (k === 'outbox') detail = '待处理='+(m.pending||0)+' 过期='+(m.stale||0);
    else if (k === 'feature_store') detail = m.freshness || (m.available ? '正常' : '缺失');
    else if (k === 'brain_adapters') detail = (m.with_data||0)+'/'+(m.configured||0)+' 有数据';
    else if (k === 'governance') detail = '在线='+(m.live||0)+' 冻结='+(m.frozen||0);
    else if (k === 'dispatch') detail = m.blocked ? '已阻断' : '放行';
    else if (k === 'daily_ops') detail = m.last_recap || '--';
    else detail = '';
    h += '<div class="stat-row '+rowCls+'" style="padding:4px 6px"><span style="font-size:12px">'+(labels[k]||k)+'</span><span><span class="'+statusCls(st)+'" style="font-size:12px">'+cn(st)+'</span>'+(detail?' <span class="dim" style="font-size:11px">'+detail+'</span>':'')+'</span></div>';
  }
  // Resources
  var res = data.resources || {};
  if (res.available) {
    h += '<div class="stat-row" style="padding:4px 6px;margin-top:4px;border-top:1px solid var(--border)"><span style="font-size:11px">CPU / 内存 / 磁盘</span><span class="num dim" style="font-size:11px">'+(res.cpu_pct!=null?res.cpu_pct.toFixed(0)+'%':'?')+' / '+(res.memory_pct!=null?res.memory_pct.toFixed(0)+'%':'?')+' / '+(res.disk_pct!=null?res.disk_pct.toFixed(0)+'%':'?')+'</span></div>';
  }
  var al = data.alert_level || 'OK';
  document.getElementById('hdr-alert-badge').innerHTML = badge(cn(al), statusCls(al));
  document.getElementById('panel-modules').innerHTML = h;
}

function renderPerformance(data) {
  if (!data || !data.brains) { document.getElementById('panel-performance').innerHTML = '<span class="muted">无绩效数据</span>'; return; }
  PERF_DATA = data;
  var brains = data.brains.slice();
  var col = PERF_SORT.col;
  var asc = PERF_SORT.asc;
  var keys = ['brain_id','governance_status','cumulative_pnl','win_rate','sharpe_ratio','profit_factor','max_drawdown','sample_count','health_signal','recommendation','long_win_rate','short_win_rate','recent_pnl_20'];
  brains.sort(function(a, b) {
    var va = a[keys[col]], vb = b[keys[col]];
    if (va == null) va = (typeof vb === 'number') ? -Infinity : '';
    if (vb == null) vb = (typeof va === 'number') ? -Infinity : '';
    if (typeof va === 'number' && typeof vb === 'number') return asc ? va - vb : vb - va;
    va = String(va); vb = String(vb);
    return asc ? va.localeCompare(vb) : vb.localeCompare(va);
  });

  var h = '<table class="dense-table" id="perf-table"><thead><tr>';
  var headers = ['大脑 ID','治理','盈亏','胜率','夏普','盈亏比','最大回撤','样本','健康','建议','做多胜率','做空胜率','近期走势'];
  var hdrCols = ['brain_id','governance_status','cumulative_pnl','win_rate','sharpe_ratio','profit_factor','max_drawdown','sample_count','health_signal','recommendation','long_win_rate','short_win_rate','recent_pnl_20'];
  for (var i = 0; i < headers.length; i++) {
    h += '<th data-col="'+i+'" data-key="'+hdrCols[i]+'">'+headers[i]+sortArrow(i)+'</th>';
  }
  h += '</tr></thead><tbody>';
  for (var r = 0; r < brains.length; r++) {
    var b = brains[r];
    var govSt = b.governance_status || 'unknown';
    var hlCls = statusCls(b.health_signal);
    var rec = b.recommendation || 'observe';
    var recCls = statusCls(rec);
    var rowCls = (SELECTED_BRAIN === b.brain_id) ? 'clickable-row selected' : 'clickable-row';
    var sparklineHtml = (b.recent_pnl_series && b.recent_pnl_series.length) ? drawSparkline(b.recent_pnl_series, 90, 32) : '<span class="dim">--</span>';
    var recLabel = cn(rec);
    h += '<tr class="'+rowCls+'" data-brain="'+(b.brain_id||'')+'" onclick="selectBrain(\''+(b.brain_id||'')+'\')">';
    h += '<td style="font-weight:600">'+(b.brain_id||'?')+'</td>';
    h += '<td>'+badge(cn(govSt), statusCls(govSt))+'</td>';
    h += '<td class="num '+cellColor(b.cumulative_pnl, 0.01, 0)+'">'+fmtNum(b.cumulative_pnl, 4)+'</td>';
    h += '<td class="num '+cellColor(b.win_rate, 0.55, 0.45)+'">'+fmtPct(b.win_rate)+'</td>';
    h += '<td class="num '+cellColor(b.sharpe_ratio, 1.0, 0.0)+'">'+fmtNum(b.sharpe_ratio, 2)+'</td>';
    h += '<td class="num '+cellColor(b.profit_factor, 1.5, 1.0)+'">'+fmtNum(b.profit_factor, 2)+'</td>';
    h += '<td class="num '+cellColor(b.max_drawdown, 5.0, 15.0, false)+'">'+fmtNum(b.max_drawdown, 2)+'</td>';
    h += '<td class="num dim">'+(b.sample_count||0)+'</td>';
    h += '<td>'+badge(cn(b.health_signal||'?'), hlCls)+'</td>';
    h += '<td class="'+recCls+'" style="font-size:11px;font-weight:600">'+recLabel+'</td>';
    h += '<td class="num '+cellColor(b.long_win_rate, 0.55, 0.45)+'">'+fmtPct(b.long_win_rate)+'</td>';
    h += '<td class="num '+cellColor(b.short_win_rate, 0.55, 0.45)+'">'+fmtPct(b.short_win_rate)+'</td>';
    h += '<td>'+sparklineHtml+'</td>';
    h += '</tr>';
  }
  h += '</tbody></table>';
  if (data.errors && data.errors.length) h += '<div class="error-block" style="margin-top:4px">错误: '+data.errors.join(', ')+'</div>';
  document.getElementById('panel-performance').innerHTML = h;
  makeSortable('perf-table');
}

// ── Brain Detail Panel ──

function renderBrainDetail(data) {
  if (!data || data.error) { document.getElementById('panel-brain-detail').innerHTML = '<span class="red">加载失败: '+(data&&data.error||'未知错误')+'</span>'; return; }
  var g = data.governance || {};
  var p = data.pnl || {};
  var pf = data.performance || {};
  var s = data.signals || {};
  var t = data.training_metrics || {};

  var h = '';

  // Key metric boxes
  h += '<div class="brain-detail-grid">';
  h += _metricBox('累计盈亏', fmtPn(p.cumulative_pnl), p.cumulative_pnl>=0?'green':'red');
  h += _metricBox('胜率', fmtPct(p.win_rate), p.win_rate>=0.5?'green':(p.win_rate>=0.4?'yellow':'red'));
  h += _metricBox('夏普比率', fmtNum(p.sharpe_ratio,2), p.sharpe_ratio>=1.0?'green':(p.sharpe_ratio>=0?'yellow':'red'));
  h += _metricBox('盈亏比', fmtNum(p.profit_factor,2), p.profit_factor>=1.5?'green':(p.profit_factor>=1.0?'yellow':'red'));
  h += _metricBox('最大回撤', fmtNum(p.max_drawdown,2)+'%', p.max_drawdown<=5?'green':(p.max_drawdown<=15?'yellow':'red'));
  h += _metricBox('样本数', String(p.sample_count||0), 'muted');
  h += '</div>';

  // PnL sparkline
  h += '<div style="background:rgba(15,23,42,0.5);border-radius:6px;padding:10px 12px;margin-bottom:10px">';
  h += '<h4 style="font-size:11px;color:var(--dim);margin-bottom:6px">近期盈亏走势 (最近20笔)</h4>';
  var pnlSeries = p.recent_pnl_series || [];
  if (pnlSeries.length > 1) {
    h += drawSparkline(pnlSeries, 400, 60);
    h += '<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-top:2px"><span>最早</span><span>最近 →</span></div>';
  } else {
    h += '<span class="dim">数据不足</span>';
  }
  h += '</div>';

  // Direction distribution
  h += '<div style="background:rgba(15,23,42,0.5);border-radius:6px;padding:10px 12px;margin-bottom:10px">';
  h += '<h4 style="font-size:11px;color:var(--dim);margin-bottom:4px">方向分布</h4>';
  var longPct = (p.long_count||0) / Math.max(1, (p.long_count||0)+(p.short_count||0)) * 100;
  var shortPct = 100 - longPct;
  var neutralPct = s.signal_frequency_pct ? (100 - s.signal_frequency_pct) : 0;
  var dirPct = s.signal_frequency_pct || 0;
  var longOfDir = dirPct > 0 ? (longPct / 100 * dirPct) : 0;
  var shortOfDir = dirPct > 0 ? (shortPct / 100 * dirPct) : 0;
  // Simpler: show long count vs short count
  h += '<div style="display:flex;gap:16px;align-items:center;margin-top:4px">';
  h += '<span style="font-size:12px">做多: <b class="green">'+(p.long_count||0)+'</b> (胜率 '+fmtPct(p.long_win_rate)+')</span>';
  h += '<span style="font-size:12px">做空: <b class="red">'+(p.short_count||0)+'</b> (胜率 '+fmtPct(p.short_win_rate)+')</span>';
  h += '<span style="font-size:12px">信号频率: <b>'+(s.signal_frequency_pct||0).toFixed(1)+'%</b></span>';
  h += '</div>';
  // Visual bar
  var totalDir = (p.long_count||0) + (p.short_count||0) || 1;
  h += '<div class="direction-bar" style="margin-top:6px">';
  h += '<div class="bar-seg bar-long" style="width:'+((p.long_count||0)/totalDir*100)+'%"></div>';
  h += '<div class="bar-seg bar-short" style="width:'+((p.short_count||0)/totalDir*100)+'%"></div>';
  h += '</div>';
  h += '</div>';

  // Governance & Performance detail row
  h += '<div class="grid-2" style="margin-bottom:10px">';
  // Governance
  h += '<div style="background:rgba(15,23,42,0.5);border-radius:6px;padding:10px 12px">';
  h += '<h4 style="font-size:11px;color:var(--dim);margin-bottom:4px">治理状态</h4>';
  h += '<div class="gov-item"><span>状态</span><span>'+badge(cn(g.status||'?'), statusCls(g.status))+'</span></div>';
  h += '<div class="gov-item"><span>冻结次数</span><span class="num">'+(g.freeze_count||0)+'</span></div>';
  h += '<div class="gov-item"><span>健康评级</span><span>'+badge(cn(pf.health_signal||'?'), statusCls(pf.health_signal))+'</span></div>';
  h += '<div class="gov-item"><span>综合评分</span><span class="num">'+fmtNum(pf.composite_score,4)+'</span></div>';
  h += '<div class="gov-item"><span>建议操作</span><span class="'+statusCls(pf.recommendation)+'">'+cn(pf.recommendation||'observe')+'</span></div>';
  if (g.last_transition) h += '<div class="gov-item"><span>最近变更</span><span class="dim">'+timeAgo(g.last_transition)+'</span></div>';
  h += '</div>';
  // Performance metrics
  h += '<div style="background:rgba(15,23,42,0.5);border-radius:6px;padding:10px 12px">';
  h += '<h4 style="font-size:11px;color:var(--dim);margin-bottom:4px">绩效指标</h4>';
  h += '<div class="gov-item"><span>平均收益</span><span class="num">'+fmtNum(p.avg_return,6)+'</span></div>';
  h += '<div class="gov-item"><span>最近20笔盈亏</span><span class="num '+ (p.recent_pnl_20>=0?'green':'red')+'">'+fmtPn(p.recent_pnl_20)+'</span></div>';
  h += '<div class="gov-item"><span>总交易笔数</span><span class="num">'+(p.sample_count||0)+'</span></div>';
  h += '<div class="gov-item"><span>做多笔数 / 胜率</span><span class="num">'+(p.long_count||0)+' / '+fmtPct(p.long_win_rate)+'</span></div>';
  h += '<div class="gov-item"><span>做空笔数 / 胜率</span><span class="num">'+(p.short_count||0)+' / '+fmtPct(p.short_win_rate)+'</span></div>';
  if (t.train_sharpe != null) h += '<div class="gov-item"><span>训练夏普</span><span class="num">'+fmtNum(t.train_sharpe,2)+'</span></div>';
  h += '</div>';
  h += '</div>';

  // Training metrics (if available)
  if (t.train_sharpe != null || t.training_date) {
    h += '<div style="background:rgba(15,23,42,0.5);border-radius:6px;padding:10px 12px">';
    h += '<h4 style="font-size:11px;color:var(--dim);margin-bottom:4px">训练指标</h4>';
    h += '<div style="display:flex;gap:24px;flex-wrap:wrap;font-size:12px">';
    if (t.train_sharpe != null) h += '<span>训练夏普: <b>'+fmtNum(t.train_sharpe,2)+'</b></span>';
    if (t.val_sharpe != null) h += '<span>验证夏普: <b>'+fmtNum(t.val_sharpe,2)+'</b></span>';
    if (t.forward_sharpe != null) h += '<span>前向夏普: <b>'+fmtNum(t.forward_sharpe,2)+'</b></span>';
    if (t.feature_count != null) h += '<span>特征数: <b>'+t.feature_count+'</b></span>';
    if (t.training_date) h += '<span>训练日期: <b>'+t.training_date+'</b></span>';
    h += '</div></div>';
  }

  document.getElementById('panel-brain-detail').innerHTML = h;
}

function _metricBox(label, val, cls) {
  return '<div class="metric-box"><h4>'+label+'</h4><div class="metric-val '+cls+'">'+val+'</div></div>';
}

// ── Analytics ──

function renderAnalytics(data) {
  if (!data) { document.getElementById('panel-analytics').innerHTML = '<span class="muted">无分析数据</span>'; return; }
  var h = '';
  var sug = data.param_suggestions || [];
  h += '<div class="gov-section"><h3>参数建议 ('+sug.length+')</h3>';
  if (!sug.length) { h += '<span class="dim" style="font-size:11px">暂无建议</span>'; }
  else {
    for (var i = 0; i < Math.min(sug.length, 5); i++) {
      var s = sug[i];
      h += '<div class="gov-item"><span style="font-size:11px">'+(s.brain_id||s.param||'?')+'</span><span class="dim" style="font-size:11px">'+(s.reason||s.suggestion||'')+'</span></div>';
    }
  }
  h += '</div>';
  var degraded = data.degraded_brains || [];
  h += '<div class="gov-section"><h3>降级模型 ('+degraded.length+')</h3>';
  if (!degraded.length) { h += '<span class="dim" style="font-size:11px">无降级</span>'; }
  else {
    for (var j = 0; j < degraded.length; j++) {
      var d = degraded[j];
      var dhl = d.health_signal === 'critical' ? 'red' : 'yellow';
      h += '<div class="gov-item"><span style="font-size:11px">'+d.brain_id+'</span><span class="'+dhl+'" style="font-size:11px">'+cn(d.health_signal)+'</span><span class="dim" style="font-size:10px">n='+d.sample_count+' 建议='+(cn(d.recommendation)||'?')+'</span></div>';
    }
  }
  h += '</div>';
  var ret = data.retirement_candidates || [];
  h += '<div class="gov-section"><h3>退役候选 ('+ret.length+')</h3>';
  if (!ret.length) { h += '<span class="dim" style="font-size:11px">无</span>'; }
  else {
    for (var k = 0; k < ret.length; k++) {
      var rt = ret[k];
      h += '<div class="gov-item"><span style="font-size:11px">'+rt.brain_id+'</span><span class="red" style="font-size:11px">'+cn(rt.health_signal)+'</span><span class="dim" style="font-size:10px">状态='+(cn(rt.current_status)||'?')+'</span></div>';
    }
  }
  h += '</div>';
  if (data.errors && data.errors.length) h += '<div class="error-block">错误: '+data.errors.join(', ')+'</div>';
  document.getElementById('panel-analytics').innerHTML = h;
}

// ── Governance ──

function renderGovernance(data) {
  if (!data) { document.getElementById('panel-governance').innerHTML = '<span class="muted">无治理数据</span>'; return; }
  var h = '';
  var counts = data.status_counts || {};
  h += '<div class="gov-section"><h3>状态分布</h3><div style="display:flex;gap:12px;font-size:12px;flex-wrap:wrap">';
  var countKeys = Object.keys(counts);
  for (var i = 0; i < countKeys.length; i++) {
    var ck = countKeys[i];
    var cc = counts[ck];
    h += '<span><span class="'+statusCls(ck)+'">'+cn(ck)+'</span>: <span class="num">'+cc+'</span></span>';
  }
  h += '</div></div>';
  var pq = data.promotion_queue || [];
  h += '<div class="gov-section"><h3>晋升队列 ('+pq.length+')</h3>';
  if (!pq.length) h += '<span class="dim" style="font-size:11px">空</span>';
  else for (var p = 0; p < pq.length; p++) {
    var pi = pq[p];
    h += '<div class="gov-item"><span style="font-size:11px">'+pi.brain_id+'</span><span class="green" style="font-size:11px">评分 '+fmtNum(pi.composite_score,3)+'</span><span class="dim" style="font-size:10px">来自 '+cn(pi.from_status)+' n='+pi.sample_count+'</span></div>';
  }
  h += '</div>';
  var dw = data.demotion_warnings || [];
  h += '<div class="gov-section"><h3>降级警告 ('+dw.length+')</h3>';
  if (!dw.length) h += '<span class="dim" style="font-size:11px">无</span>';
  else for (var w = 0; w < dw.length; w++) {
    var di = dw[w];
    var recLabel = cn(di.recommendation);
    h += '<div class="gov-item"><span style="font-size:11px">'+di.brain_id+'</span><span class="red" style="font-size:11px">'+recLabel+'</span><span class="dim" style="font-size:10px">评分='+fmtNum(di.composite_score,3)+'</span></div>';
  }
  h += '</div>';
  var fl = data.freeze_list || [];
  h += '<div class="gov-section"><h3>冻结模型 ('+fl.length+')</h3>';
  if (!fl.length) h += '<span class="dim" style="font-size:11px">无冻结</span>';
  else for (var f = 0; f < fl.length; f++) {
    var fi = fl[f];
    h += '<div class="gov-item"><span style="font-size:11px">'+fi.brain_id+'</span><span class="red" style="font-size:10px">×'+fi.freeze_count+'</span><span class="dim" style="font-size:10px">'+(fi.reason||'')+'</span></div>';
  }
  h += '</div>';
  var tr = data.recent_transitions || [];
  h += '<div class="gov-section"><h3>最近变更</h3><div class="transition-log">';
  if (!tr.length) h += '<span class="dim" style="font-size:11px">无变更</span>';
  else for (var t = 0; t < tr.length; t++) {
    var tx = tr[t];
    var from = tx.from_status || tx.from || '?';
    var to = tx.to_status || tx.to || '?';
    var when = tx.transitioned_at || tx.at || tx.timestamp || '';
    h += '<div class="tl-entry"><span>'+tx.brain_id+'</span><span>'+cn(from)+' → <span class="'+statusCls(to)+'">'+cn(to)+'</span></span><span class="dim">'+timeAgo(when)+'</span></div>';
  }
  h += '</div></div>';
  if (data.errors && data.errors.length) h += '<div class="error-block">错误: '+data.errors.join(', ')+'</div>';
  document.getElementById('panel-governance').innerHTML = h;
}

// ── Positions ──

function renderPositions(data) {
  if (!data.connected) {
    document.getElementById('panel-positions').innerHTML = '<span class="yellow">MT5 未连接</span>';
    return;
  }
  if (!data.positions || data.positions.length === 0) {
    var age = data.generated_at ? (' <span class="dim">(快照 '+fmtTime(data.generated_at)+')</span>') : '';
    document.getElementById('panel-positions').innerHTML = '<span class="muted">无持仓</span>' + age;
    return;
  }
  var src = data._source ? ' <span class="dim" style="font-size:10px">['+data._source+']</span>' : '';
  var h = '<table><tr><th>Ticket</th><th>品种</th><th>方向</th><th>入场价</th><th>止损</th><th>止盈</th><th>盈亏</th></tr>';
  for (var i=0; i<data.positions.length; i++) {
    var p = data.positions[i];
    var sideCls = p.side === 'BUY' ? 'green' : 'red';
    var sideLabel = p.side === 'BUY' ? '做多' : (p.side === 'SELL' ? '做空' : p.side);
    var pnl = p.profit != null ? p.profit : 0;
    var pnlCls = pnl >= 0 ? 'green' : 'red';
    h += '<tr><td class="num">' + (p.ticket||'?') + '</td><td>' + (p.symbol||'?') + '</td><td class="'+sideCls+'">' + (sideLabel||'?') + '</td><td class="num">' + (p.price_open||'?') + '</td><td class="num dim">' + (p.sl||'--') + '</td><td class="num dim">' + (p.tp||'--') + '</td><td class="num '+pnlCls+'">' + fmtPn(pnl) + '</td></tr>';
  }
  h += '</table>';
  if (data.generated_at) h += '<div class="dim" style="margin-top:4px">'+fmtTm(data.generated_at)+src+'</div>';
  document.getElementById('panel-positions').innerHTML = h;
}

// ── Brain Signals ──

function renderBrains(data) {
  var brains = data.brains || [];
  if (!brains.length) { document.getElementById('panel-brains').innerHTML = '<span class="muted">无大脑数据</span>'; return; }
  var h = '<table class="dense-table"><tr><th>大脑 ID</th><th>治理</th><th>健康</th><th>方向</th><th>置信度</th><th>盈亏</th><th>夏普</th><th>样本</th></tr>';
  for (var i=0; i<brains.length; i++) {
    var b = brains[i];
    var st = b.status || 'unknown';
    var hl = b.health || 'unknown';
    var dirCls = b.last_direction === 'LONG' ? 'green' : b.last_direction === 'SHORT' ? 'red' : 'yellow';
    var dirLabel = b.last_direction === 'LONG' ? '做多' : b.last_direction === 'SHORT' ? '做空' : (b.last_direction||'?');
    var conf = b.confidence != null ? (b.confidence*100).toFixed(0)+'%' : '--';
    var pnl = b.pnl_total != null ? b.pnl_total : 0;
    var pnlStr = pnl === 0 && b.pnl_samples == null ? '--' : (pnl>=0?'+':'')+pnl.toFixed(3);
    var pnlCls = pnl > 0 ? 'green' : (pnl < 0 ? 'red' : 'muted');
    var sharpe = b.sharpe_ratio != null ? b.sharpe_ratio.toFixed(2) : '--';
    var n = b.sample_count || b.pnl_samples || 0;
    var rowCls = hl === 'critical' || st === 'frozen' ? 'module-critical' : (hl === 'degraded' ? 'module-warn' : '');
    h += '<tr class="'+rowCls+'"><td style="font-weight:500">'+(b.brain_id||'?')+'</td><td>'+badge(cn(st), statusCls(st))+'</td><td>'+badge(cn(hl), statusCls(hl))+'</td><td class="'+dirCls+'">'+dirLabel+'</td><td class="num">'+conf+'</td><td class="num '+pnlCls+'">'+pnlStr+'</td><td class="num">'+sharpe+'</td><td class="num dim">'+n+'</td></tr>';
  }
  h += '</table>';
  document.getElementById('panel-brains').innerHTML = h;
}

// ── Decisions ──

function renderDecisions(data) {
  var h = '<div class="decision-card">';
  h += _renderDecisionCard('影子组合 (Shadow)', data.shadow);
  h += _renderDecisionCard('实盘调度 (Live)', data.live);
  h += '</div>';
  document.getElementById('panel-decisions').innerHTML = h;
}

function _renderDecisionCard(title, d) {
  if (!d) return '<div><span class="dim">'+title+'</span><br><span class="muted">无数据</span></div>';
  var c = d.consensus || 'no_results';
  var cls = 'consensus-'+c;
  var att = d.brains || {};
  var sup = (att.supporting||[]).join(', ') || '--';
  var opp = (att.opposing||[]).join(', ') || '--';
  var actionLabel = d.decision_action === 'ABSTAIN' ? '观望' : (d.decision_action||'?');
  var sideLabel = d.decision_side === 'FLAT' ? '无方向' : (d.decision_side||'?');
  return '<div><span class="dim">'+title+'</span><br><span class="'+cls+'" style="font-size:16px">' + c.toUpperCase() + '</span><br><span class="muted">'+ actionLabel + ' · ' + sideLabel + '</span><br><span class="dim" style="font-size:11px">'+fmtTm(d.event_time)+'</span><br><span class="dim">一致性: '+((d.agreement_score||d.consensus_score||0)*100).toFixed(1)+'%</span><br><span class="green">支持: '+sup+'</span><br><span class="red">反对: '+opp+'</span></div>';
}

// ── SLO ──

function renderSLO(data) {
  var objs = data.objectives || {};
  var names = Object.keys(objs);
  if (!names.length) { document.getElementById('panel-slo').innerHTML = '<span class="muted">无SLO数据</span>'; return; }
  var labels = { decision_success_rate:'决策成功率', dispatch_success_rate:'调度成功率', reconciliation_match_rate:'对账匹配率', throttle_rate:'节流率', circuit_open_rate:'熔断率' };
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
  h += '<div style="margin-top:4px;font-size:11px">状态: <span class="'+statusCls+'">' + ((data.status||'?').toUpperCase()) + '</span>';
  if (data.failed_objectives && data.failed_objectives.length) {
    h += ' · <span class="red">'+data.failed_objectives.length+' 项超标</span>';
  }
  h += '</div>';
  document.getElementById('panel-slo').innerHTML = h;
}

// ── Risk Gates ──

function renderRisk(data) {
  var policies = data.policies || [];
  var h = '';
  var overall = data.overall || 'PASS';
  var overallCls = statusCls(overall);
  h += '<div class="stat-row"><span>总体</span><span>' + badge(cn(overall), overallCls) + '</span></div>';
  for (var i=0; i<policies.length; i++) {
    var p = policies[i];
    var policyName = {market_calendar:'市场日历', journal_quality:'日志质量', spread:'点差检查', DrawdownPolicy:'回撤策略', PositionLimitPolicy:'仓位限制', ConcentrationPolicy:'集中度策略', ExposurePolicy:'敞口策略', ModePolicy:'模式策略'}[p.name] || p.name;
    h += '<div class="stat-row"><span style="font-size:12px">' + policyName + '</span><span>' + badge(p.passed?cn('PASS'):cn('BLOCK'), p.passed?'green':'red') + '</span></div>';
    if (p.detail) h += '<div class="dim" style="font-size:11px;padding-left:8px">' + p.detail + '</div>';
  }
  if (data.flag_active) h += '<div class="stat-row"><span>调度开关</span><span>' + badge(cn('BLOCKED'),'red') + '</span></div>';
  document.getElementById('panel-risk').innerHTML = h;
}

// ── Alerts ──

function renderAlerts(data) {
  var alerts = data.alerts || [];
  if (!alerts.length) { document.getElementById('panel-alerts').innerHTML = '<span class="muted">无告警</span>'; return; }
  var h = '<table><tr><th>时间</th><th>级别</th><th>规则</th><th>上下文</th></tr>';
  for (var i=0; i<Math.min(alerts.length, 30); i++) {
    var a = alerts[i];
    var sevCls = statusCls(a.severity);
    var ctx = a.context_snapshot || {};
    var ctxStr = Object.keys(ctx).slice(0,3).map(function(k){return k+'='+ctx[k]}).join(' ');
    h += '<tr class="'+(a.severity==='critical'?'alert-critical':(a.severity==='warning'?'alert-warning':''))+'"><td class="dim num">' + fmtTime(a.fired_at) + '</td><td>' + badge(cn(a.severity||'?'), sevCls) + '</td><td style="font-size:12px">' + (a.rule_name||'?') + '</td><td class="dim" style="font-size:11px">' + (ctxStr||'--') + '</td></tr>';
  }
  h += '</table>';
  document.getElementById('panel-alerts').innerHTML = h;
}

// ── Journal ──

function renderJournal(data) {
  var entries = data.entries || [];
  if (!entries.length) { document.getElementById('panel-journal').innerHTML = '<span class="muted">今日无交易</span>'; return; }
  var h = '<table><tr><th>时间</th><th>品种</th><th>操作</th><th>方向</th><th>状态</th><th>止损</th><th>止盈</th></tr>';
  for (var i=0; i<entries.length; i++) {
    var e = entries[i];
    var stCls = {'accepted':'green','rejected':'red','acknowledged':'blue','closed':'dim'}[e.ack_status] || 'muted';
    var stLabel = {'accepted':'已接受','rejected':'已拒绝','acknowledged':'已确认','closed':'已关闭'}[e.ack_status] || e.ack_status;
    var sideCls = e.side === 'long' ? 'green' : e.side === 'short' ? 'red' : '';
    var sideLabel = e.side === 'long' ? '做多' : e.side === 'short' ? '做空' : (e.side||'?');
    var actionLabel = e.action === 'open' ? '开仓' : e.action === 'close' ? '平仓' : (e.action||'?');
    h += '<tr><td class="dim num">' + fmtTime(e.recorded_at) + '</td><td>' + (e.symbol||'?') + '</td><td>' + actionLabel + '</td><td class="'+sideCls+'">' + sideLabel + '</td><td>' + badge(stLabel||'?', stCls) + '</td><td class="num dim">' + (e.sl||'--') + '</td><td class="num dim">' + (e.tp||'--') + '</td></tr>';
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

// ── Unified health render (Phase 3b) ──
function renderUnifiedHealth(data) {
  var statusEl = document.getElementById('health-status');
  if (statusEl) {
    statusEl.textContent = data.overall_status || 'unknown';
    statusEl.className = 'badge ' + ((data.overall_status === 'healthy') ? 'badge-green' : (data.overall_status === 'degraded') ? 'badge-yellow' : 'badge-red');
  }
}

function refreshAll() {
  updateTimestamp();
  // Phase 3b: unified health first — single-request rendering for health panels
  doFetch('/api/health/full', renderUnifiedHealth);
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


# ═══════════════════════════════════════════════════════════════════════════════
# Data helpers
# ═══════════════════════════════════════════════════════════════════════════════


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
    """Collect system resource usage (CPU, memory, disk for data/ volume)."""
    try:
        import psutil

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
    except Exception:  # BLE001:REVIEWED
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
        except Exception:  # BLE001:REVIEWED
            return {"available": False}


# ═══════════════════════════════════════════════════════════════════════════════
# Live MT5 positions fetch (thread-timeout guarded)
# ═══════════════════════════════════════════════════════════════════════════════


def _fetch_live_mt5_positions(
    mt5_terminal_path: str,
    symbol: str = "XAUUSDc",
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Call MT5 positions_get() in a daemon thread with a hard timeout."""
    import threading

    result: list[dict[str, Any] | None] = [None]
    exc_info: list[Exception | None] = [None]

    def _target() -> None:
        try:
            from scripts.mt5_positions_snapshot import build_snapshot

            snap = build_snapshot(mt5_terminal_path=mt5_terminal_path, symbol=symbol)
            result[0] = snap
        except Exception as exc:  # BLE001:REVIEWED
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


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP handler
# ═══════════════════════════════════════════════════════════════════════════════


class LiveDashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the live trading dashboard."""

    BASE_DIR: Path = Path("data")
    MT5_TERMINAL_PATH: str | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ARG002
        """Suppress default access logging to stderr."""
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            # Brain detail endpoint: /api/brain/{brain_id}
            if path.startswith("/api/brain/") and len(path) > len("/api/brain/"):
                brain_id = path[len("/api/brain/") :]
                self._serve_api_brain_detail(brain_id)
            elif path == "/":
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
            elif path == "/api/health/full":
                self._serve_api_health_full()
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
        except Exception:  # BLE001:REVIEWED (Sev 4, Phase 3b)
            logger.exception("Dashboard request failed: %s", self.path)
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
            from scripts.live_dashboard import build_dashboard

            report = build_dashboard(base_dir=str(self.BASE_DIR), date_key=date)
            report.pop("text", None)
            report["generated_at"] = _utc_now_iso()
            self._serve_json(report)
        except Exception as exc:  # BLE001:REVIEWED
            logger.warning("Dashboard collect failed: %s", exc)
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
        if self.MT5_TERMINAL_PATH:
            data = _fetch_live_mt5_positions(
                mt5_terminal_path=self.MT5_TERMINAL_PATH,
                symbol="XAUUSDc",
                timeout=4.0,
            )
            if not data.get("error"):
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
        """Merge tracker health + governance status + PnL + last direction per brain."""
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
            except Exception:  # BLE001:REVIEWED
                logger.warning("Failed to load brain performance tracker", exc_info=True)

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
            except Exception:  # BLE001:REVIEWED
                logger.warning("Failed to load governance state", exc_info=True)

        # 2.5 PnL data
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
            except Exception:  # BLE001:REVIEWED
                logger.warning("Failed to load brain PnL ledger", exc_info=True)

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
        """P0 FIX: Shadow reads from decisions file; Live reads from trade journal."""
        date = _today_key()
        dec_dir = self.BASE_DIR / "decisions" / date

        shadow_path = dec_dir / "XAUUSDc.decisions.jsonl"

        def _parse_decisions(p: Path) -> dict[str, Any] | None:
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

        # Live: read last accepted/acknowledged trade from journal
        def _parse_live_from_journal() -> dict[str, Any] | None:
            journal_path = self.BASE_DIR / "live_trade_journal.jsonl"
            lines = _read_last_n_lines(journal_path, n=50)
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ack_status") in ("accepted", "acknowledged") and rec.get("action") in (
                    "open",
                    "close",
                ):
                    side = rec.get("side", "unknown")
                    return {
                        "event_time": rec.get("recorded_at", ""),
                        "consensus": f"live_{rec.get('action', 'unknown')}",
                        "decision_action": rec.get("action", "?").upper(),
                        "decision_side": side.upper() if side in ("long", "short") else "FLAT",
                        "agreement_score": None,
                        "consensus_score": None,
                        "brains": {
                            "supporting": [],
                            "opposing": [],
                        },
                    }
            return None

        self._serve_json(
            {
                "shadow": _parse_decisions(shadow_path),
                "live": _parse_live_from_journal(),
            }
        )

    def _serve_api_slo(self) -> None:
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from core.observability.slo_service import SloService

            slo = SloService()
            report = slo.evaluate()
            self._serve_json(report)
        except Exception as exc:  # BLE001:REVIEWED
            logger.warning("SLO service failed: %s", exc)
            self._serve_json(
                {
                    "status": "unavailable",
                    "error": str(exc)[:200],
                    "objectives": {},
                    "generated_at": _utc_now_iso(),
                }
            )

    def _serve_api_alerts(self) -> None:
        alerts: list[dict[str, Any]] = []

        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from core.observability.alert_service import (
                AlertService,
            )

            fired = AlertService.with_default_rules().get_fired_history(limit=50)
            alerts.extend(fired)
        except Exception:  # BLE001:REVIEWED
            logger.warning("AlertService failed", exc_info=True)

        try:
            from core.observability.audit_log import (
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
        except Exception:  # BLE001:REVIEWED
            logger.warning("AuditLog failed", exc_info=True)

        alerts.sort(key=lambda a: str(a.get("fired_at", "")), reverse=True)
        self._serve_json({"count": len(alerts[-50:]), "alerts": alerts[-50:]})

    # ── Unified health aggregator (Phase 3a) ──

    @staticmethod
    def _read_json_safe(path: Path) -> dict[str, Any]:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # BLE001:REVIEWED
            pass
        return {}

    @staticmethod
    def _read_jsonl_head(path: Path, n: int = 10) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            records.append(json.loads(line))
                            if len(records) >= n:
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception:  # BLE001:REVIEWED
            pass
        return records

    def _build_unified_health(self) -> dict[str, Any]:
        """Aggregate 7 health data sources into a single unified report."""
        now_ts = time_module.time()
        report: dict[str, Any] = {
            "generated_at": _utc_now_iso(),
            "overall_status": "healthy",
            "bridge": {
                "connected": False,
                "heartbeat_age_s": None,
                "outbox_pending": 0,
                "last_error": None,
            },
            "intent_loop": {
                "iteration": 0,
                "last_cycle_s": None,
                "bar_sync": {},
                "feature_store": {},
                "daily_ops": {},
            },
            "brains": {
                "tracked": 0,
                "active": 0,
                "probation": 0,
                "frozen": 0,
                "schema_mismatches": [],
            },
            "positions": {"open_count": 0, "total_exposure": 0.0},
            "alerts": {"active_count": 0, "last_alert_age_s": None},
            "slo": {"compliance_pct": 100.0, "budget_remaining_pct": 100.0},
        }

        # ── 1. Bridge health ──
        try:
            bridge = self._read_json_safe(self.BASE_DIR / "reports" / "mt5_bridge_health.json")
            if bridge:
                report["bridge"]["connected"] = bridge.get("connected", False)
                hb = bridge.get("last_heartbeat_utc")
                if hb:
                    hb_ts = hb if isinstance(hb, int | float) else time_module.time()
                    report["bridge"]["heartbeat_age_s"] = round(now_ts - hb_ts, 1)
                report["bridge"]["outbox_pending"] = bridge.get("outbox_pending", 0)
                report["bridge"]["last_error"] = bridge.get("last_error")
        except Exception:  # BLE001:REVIEWED
            pass

        # ── 2. Intent loop / bar sync ──
        try:
            bar = self._read_json_safe(self.BASE_DIR / "bar_sync_state.json")
            if bar:
                report["intent_loop"]["iteration"] = bar.get("iteration", 0)
                report["intent_loop"]["bar_sync"] = {
                    "last_bar_time": bar.get("last_bar_time"),
                    "lag_count": bar.get("lag_count", 0),
                }
                report["intent_loop"]["last_cycle_s"] = round(
                    now_ts - bar.get("last_cycle_ts", now_ts), 1
                )
        except Exception:  # BLE001:REVIEWED
            pass

        # ── 3. Feature store ──
        try:
            store_dir = self.BASE_DIR / "feature_store"
            schemas_path = store_dir / "schemas.json"
            feature_schemas = self._read_json_safe(schemas_path)
            report["intent_loop"]["feature_store"] = {
                "schemas_count": len(feature_schemas),
                "schema_version_ok": True,
            }
        except Exception:  # BLE001:REVIEWED
            pass

        # ── 4. Daily ops ──
        try:
            ds = self._read_json_safe(self.BASE_DIR / "state" / "daily_ops_state.json")
            if ds:
                last_ts = ds.get("last_daily_ops_utc", 0)
                report["intent_loop"]["daily_ops"] = {
                    "last_run_utc": last_ts,
                    "hours_ago": round((now_ts - float(last_ts)) / 3600.0, 2) if last_ts else None,
                }
        except Exception:  # BLE001:REVIEWED
            pass

        # ── 5. Brain tracker + governance ──
        try:
            from core.feedback.brain_performance_tracker import BrainPerformanceTracker

            tracker_path = self.BASE_DIR / "brain_performance.json"
            if tracker_path.exists():
                tracker = BrainPerformanceTracker.load(tracker_path)
                summaries = tracker.get_all_summaries()
                report["brains"]["tracked"] = len(summaries)
                for s in summaries:
                    hs = s.get("health_signal", "")
                    if hs in ("healthy", "stable"):
                        report["brains"]["active"] += 1
                    elif hs == "degraded":
                        report["brains"]["probation"] += 1
        except Exception:  # BLE001:REVIEWED
            pass

        try:
            from core.governance.governance_service import GovernanceService

            gov_path = self.BASE_DIR / "governance_state.json"
            if gov_path.exists():
                gov = GovernanceService.load(gov_path)
                states = gov.get_all_states()
                report["brains"]["frozen"] = sum(
                    1 for s in states.values() if s.get("status") == "frozen"
                )
                report["brains"]["probation"] += sum(
                    1 for s in states.values() if s.get("status") == "probation"
                )
        except Exception:  # BLE001:REVIEWED
            pass

        # ── 6. Open positions ──
        try:
            snap_path = self.BASE_DIR / "reports" / "mt5_positions_live_now.json"
            pos_data = self._read_json_safe(snap_path)
            positions = pos_data.get("positions", [])
            if positions:
                report["positions"]["open_count"] = len(positions)
                report["positions"]["total_exposure"] = round(
                    sum(float(p.get("volume", 0)) for p in positions), 2
                )
        except Exception:  # BLE001:REVIEWED
            pass

        # ── 7. Alerts ──
        try:
            alerts_path = self.BASE_DIR / "reports" / "exit_watchdog_alerts.jsonl"
            recent_alerts = self._read_jsonl_head(alerts_path, n=50)
            report["alerts"]["active_count"] = len(recent_alerts)
            if recent_alerts:
                newest_ts_str = recent_alerts[-1].get("fired_at") or recent_alerts[-1].get(
                    "timestamp"
                )
                if newest_ts_str:
                    try:
                        newest_dt = datetime.fromisoformat(
                            str(newest_ts_str).replace("Z", "+00:00")
                        )
                        report["alerts"]["last_alert_age_s"] = round(
                            now_ts - newest_dt.timestamp(), 1
                        )
                    except (ValueError, TypeError):
                        pass
        except Exception:  # BLE001:REVIEWED
            pass

        # ── 8. SLO ──
        try:
            slo_path = self.BASE_DIR / "slo_budget.json"
            if slo_path.exists():
                slo_data = self._read_json_safe(slo_path)
                report["slo"] = {
                    "compliance_pct": slo_data.get("compliance_pct", 100.0),
                    "budget_remaining_pct": slo_data.get("budget_remaining_pct", 100.0),
                }
        except Exception:  # BLE001:REVIEWED
            pass

        # ── Compute overall status ──
        failures: list[str] = []
        if not report["bridge"]["connected"]:
            failures.append("bridge_disconnected")
        if report["brains"]["frozen"] > 0:
            failures.append(f"{report['brains']['frozen']}_brains_frozen")
        bridge_age = report["bridge"]["heartbeat_age_s"]
        if bridge_age is not None and bridge_age > 120:
            failures.append(f"bridge_stale_{bridge_age:.0f}s")

        if len(failures) >= 2:
            report["overall_status"] = "critical"
        elif len(failures) == 1:
            report["overall_status"] = "degraded"
        else:
            report["overall_status"] = "healthy"

        return report

    def _serve_api_health_full(self) -> None:
        global _UNIFIED_HEALTH_CACHE
        now_ts = time_module.time()
        if (
            _UNIFIED_HEALTH_CACHE["data"] is not None
            and (now_ts - _UNIFIED_HEALTH_CACHE["ts"]) < _UNIFIED_HEALTH_CACHE_TTL
        ):
            self._serve_json(_UNIFIED_HEALTH_CACHE["data"])
            return

        data = self._build_unified_health()
        _UNIFIED_HEALTH_CACHE = {"ts": now_ts, "data": data}
        self._serve_json(data)

    def _serve_api_health(self) -> None:
        report: dict[str, Any] = {
            "generated_at": _utc_now_iso(),
            "alert_level": "OK",
            "subsystems": {},
            "resources": _collect_system_resources(),
        }

        try:
            from scripts.live_auto_healthcheck import (
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
        except Exception as exc:  # BLE001:REVIEWED
            logger.warning("Health check failed: %s", exc)
            report["subsystems"]["healthcheck"] = {"status": "ERROR", "detail": str(exc)[:200]}

        try:
            from core.feedback.brain_performance_tracker import (
                BrainPerformanceTracker,
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
        except Exception:  # BLE001:REVIEWED
            logger.warning("Brain health summary failed", exc_info=True)

        try:
            from core.governance.governance_service import (
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
        except Exception:  # BLE001:REVIEWED
            logger.warning("Governance health summary failed", exc_info=True)

        self._serve_json(report)

    def _serve_api_risk(self) -> None:
        policies: list[dict[str, Any]] = []
        overall = "PASS"

        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from scripts.live_dispatch_policy import (
                build_parser as policy_parser,
            )
            from scripts.live_dispatch_policy import (
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
        except Exception:  # BLE001:REVIEWED
            logger.warning("Risk gate policy evaluation failed", exc_info=True)
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

    # ── Performance matrix ──

    def _serve_api_performance(self) -> None:
        """Return per-brain performance matrix with recent PnL time series."""
        global _PERF_CACHE
        pnl_path = self.BASE_DIR / "brain_pnl_ledger.json"
        tracker_path = self.BASE_DIR / "brain_performance.json"
        gov_path = self.BASE_DIR / "governance_state.json"

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

        # 1. PnL ledger
        pnl_table: list[dict[str, Any]] = []
        if pnl_path.exists():
            try:
                from core.feedback.brain_pnl_ledger import BrainPnLStore

                store = BrainPnLStore.load(pnl_path)
                pnl_table = store.get_summary_table()
            except Exception as exc:  # BLE001:REVIEWED
                errors.append(f"pnl_ledger: {exc}")
                logger.warning("PnL ledger load failed: %s", exc)

        # 2. BrainPerformanceTracker
        tracker: dict[str, dict[str, Any]] = {}
        if tracker_path.exists():
            try:
                from core.feedback.brain_performance_tracker import (
                    BrainPerformanceTracker,
                )

                t = BrainPerformanceTracker.load(tracker_path)
                for s in t.get_all_summaries():
                    tracker[s["brain_id"]] = s
            except Exception as exc:  # BLE001:REVIEWED
                errors.append(f"tracker: {exc}")
                logger.warning("Tracker load failed: %s", exc)

        # 3. Governance
        gov_states: dict[str, dict[str, Any]] = {}
        if gov_path.exists():
            try:
                from core.governance.governance_service import GovernanceService

                g = GovernanceService.load(gov_path)
                gov_states = g.get_all_states()
            except Exception as exc:  # BLE001:REVIEWED
                errors.append(f"governance: {exc}")
                logger.warning("Governance load failed: %s", exc)

        # 4. Merge with recent_pnl_series for sparkline
        for pnl in pnl_table:
            bid = pnl.get("brain_id", "")
            entry = dict(pnl)
            entry["governance_status"] = gov_states.get(bid, {}).get("status", "unknown")
            entry["freeze_count"] = gov_states.get(bid, {}).get("freeze_count", 0)
            tinfo = tracker.get(bid, {})
            entry["health_signal"] = tinfo.get("health_signal", "insufficient_data")
            entry["composite_score"] = round(tinfo.get("composite_mean", 0), 4)
            entry["recommendation"] = tinfo.get("recommendation", "observe")
            # Try to get recent PnL series from raw data
            entry["recent_pnl_series"] = _get_recent_pnl_series(
                self.BASE_DIR / "brain_pnl_ledger.json", bid
            )
            brains.append(entry)

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
                        "recent_pnl_series": [],
                    }
                )
                seen.add(bid)

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

    # ── Brain detail endpoint ──

    def _serve_api_brain_detail(self, brain_id: str) -> None:
        """Return detailed metrics for a single brain."""
        pnl_path = self.BASE_DIR / "brain_pnl_ledger.json"
        tracker_path = self.BASE_DIR / "brain_performance.json"
        gov_path = self.BASE_DIR / "governance_state.json"

        result: dict[str, Any] = {
            "brain_id": brain_id,
            "generated_at": _utc_now_iso(),
        }

        # PnL metrics
        if pnl_path.exists():
            try:
                from core.feedback.brain_pnl_ledger import BrainPnLStore

                store = BrainPnLStore.load(pnl_path)
                metrics = store.get_metrics(brain_id)
                if metrics.sample_count > 0:
                    result["pnl"] = metrics.to_dict()
                    # Add recent PnL time series for sparkline
                    result["pnl"]["recent_pnl_series"] = _get_recent_pnl_series(
                        pnl_path, brain_id, n=30
                    )
                    result["pnl"]["long_count"] = getattr(metrics, "long_count", 0)
                    result["pnl"]["short_count"] = getattr(metrics, "short_count", 0)
                else:
                    result["pnl"] = {
                        "cumulative_pnl": 0,
                        "win_rate": 0,
                        "sharpe_ratio": 0,
                        "profit_factor": 0,
                        "max_drawdown": 0,
                        "avg_return": 0,
                        "recent_pnl_20": 0,
                        "long_win_rate": 0,
                        "short_win_rate": 0,
                        "long_count": 0,
                        "short_count": 0,
                        "sample_count": 0,
                        "recent_pnl_series": [],
                    }
            except Exception as exc:  # BLE001:REVIEWED
                logger.warning("Brain PnL detail failed for %s: %s", brain_id, exc)
                result["pnl_error"] = str(exc)[:200]

        # Performance / health
        if tracker_path.exists():
            try:
                from core.feedback.brain_performance_tracker import (
                    BrainPerformanceTracker,
                )

                t = BrainPerformanceTracker.load(tracker_path)
                summary = t.get_brain_summary(brain_id)
                result["performance"] = {
                    "health_signal": summary.get("health_signal", "insufficient_data"),
                    "composite_score": summary.get("composite_mean", 0),
                    "recommendation": summary.get("recommendation", "observe"),
                    "sample_count": summary.get("sample_count", 0),
                    "outcome_distribution": summary.get("outcome_distribution", {}),
                }
            except Exception as exc:  # BLE001:REVIEWED
                logger.warning("Brain performance detail failed for %s: %s", brain_id, exc)

        # Governance
        if gov_path.exists():
            try:
                from core.governance.governance_service import GovernanceService

                g = GovernanceService.load(gov_path)
                state = g.get_all_states().get(brain_id, {})
                result["governance"] = {
                    "status": state.get("status", "unknown"),
                    "freeze_count": state.get("freeze_count", 0),
                    "last_transition": state.get("last_transition_at", ""),
                }
            except Exception as exc:  # BLE001:REVIEWED
                logger.warning("Brain governance detail failed for %s: %s", brain_id, exc)

        # Training metrics from brain config (if available)
        config_path = Path(f"configs/brains/{brain_id}.json")
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                train = cfg.get("training_metrics", cfg.get("training", {}))
                result["training_metrics"] = {
                    "train_sharpe": train.get("train_sharpe") or train.get("sharpe"),
                    "val_sharpe": train.get("val_sharpe") or train.get("validation_sharpe"),
                    "forward_sharpe": train.get("forward_sharpe") or train.get("fw_sharpe"),
                    "feature_count": train.get("feature_count")
                    or cfg.get("feature_count")
                    or cfg.get("input_dim"),
                    "training_date": train.get("training_date") or cfg.get("trained_at", ""),
                }
            except (json.JSONDecodeError, OSError):
                pass

        # Signal stats from tracker
        if tracker_path.exists() and "performance" not in result:
            pass  # Already loaded above

        self._serve_json(result)

    # ── Governance dashboard ──

    def _serve_api_governance(self) -> None:
        tracker_path = self.BASE_DIR / "brain_performance.json"
        gov_path = self.BASE_DIR / "governance_state.json"
        errors: list[str] = []

        status_counts: dict[str, int] = {}
        promotion_queue: list[dict[str, Any]] = []
        demotion_warnings: list[dict[str, Any]] = []
        freeze_list: list[dict[str, Any]] = []
        recent_transitions: list[dict[str, Any]] = []

        gov_states: dict[str, dict[str, Any]] = {}
        if gov_path.exists():
            try:
                from core.governance.governance_service import GovernanceService

                g = GovernanceService.load(gov_path)
                gov_states = g.get_all_states()
                for s in gov_states.values():
                    st = s.get("status", "unknown")
                    status_counts[st] = status_counts.get(st, 0) + 1
                tlog = g.get_transition_log()
                recent_transitions = list(tlog[-20:])
                recent_transitions.reverse()
            except Exception as exc:  # BLE001:REVIEWED
                errors.append(f"governance: {exc}")
                logger.warning("Governance load failed: %s", exc)

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
            except Exception as exc:  # BLE001:REVIEWED
                errors.append(f"tracker: {exc}")
                logger.warning("Tracker load failed for governance: %s", exc)

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

    # ── Analytics recommendations ──

    def _serve_api_analytics(self) -> None:
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
            except Exception:  # BLE001:REVIEWED
                logger.warning("Governance load failed for analytics", exc_info=True)

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
            except Exception as exc:  # BLE001:REVIEWED
                errors.append(f"tracker: {exc}")
                logger.warning("Tracker load failed for analytics: %s", exc)

        self._serve_json(
            {
                "generated_at": _utc_now_iso(),
                "param_suggestions": param_suggestions,
                "retirement_candidates": retirement_candidates,
                "degraded_brains": degraded_brains,
                "errors": errors,
            }
        )

    # ── Module health ──

    def _serve_api_modules(self) -> None:
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
        except Exception as exc:  # BLE001:REVIEWED
            modules["mt5_bridge"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 2. Outbox
        try:
            outbox_dir = self.BASE_DIR / "mt5_outbox"
            pending = 0
            stale = 0
            if outbox_dir.exists():
                cutoff = now - 600
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
        except Exception as exc:  # BLE001:REVIEWED
            modules["outbox"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 3. Feature Store
        try:
            fs_dir = self.BASE_DIR / "feature_store"
            if fs_dir.exists():
                max_mtime = 0.0
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
        except Exception as exc:  # BLE001:REVIEWED
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
            adapter_status = "WARNING" if (configured > 0 and with_data == 0) else "OK"
            modules["brain_adapters"] = {
                "status": adapter_status,
                "configured": configured,
                "with_data": with_data,
                "active": with_data,
            }
        except Exception as exc:  # BLE001:REVIEWED
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
        except Exception as exc:  # BLE001:REVIEWED
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
        except Exception as exc:  # BLE001:REVIEWED
            modules["daily_ops"] = {"status": "ERROR", "detail": str(exc)[:100]}

        # 8. Resources
        resources = _collect_system_resources()

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


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers for per-brain PnL time series
# ═══════════════════════════════════════════════════════════════════════════════


def _get_recent_pnl_series(pnl_path: Path, brain_id: str, n: int = 20) -> list[float]:
    """Extract recent PnL values for a brain from the ledger for sparkline display.

    The ledger stores settled outcomes as a list of dicts per brain_id,
    each dict containing a 'pnl' field with the realized PnL value.
    """
    try:
        data = json.loads(pnl_path.read_text(encoding="utf-8"))
        outcomes = data.get("settled", {}).get(brain_id, [])
        if not isinstance(outcomes, list) or not outcomes:
            return []
        pnl_values = []
        for entry in outcomes:
            if isinstance(entry, dict):
                pnl_values.append(float(entry.get("pnl", 0)))
            elif isinstance(entry, int | float):
                pnl_values.append(float(entry))
        if not pnl_values:
            return []
        recent = pnl_values[-n:]
        cum = 0.0
        result = []
        for pnl in recent:
            cum += pnl
            result.append(round(cum, 6))
        return result
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Server entry point
# ═══════════════════════════════════════════════════════════════════════════════


def run_dashboard_server(
    base_dir: str = "data",
    host: str = "127.0.0.1",
    port: int = 8080,
    mt5_terminal_path: str | None = None,
) -> HTTPServer:
    """Create and return the dashboard HTTP server."""
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
        help="MT5 terminal path for live position fetching",
    )
    args = parser.parse_args(argv)

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
