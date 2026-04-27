# Quant OS Long-Term Roadmap

This project goal is to evolve into a governed, auditable, self-improving Quant Operating System.
The long-term north star is the user's "stars and sea" vision:方案 C and beyond, but always under
strong governance, risk limits, evidence, rollback, and auditability.

## Core Principle

The system should not become merely a smarter trading script. It should become a controlled operating system:

```text
trusted data -> pluggable strategies -> controlled execution -> independent risk -> auditable allocation ->
governed strategy generation -> certified release/strategy lifecycle -> multi-agent Quant OS
```

Strategies must never directly place orders.

```text
Strategy -> Signal -> Intent -> Risk -> Governance -> Execution Gateway -> Execution Events -> Reconciliation
```

## Current Foundation: A0 Governed Kernel

Already implemented directionally:

- Release readiness
- Runbooks
- SLO / error budget
- Release gate
- Evidence bundle
- Deployment plan
- Dry-run deployment executor
- Rollback drill
- Operations timeline
- Postmortem report
- Release certificate
- Release registry
- Compliance audit
- Compliance control matrix

This foundation exists so future automation can remain safe, explainable, reversible, and auditable.

## Target Roadmap

### A0: Governed Single-Kernel Foundation

Goal: make the system controllable before making it autonomous.

Key capabilities:

- Readiness gates
- Runbooks
- Evidence bundle
- Deployment dry-run
- Rollback drill
- Certificates and registry
- Compliance reports
- Audit control matrix

Next optional A0 hardening:

- Config policy engine
- Secrets management
- Operator approval workflow
- Incident severity router
- Backup / restore drill
- Disaster recovery plan

### A1: Data and Execution Infrastructure

Goal: move from governed kernel to production-grade research/execution infrastructure.

Priority order:

1. Strategy Plugin Protocol
2. Feature Store MVP, initially Parquet/DuckDB-friendly
3. Paper Execution Gateway
4. Order State Machine and Fill Simulator
5. Execution Quality Analytics
6. Broker/FIX Gateway Adapter
7. 24/5 runtime hardening

Recommended architecture:

```text
Feature Store
-> Strategy Plugin
-> Signal
-> Intent
-> Risk
-> Governance
-> Execution Gateway
   -> Paper Gateway
   -> Simulated Gateway
   -> FIX Gateway
-> Execution Events
-> Reconciliation
-> Slippage / Fill Quality Reports
```

### B0: Alpha Factory

Goal: make many strategies manageable, measurable, and lifecycle-controlled.

Required capabilities:

- Alpha registry
- Alpha lifecycle states: candidate, backtest_passed, paper_trading, probation_live, active, throttled, retired
- Alpha performance store
- Alpha health score
- Correlation graph
- Promotion gate
- Retirement policy

### B1: Internal Capital Market

Goal: dynamically allocate capital across alphas under strict risk budgets.

Flow:

```text
Alpha Performance -> Allocation Proposal -> Risk Budget -> Governance Approval -> Capital Assignment
```

LLM/RL may propose and explain allocations, but must not authorize capital by itself.

Hard constraints:

- max weight per alpha
- max weight per cluster
- max daily weight change
- max drawdown cutoff
- min observation days
- cooldown after loss
- kill-switch thresholds

### C0: Governed Strategy Self-Generation

Goal: allow machine-generated candidate strategies without allowing arbitrary unsafe code into live trading.

Required capabilities:

- Strategy DSL / template library
- Code sandbox
- Data leakage detector
- Backtest farm
- Walk-forward validation
- Paper promotion gate
- Live promotion gate
- Strategy certificate
- Strategy registry

The system may generate candidates, but cannot bypass sandbox, risk, governance, or certification.

### C1: Multi-Agent Quant OS

Goal: multi-market, multi-horizon, multi-agent self-improving Quant OS.

Constitution layer must be non-bypassable:

- max daily loss
- max monthly drawdown
- max leverage
- max symbol exposure
- max strategy weight
- max correlation-cluster weight
- max order rate
- max slippage
- allowed markets
- allowed order types
- manual approval requirements
- emergency shutdown conditions

## Immediate Next Engineering Direction

Proceed into A1:

1. Strategy Plugin Protocol
2. Feature Store MVP
3. Paper Execution Gateway
4. Execution Quality Analytics
5. FIX Gateway Adapter

## Non-Negotiable Design Rules

1. Strategy never directly places orders.
2. Execution layer is independent and adapter-based.
3. Risk is a hard constraint, not advice.
4. Automated promotion must go through gates.
5. Critical actions must produce evidence.
6. Machine-generated strategies must run in sandbox first.
7. Every autonomous action must be auditable, reversible, and bounded.
