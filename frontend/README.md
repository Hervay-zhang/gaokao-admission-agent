# 前端（frontend）

原生 **HTML / CSS / JavaScript (ES Modules)** 实现，**零框架、零构建**，移动优先（< 1024px 为手机/平板），≥ 1024px 展开桌面三栏布局。

> 布局严格对齐参考站点「见径 · 升学人生决策平台」：**导航 6 项 + 主 CTA、Hero 双行衬线大标题、三栏（统计 / 菜单 / 列表）**。不自行发明底部 Tab，不改导航结构。

## 目录结构

```
frontend/
├── index.html                 # 唯一 HTML 入口（挂载点：contour-bg / navbar-root / app / floating-ai-root / disclaimer-root）
├── css/
│   ├── tokens.css             # 设计令牌（色彩 / 字号 / 间距 / 阴影 / 圆角）
│   └── app.css                # 组件与布局样式（含响应式）
├── js/
│   ├── main.js                # 启动引导：背景 → 导航 → 免责声明 → 快捷链接 → AI 悬浮入口 → 路由 → 后端探测
│   ├── config.js              # 静态配置（省份 / 科类 / 选科池 / 标签 / 数据年份 / 模式）
│   ├── state.js               # 全局状态（发布订阅 + localStorage 持久化，键 gaokao_state_v2）
│   ├── auth.js                # 前端演示登录态与考生/家长/管理员角色
│   ├── ui.js                  # 渲染工具：h() / clear() / escapeHtml() / formatNumber() / toast()
│   ├── api.js                 # 后端接口封装（业务 API + admin + SSE）
│   ├── router.js              # Hash 路由（懒加载，18 条路由）
│   ├── caseFavorites.js       # 案例本地收藏（localStorage）
│   ├── mock.js                # T06 阶段的演示数据（真实数据由后端 T08+ 提供）
│   ├── components/
│   │   ├── ContourBg.js       # 等高线纸感背景
│   │   ├── NavBar.js          # 顶栏（Logo 印章块 + 7 项导航 + 主 CTA「开始测算 →」）
│   │   ├── Hero.js            # 首页 Hero（状态徽章 + 两行衬线大标题 + 双按钮）
│   │   ├── DisclaimerBar.js   # 常驻免责声明条
│   │   ├── FloatingAI.js      # 「✦ AI 助手」悬浮入口（与「预约专家」相互独立）
│   │   ├── QuickLinks.js      # 底部快捷链接与官方平台入口
│   │   ├── PageShell.js       # 统一页面外壳（眉标 / 标题 / 副标题 / 内容区）
│   │   ├── ThreeCol.js        # 三栏骨架（左统计 + 中菜单 + 右列表）
│   │   ├── CardList.js        # 卡片列表（标题 / 元信息 / 标签 / ›）
│   │   ├── Skeleton.js        # 骨架屏（加载态）
│   │   └── ScoreInput.js      # 分数 + 选科输入（前端校验）
│   └── pages/                 # 页面模块（每个默认导出 async function(mount)）
│       ├── home.js            # #/           首页
│       ├── assessment.js      # #/assessment 全景评估画像
│       ├── login.js           # #/login      统一登录入口
│       ├── report.js          # #/report     冲稳保路径报告
│       ├── schools.js         # #/schools    院校与专业库
│       ├── schoolDetail.js    # #/schools/:id 院校详情
│       ├── schoolCompare.js   # #/schools/compare 院校对比
│       ├── chat.js            # #/chat       AI 问答
│       ├── cases.js           # #/cases      案例中心
│       ├── caseDetail.js      # #/cases/:id  案例详情
│       ├── expert.js          # #/expert     预约专家（付费）
│       ├── plan.js            # #/plan       志愿表
│       ├── me.js              # #/me         我的（三栏）
│       ├── community.js       # #/community  社区列表
│       ├── postDetail.js      # #/community/:id 帖子详情
│       ├── userProfile.js     # #/user/:id   用户主页
│       ├── shareView.js       # #/share/:id  只读分享
│       └── admin.js           # #/admin      管理后台
```

## 路由表

