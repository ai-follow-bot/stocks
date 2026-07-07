---
name: integrate-website
description: |
  把 /opt/stocks 的能力（chain_agent / skills.deep-analyze / skills.valuation-lens 的参数、环境变量、输出格式）
  集成到 /home/smallsite-vue 网站的产业链任务表单和报告页。
  触发词：「集成到网站」「加到网页」「前端展示」「网页上加」「admin-stocks 加」「任务表单加」
  不触发：纯 /opt/stocks CLI 改动、纯 Python pipeline 改动（不涉及网站前端的）。
---

# 集成到网站 · /opt/stocks ↔ /home/smallsite-vue

## 项目关系

```
/opt/stocks                    /home/smallsite-vue
─────────────                  ───────────────────
Python CLI + LLM pipeline  ←── Vue3 + Express + SQLite
chain_agent/                     backend/src/api/admin-stocks.ts
skills/{deep-analyze,            （任务调度 + spawn 子进程）
        valuation-lens}/
                                 frontend/src/views/admin/StocksTasks.vue
                                 frontend/src/views/admin/StocksReports.vue
```

**调用链**：
网页表单 → `POST /api/admin/stocks/tasks` → `stocks_tasks` SQLite 队列 → 10s tick worker → `spawn` 子进程 `/opt/stocks/.venv/bin/python -m {chain_agent.agent | skills.deep-analyze | skills.valuation-lens}` → 写 markdown 报告到 `STOCKS_OUTPUT_DIR` → `stocks_tasks.output_file` 记录路径 → 网页展示。

## 关键文件清单

| 角色 | 路径 | 关注点 |
|---|---|---|
| 后端任务路由 | `/home/smallsite-vue/backend/src/api/admin-stocks.ts` | `TaskInput` interface、`POST /tasks`、`buildAgentArgs`、`runTask` spawn env、worker tick |
| 后端 DB schema | `/home/smallsite-vue/backend/src/services/db.ts` | `stocks_tasks` 建表（line 334 起）+ 迁移模式（line 357-358） |
| 后端类型 | `/home/smallsite-vue/backend/src/types.ts` | 跨模块共享类型 |
| 前端 API 层 | `/home/smallsite-vue/frontend/src/api/index.ts` | `StocksTask` interface、`stocksApi.createTask` payload（line 408 起） |
| 前端任务页 | `/home/smallsite-vue/frontend/src/views/admin/StocksTasks.vue` | 表单 + 任务列表表格 |
| 前端报告页 | `/home/smallsite-vue/frontend/src/views/admin/StocksReports.vue` | 报告列表 + 内容展示 |
| Python 入口 | `/opt/stocks/chain_agent/agent.py`、`/opt/stocks/skills/{deep-analyze,valuation-lens}/__main__.py` | 接受 CLI 参数 + 环境变量 |
| 站点编码规范 | `/home/smallsite-vue/CLAUDE.md` | TypeScript strict、Element Plus、better-sqlite3 单例 |

## 任务类型与 spawn 模式

`admin-stocks.ts::buildAgentArgs(task)` 按任务类型决定跑哪个 Python 模块：

| task_type | Python 模块 | 典型参数 |
|---|---|---|
| `chain`（默认） | `chain_agent.agent` / `us_chain_agent.agent`（`market='us'`） | `<sector> --days N --top-n N --tavily-results N --llm\|--json --out file` |
| `deep_chain` | `skills.deep-analyze` / `skills.us-deep-analyze` | `--chain <sector> --top-n N --days N --out file.md` |
| `stock` | `skills.deep-analyze` / `skills.us-deep-analyze` | `--stock <input> --days N --out file.md` |
| `valuation` | `skills.valuation-lens`（**仅 A 股，无 US 镜像**；`market='us'` + `valuation` 在 `POST /tasks` 已拦截） | `--chain <sector> --top-n N --days N --out file.md` |

> `market='us'` 时 `buildAgentArgs` 把 chain/deep/stock 三个 task_type 的模块换成 `us_chain_agent` / `skills.us-deep-analyze`；`valuation` 无 US 版，美股请求走不到该分支。

`runTask(task)` 在 `admin-stocks.ts:633-690`：
- `spawn(STOCKS_VENV_PYTHON, ['-m', agentModule, ...args], { cwd: STOCKS_ROOT, env: {...process.env, PYTHONUNBUFFERED:'1', <自定义 env>} })`
- worker 并发 = 1，10s tick
- 输出文件路径写回 `stocks_tasks.output_file` 列

## 环境变量约定

| 变量 | 值 | 用途 |
|---|---|---|
| `STOCKS_VENV_PYTHON` | `/opt/stocks/.venv/bin/python` | **必须用此 venv**，系统 python 的 tavily-python 0.1.9 会 ImportError |
| `STOCKS_ROOT` | `/opt/stocks` | spawn 的 cwd |
| `STOCKS_OUTPUT_DIR` | 报告输出目录 | markdown 报告落盘位置 |
| `SERENITY_LENS` | `1` / `0` | 控制 Serenity 四框架视角注入（默认 `1` 开） |
| `KIMI_API_KEY` / `ANTHROPIC_API_KEY` | API key | LLM 综合报告需要 |

## 添加新功能的标准模式

以「在表单加一个开关 X，影响 spawn 行为」为例：

