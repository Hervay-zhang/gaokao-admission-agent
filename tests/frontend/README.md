# 前端测试（Playwright）

无框架 runner：直接用 `node` 执行 `.mjs` 脚本，脚本内部用 Playwright 驱动 Chromium，并起一个只服务 `frontend/` 的极简静态服务器。

## 运行

在**项目根目录**执行：

```bash
"C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2-6/node.exe" \
  tests/frontend/t06-shell.test.mjs
```

若 `playwright` 不在默认工作区路径，用 `PW_WORKSPACE` 指定其 `node_modules` 目录：

```bash
PW_WORKSPACE="/path/to/node_modules" node tests/frontend/t06-shell.test.mjs
```

退出码：`0` 全通过；`1` 存在失败用例；`2` 执行异常（依赖缺失等）。

## 覆盖范围（T06）

| 编号 | 场景 |
| --- | --- |
| J1 | 主要路由（含社区、帖子详情、用户主页）可导航且渲染出内容，全程无控制台错误 |
| J2 | 顶栏 = Logo 印章块 + 7 项导航 + 唯一主 CTA「开始测算 →」；无底部 Tab |
| J3 | 首页 Hero = 状态徽章 + 两行衬线大标题 + 双按钮 |
| J4 | 「我的」页为左统计 + 中菜单 + 右列表三栏（≥1024px 三列） |
| J5 | 表单校验：分数 0–750 整数、首选/再选科目必填；合法提交完成第 1 步并写入本地档案 |
| J6 | 375px 不破版 + 三栏折叠 + 抽屉导航；1024px 横向导航 + 三栏展开 |
| J7 | 全路由零控制台/页面错误（ES Module 加载正确） |

## 说明

- 后端未启动时 `api.js` 会探测 `http://127.0.0.1:8000/health` 并失败，这是**预期降级**，测试已将其排除在「前端错误」之外。
- 字体等外链资源的离线加载失败同样被排除。
- 断言同时覆盖 **DOM 结构**（类名/数量）与**计算样式**（栅格列数、字体族），确保「布局必须与见径一致」这一红线被机器守住。
