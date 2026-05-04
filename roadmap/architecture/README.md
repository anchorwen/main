# 架构总图 — System Architecture Atlas

> **最后更新**: 2026-05-02T07:05:00Z (UTC)  
> **维护者**: Team + Agent  
> **语言**: 中文 + English (bilingual where necessary for international readability)

---

## 这是什么？

本目录是 **Quant OS 的唯一架构真相源（Single Source of Truth for System Architecture）**。

任何人/任何机器想要理解当前系统：
- 有哪些模块？
- 它们怎么连在一起的？
- 哪些已经能用、哪些还是空壳？
- 数据从哪来、经过谁、最后到哪去？

**只需要看这个目录下的 5 个文件。**

---

## 5 文件导航

| 文件 | 回答了什么问题 | 受众 |
|------|----------------|------|
| [SYSTEM_TOPOLOGY.md](SYSTEM_TOPOLOGY.md) | 系统整体长什么样？层级、子系统、连接器 | 所有人（含 AI） |
| [MODULE_INVENTORY.md](MODULE_INVENTORY.md) | 每个模块现在到底能不能用？完成度精确到文件 | 开发者、AI |
| [DATA_FLOW.md](DATA_FLOW.md) | 一笔决策请求从产生到执行的完整旅程 | 开发者、运维 |
| [DEPENDENCY_GRAPH.md](DEPENDENCY_GRAPH.md) | A 依赖 B，改了 A 会影响谁？ | 开发者、重构 |
| [INTERFACE_CONTRACTS.md](INTERFACE_CONTRACTS.md) | 关键模块间的接口约定（输入/输出/错误处理） | 集成测试、AI |

**图表源文件**在 [diagrams/](diagrams/) 子目录，可被 Mermaid Live Editor 直接导入。

---

## 更新规则（强制）

### 什么时候必须更新？

1. **新增或删除 `core/` 下的模块文件** → 更新 MODULE_INVENTORY.md
2. **改动模块间的调用关系** → 更新 SYSTEM_TOPOLOGY.md + DIAGRAMS/
3. **新增/修改接口契约** → 更新 INTERFACE_CONTRACTS.md
4. **Deployment/Config 层改动** → 更新 DATA_FLOW.md

### 谁来更新？

- **人类**：在 PR/MR 中附带架构文档的 diff
- **AI/Agent**：在完成架构级修改后，自动追加 CHANGELOG 并更新对应文件

### 自动化辅助

```bash
# 从代码中自动扫描模块依赖，更新 DEPENDENCY_GRAPH.md 草稿
python roadmap/scripts/update_roadmap.py --scan-deps

# 自动生成 MODULE_INVENTORY.md 骨架（完成度需人工标注）
python roadmap/scripts/update_roadmap.py --scan-modules
```

---

## 格式约束

1. **所有图表使用 Mermaid**（纯文本，可 git diff，GitHub/GitLab/VS Code 原生渲染）
2. **所有表格使用 Markdown 表格**（人类可直接阅读）
3. **模块状态必须使用固定标签**：`✅ 完整` / `⚠️ 部分` / `🧪 存根` / `❌ 缺失`
4. **所有文件使用 UTF-8 编码，LF 换行**

---

## 与 roadmap.json 的关系

`roadmap.json` 是**计划的机器可读版本**。
`architecture/` 是**当前实际的机器+人类双读版本**。

两者互补：
- `roadmap.json` → 回答"我们要去哪"
- `architecture/` → 回答"我们现在在哪"

自动化脚本 `update_roadmap.py` 会同时更新两者。