### 路径 A：不持久化（开关只影响本次 spawn，不进任务历史）

1. **后端 API** (`admin-stocks.ts`)
   - `TaskInput` interface 加 `x?: boolean`
   - 在 `admin-stocks.ts` 顶部加模块级 `const pendingX = new Map<number, boolean>();`
   - `POST /tasks` handler 在 `res.json(...)` **之后**调用 `pendingX.set(Number(result.lastInsertRowid), input.x !== false)`
   - `runTask(task)` spawn 前取走并删除：
     ```ts
     const x = pendingX.get(task.id) ?? true;
     pendingX.delete(task.id);
     const child = spawn(STOCKS_VENV_PYTHON, [...], {
       cwd: STOCKS_ROOT,
       env: { ...process.env, PYTHONUNBUFFERED: '1', X_ENV: x ? '1' : '0' },
       detached: false,
     });
     ```
2. **Python 端**：在 `chain_agent/llm/prompts.py` 或 `skills/deep-analyze/prompts.py` 用 `os.environ.get('X_ENV', '1')` 控制 prompt 注入（参考 `_serenity_lens()` 实现）
3. **前端类型** (`api/index.ts`)：`createTask` payload 加 `x?: boolean`（`StocksTask` interface 不动）
4. **前端表单** (`StocksTasks.vue`)：
   - form state 加 `x: true`
   - 加 `<el-form-item label="X"><el-switch v-model="form.x" /><span class="hint">说明</span></el-form-item>`（放在两个 `<template>` 之外、入队按钮之前，对所有任务类型生效）
   - `resetForm()` 加 `x: true`
   - `submitTask` 已用 `{ ...form.value }` 展开，payload 自动包含
5. **构建部署**：
   ```bash
   cd /home/smallsite-vue/backend && npm run build && pm2 restart followbot-backend
   cd /home/smallsite-vue/frontend && npm run build
   ```

### 路径 B：持久化（开关进任务历史，可回看）

在路径 A 基础上额外加：
- **DB 迁移** (`db.ts` line 357-358 附近)：
  ```ts
  try { db.exec('ALTER TABLE stocks_tasks ADD COLUMN x INTEGER NOT NULL DEFAULT 1'); } catch {}
  ```
- `POST /tasks` 的 `INSERT` 语句加 `x` 列（`Boolean(input.x) ? 1 : 0`）
- `StocksTask` interface 加 `x: boolean`
- 前端任务列表表格可加 `<el-tag v-if="row.x">X</el-tag>` 显示

### 路径 C：加 CLI 参数（而非 env 变量）

如果新功能是 Python 端的 CLI 参数（如 `--new-flag`）：
- 后端：`buildAgentArgs(task)` 里 `args.push('--new-flag')`
- 不需要 env 变量
- 持久化与否同上

## 认证说明

- `admin_session` cookie 有 `Secure` flag，curl 在 HTTP localhost 上不会自动回送
- 验证 API 时用 `-D -` 抓 `Set-Cookie`，手动 `-H "Cookie: admin_session=$TOKEN"` 注入
- 测试账号：`follow` / `Followbot4321@`

## 已集成的开关清单

- `use_llm`（持久化）：chain 模式启用 LLM 综合报告
- `tavily_results`（持久化）：Tavily 搜索结果数
- `days` / `top_n` / `batch_sectors`（持久化）：通用参数
- `serenity_lens`（**不**持久化）：Serenity 四框架视角注入，参考实现见 `admin-stocks.ts::pendingSerenityLens` + `chain_agent/llm/prompts.py::_serenity_lens()`

## 编码规范要点（摘自 /home/smallsite-vue/CLAUDE.md）

- 前后端 TypeScript strict 模式，禁止 `any` 绕过类型（axios 封装层 `data?: any` 是例外）
- 前端组件 `<script setup lang="ts">` + Element Plus，`<style scoped>`
- 后端每个功能一个 router 文件，`verifyAuth` 中间件保护 `/admin/*` 路由
- DB 用 better-sqlite3 单例，参数化查询，禁止字符串拼接 SQL
- 错误处理：try-catch 包裹，catch 中 `console.error` + `res.status(500).json({ error: '中文描述' })`

## 验证新功能的标准流程

1. 后端构建：`cd /home/smallsite-vue/backend && npm run build`（tsc 必须无错）
2. 前端构建：`cd /home/smallsite-vue/frontend && npm run build`
3. 重启后端：`pm2 restart followbot-backend`
4. 登录取 token：
   ```bash
   TOKEN=$(curl -s -X POST http://localhost:2000/api/auth/login \
     -H "Content-Type: application/json" \
     -d '{"username":"follow","password":"Followbot4321@"}' \
     -D - -o /dev/null | grep -i "set-cookie: admin_session" | sed 's/.*admin_session=//;s/;.*//')
   ```
5. 提交任务：`curl -H "Cookie: admin_session=$TOKEN" -X POST http://localhost:2000/api/admin/stocks/tasks -H "Content-Type: application/json" -d '{...}'`
6. 看 spawn 日志：`pm2 logs followbot-backend --lines 5 --nostream | grep admin-stocks`，确认新 env / 新参数已传入
7. 任务结束后看报告：`ls /opt/stocks/output/*.md`，grep 验证新功能效果
