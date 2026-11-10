# 行情工作台前端

Vue 3、TypeScript、Naive UI 和 Vite，使用 pnpm 11.25.0 的冻结锁文件。构建环境采用 Node.js 24。

```bash
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
```

构建产物位于 `frontend/dist/`，由 Python 服务提供。静态资源地址以 `/ui/` 开头，业务页面继续使用 `/signals/:view`、`/summary`、`/tiers`、`/strategy-review` 和 `/login`。生产环境运行静态产物，开发服务器仅用于开发。

```bash
EMSCAN_DEV_BACKEND=http://127.0.0.1:8080 pnpm --dir frontend dev
```

开发时打开 Vite 地址的 `/login`，代理会把 API、退出和下载请求转发给后端。后端提供 `/api/session` 登录接口并使用 HttpOnly Cookie。所有页面通过 `requestJson` 读取同源接口，登录过期时返回登录页。

主题、内容宽度和表格密度统一定义在 `src/theme.css` 与 `src/App.vue`。公共导航由 `AppShell` 渲染；业务页面只维护页面标题和内容。Inter 拉丁字体随构建打包，中文使用系统字体。
