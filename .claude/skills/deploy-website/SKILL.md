---
name: deploy-website
description: |
  重启/部署 /home/smallsite-vue 网站（前端 Vue + 后端 Express + nginx）。
  改了 TS/Vue 代码后规范上线：构建 -> 重启 -> 验证。含 nginx pid 文件坑修复。
  触发词：「重启网站」「部署」「deploy」「上线」「生效」「前端改了要重启」「后端改了要重启」「pm2 restart」「nginx reload」。
  不触发：纯 /opt/stocks Python 改动（不需要重启网站，Python 进程每次新起）。
---

# 网站重启/部署

## 什么时候要重启

| 改了什么 | 要重启吗 | 怎么重启 |
|---|---|---|
| **前端 Vue/TS**（StocksTasks.vue / StocksReports.vue / AdminLayout.vue / api/index.ts 等） | ✅ 要 | `npm run build` + `deploy.sh --quick` |
| **后端 TS**（admin-stocks.ts / db.ts / server.ts 等） | ✅ 要 | `npm run build`（tsc）+ `deploy.sh --quick`（pm2 restart + nginx reload） |
| **前后端都改** | ✅ 要 | `deploy.sh`（full：前后端都 build + 重启） |
| **纯 /opt/stocks Python**（skills/chain_agent 等） | ❌ 不要 | 网站每次 spawn 新 Python 进程，自动用最新代码 |
| **/opt/stocks 数据 JSON**（sector_ecosystem/keywords 等） | ❌ 不要 | Python 进程每次读文件，但前端有 mtime 缓存可能需 reload |

## 标准流程

### 1. 构建

```bash
# 仅前端改了
cd /home/smallsite-vue/frontend && npm run build

# 仅后端改了
cd /home/smallsite-vue/backend && npm run build  # tsc

# 前后端都改了（或不确定）
cd /home/smallsite-vue && bash deploy.sh  # full: 前后端都 build + 重启
```

### 2. 重启（nginx pid 坑修复）

**先检查 pid 文件**（deploy.sh 的 reload_nginx 依赖它）：

```bash
# 检查 /tmp/aa_nginx.pid 是否有有效 pid
PID=$(cat /tmp/aa_nginx.pid 2>/dev/null)
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  # pid 缺失或无效 -> 补真实 master pid
  echo $(pgrep -f "nginx: master process /usr/sbin/aa_nginx") > /tmp/aa_nginx.pid
fi
```

**再跑 deploy.sh**：

```bash
cd /home/smallsite-vue && bash deploy.sh --quick
# --quick: 跳过 build，仅 stop+restart 后端(PM2) + reload nginx
# 不加 --quick: full（前后端 build + 重启）
```

### 3. 验证

```bash
# 后端 health
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:2000/health  # 应 200

# 前端 serve 的 JS hash 是否变了（确认新 dist 生效）
curl -sk https://followbot.cn/ | grep -oE "assets/index-[A-Za-z0-9_-]+\.js" | head -1

# 前端 build 产出的 hash
ls /home/smallsite-vue/frontend/dist/assets/index-*.js | tail -1 | xargs basename
# 两者应一致
```

### 4. 浏览器硬刷新

用户侧需 `Ctrl+Shift+R`（硬刷新）清浏览器缓存。index.html 是 `no-cache`，但浏览器可能缓存旧 JS chunk。

## deploy.sh 三种模式

| 命令 | 做什么 | 何时用 |
|---|---|---|
| `bash deploy.sh` | full：stop + 前端 build + 后端 build + start + nginx | 前后端都改了 |
| `bash deploy.sh --quick` | 跳过 build：stop + restart 后端 + reload nginx | 已 build 过，只需重启 |
| `bash deploy.sh --build` | 仅 build（不重启） | 先 build，稍后手动重启 |

## nginx pid 坑（重要）

`deploy.sh` 的 `reload_nginx` 判断 `if [ -f /tmp/aa_nginx.pid ] && kill -0 "$(cat /tmp/aa_nginx.pid)"`：
- pid 文件**缺失或为空**（如某次失败启动留下空壳）-> 条件为假 -> 走"启动新实例"分支 -> 和已在跑的 master 抢 80/443 端口失败（`bind() Address already in use`）。
- **修复**：`echo $(pgrep -f "nginx: master process /usr/sbin/aa_nginx") > /tmp/aa_nginx.pid` 补真实 pid，再跑 deploy.sh。

详见 memory: `deploy-sh-nginx-pid-gotcha.md`。

## 不需要重启的情况

- **Python 改动**（/opt/stocks 的 skills/chain_agent）：网站每次 `spawn` 新 Python 进程，自动用最新代码。**不需要重启网站**。
- **数据 JSON 改动**（sector_ecosystem.json 等）：Python 进程每次读文件。但前端 TS 层有 mtime 失效缓存，可能需要 `pm2 restart followbot-backend` 让 Node 进程重新读 JSON。
- **output 目录报告**：nginx 直接 serve 静态文件，报告一写完就可在 `/admin/stocks-reports` 看到，不需重启。

## 关键文件

| 文件 | 作用 |
|---|---|
| `/home/smallsite-vue/deploy.sh` | 部署脚本（full/--quick/--build） |
| `/home/smallsite-vue/ecosystem.config.cjs` | PM2 配置（followbot-backend） |
| `/etc/aa_nginx/aa_nginx.conf` | nginx 配置（root /home/smallsite-vue/frontend/dist） |
| `/tmp/aa_nginx.pid` | nginx master pid（deploy.sh reload 依赖） |
| `/home/smallsite-vue/stop-prod.sh` | 停止服务 |

## Do / Don't

- ✅ 改了前端/后端 TS 后，先 `npm run build` 再 `deploy.sh --quick`。
- ✅ deploy.sh --quick 前检查 `/tmp/aa_nginx.pid` 有效（缺失就补）。
- ✅ 验证后端 /health = 200 + 前端 JS hash 一致。
- ✅ 告诉用户硬刷新（Ctrl+Shift+R）。
- ❌ 别只 `pm2 restart` 不 `nginx reload`（前端 dist 变了 nginx 不 reload 不生效）。
- ❌ 别只 `nginx -s reload` 不 `pm2 restart`（后端 TS 变了不 restart 不生效）。
- ❌ 别在 Python 改动后重启网站（没必要，Python 每次新起进程）。
- ❌ 别忽略 deploy.sh 的 `bind() failed` 报错（是 pid 文件坑，按上面修复）。
