# DQAF Terminal Closure Lock (全生命周期终态锁)

> **读取指令**: 当本次会话中存在 DQAF 诊断 → 修复 → commit 的完整链路时，
> Agent **必须在 commit 完成后立即执行**: `Read d:\future\references\dqaf_closing_protocol.md`
> 严禁在未完成三步归档协议之前向人类汇报"完成"。

---

## 触发条件

仅当本次会话中存在 DQAF docket 且对应的修复已通过 `verify.py --quick` 并 commit 后触发。
非诊断性编辑（typo、格式、配置值修改）不受此锁约束。

## 强制三步归档协议

### Step 1 — 强制输出账本回填断言块

```
[DQAF_CLOSING_PROTOCOL]
- Target Docket: DQAF-YYYYMMDD-NNN
- Registry Updated: [ ] DQAF_DOCKET_REGISTRY.md (Status -> CLOSED)
- CCT Embedded:   [ ] CCT_LEDGER.md (Layer 1-3 causal chain registered)
- Pattern Indexed: [ ] ReB_PATTERN_INDEX.md (Pattern Signature cataloged)
[AWAITING_ARCHIVE_SEALS]
```

### Step 2 — 执行文件改写

自动依次打开以下三个 Markdown 文件，完成实质性的回填：
1. `blueprints/system/DQAF_DOCKET_REGISTRY.md` — 更新 Docket 状态为 CLOSED
2. `blueprints/system/CCT_LEDGER.md` — 写入 CCT 因果链条目
3. `blueprints/system/ReB_PATTERN_INDEX.md` — 登记 ReB 模式签名

### Step 3 — 最终收口

仅当三个文件全部改写成功后，输出终态标识符：

```
[DQAF_CLOSED] 案卷关闭。代码与蓝图资产已完美沉淀。
```