| 路径 | 页面 | 说明 |
| --- | --- | --- |
| `#/` | 首页 | Hero + 快捷试问 |
| `#/login` | 登录 | 考生 / 家长 / 管理员演示账号 |
| `#/assessment` | 全景评估画像 | 分数 / 选科表单（第 1 步） |
| `#/report` | 路径报告 | 冲稳保三档（第 2 步） |
| `#/schools` | 院校与专业库 | 检索与筛选 |
| `#/schools/:id` | 院校详情 | 专业组与历年投档 |
| `#/schools/compare` | 院校对比 | 最多 3 所 |
| `#/chat` | AI 问答 | 非一级导航，从悬浮入口进入 |
| `#/cases` | 案例中心 | 真实填报案例 |
| `#/cases/:id` | 案例详情 | 脱敏决策复盘 |
| `#/expert` | 预约专家 | 独立付费真人服务 |
| `#/plan` | 志愿表 | 志愿编排（第 3 步） |
| `#/me` | 我的 | 三栏（统计 / 菜单 / 列表） |
| `#/community` | 社区 | 匿名交流与分享 |
| `#/community/:id` | 帖子详情 | 点赞、收藏与评论 |
| `#/user/:id` | 用户主页 | 发帖、收藏与评论记录 |
| `#/share/:id` | 只读分享 | 志愿表快照 |
| `#/admin` | 管理后台 | 数据与配置管理 |

## 演示账号

登录页内置 3 个本地演示账号，用于区分角色界面：

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| `student` | `student123` | 考生 |
| `parent` | `parent123` | 家长 |
| `admin` | `admin123` | 管理员 |

> 该登录态仅用于前端原型演示；`MOCK_MODE=true` 时管理员演示账号可直接查看后台，
> 生产模式仍需 `X-Admin-Token` 鉴权。

## 本地运行（离线，无需后端）

ES Modules 受同源策略限制，**必须通过 HTTP 服务打开**，直接双击 `index.html`（`file://`）会因模块加载失败而白屏。

### 方式一：npm run dev（推荐，热重载）

```bash
cd frontend
npm install        # 首次安装 vite
npm run dev        # 默认 http://localhost:5173
```

Vite 提供热模块重载（HMR），开发时修改 JS/CSS 无需手动刷新。

### 方式二：Python 内置（零依赖）

```bash
cd frontend
python -m http.server 5500
```

### 方式三：npx serve

```bash
cd frontend
npx --yes serve -l 5500 .
```

后端未启动时页面仍可正常浏览——`api.js` 会探测失败并静默降级，各业务页使用 `mock.js` 演示数据。

## 设计令牌

集中在 `css/tokens.css`，禁止在页面里硬编码颜色/字号：

| 令牌 | 值 | 用途 |
| --- | --- | --- |
| `--color-paper` | `#FAF7F2` | 纸感底色 |
| `--color-ink` | `#1E3A5F` | 墨蓝主色（标题 / 主按钮） |
| `--color-seal` | `#C0392B` | 朱砂红（印章 / 强调） |
| `--color-tier-chong` | `#C0392B` | 冲档 |
| `--color-tier-wen` | `#B98A2F` | 稳档 |
| `--color-tier-bao` | `#2E7D5B` | 保档 |
| `--font-serif` | Noto Serif SC | 衬线标题 |
| `--container` | `1200px` | 版心宽度 |

## 约定

- **状态**：只通过 `state.js` 的 `getState / setState / updateProfile / subscribe` 读写，禁止直接操作 localStorage。
- **渲染**：一律使用 `ui.js` 的 `h()` 构造元素，禁止 `innerHTML` 拼接用户数据（XSS）。
- **页面模块**：默认导出 `async function(mount)`，内部用 `mount.replaceChildren(...)` 挂载；需要订阅状态的页面必须在离开时 `unsubscribe`。
- **T06 阶段边界**：不写业务逻辑、不调后端业务接口、不引框架/CDN；数据走 `mock.js`；免责声明常驻可见。

## 测试

前端自动化测试（Playwright，J1–J7）位于 `tests/frontend/`，运行方式见该目录 README。
