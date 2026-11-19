# EconoScout · 上线部署指南（GitHub 自动更新版）

这套方案让 EconoScout 变成一个**每天自动抓取真实经济新闻、自动生成导读、自动发布**的在线产品。

## 它是怎么运作的

```
GitHub Actions（每天北京时间 08:00 自动运行）
   ↓ 抓取 14 个公开 RSS 源（Google/Bing News + 36氪 + 爱范儿…）
   ↓ DeepSeek 大模型按你的「岗位说明书 V1.1」生成 8 字段导读
   ↓ 结构校验通过 → 写入 data/briefs.json → 自动提交回仓库
GitHub Pages 把仓库变成公开网站，访客浏览器读到最新简报
```

- 模型 API Key 只存在 GitHub 仓库的 **Secrets** 里，前端代码看不到 → **F12 搜 Key 依然是 0 处**
- 全免费：Actions 每月 2000 分钟额度，每天跑一次完全够；Pages 免费托管
- 出报失败时自动保留上一期，不会推错误内容

---

## 第一步：拿一个 DeepSeek API Key（约 3 分钟）

1. 打开 <https://platform.deepseek.com/> ，手机号/邮箱注册并登录
2. 左侧菜单点 **「API Keys」** → **「创建 API Key」**
3. 复制那串 `sk-` 开头的字符，先粘到记事本备用（**只显示一次**）
4. 充值：左下角 **「充值」**，充 10 元足够用很久（一天一期导读的成本约几分钱）

> 如果你已有其他 OpenAI 兼容模型的 Key（如通义、Kimi、智谱），也可以用，后面填 `DEEPSEEK_BASE_URL` 换成对应地址即可。

---

## 第二步：建 GitHub 仓库（约 3 分钟）

1. 打开 <https://github.com/new> 登录
2. 填写：
   - **Repository name**：`econoscout`（或任意英文名）
   - 选 **Public**（必须公开，否则 Pages 免费版不能发布）
   - **不要**勾选 "Add a README" 等任何初始化选项，保持空仓库
3. 点 **Create repository**
4. 创建后页面会显示一段 `git remote add origin ...` 的命令，记下里面的仓库地址，形如：  
   `https://github.com/你的用户名/econoscout.git`

---

## 第三步：把项目代码推上去

在电脑上打开终端（PowerShell 或 Git Bash），依次执行。把下面地址换成你自己的：

```bash
cd "C:/Users/hp/WorkBuddy/econoscout/gitops"
git init
git add .
git commit -m "EconoScout 初次上线"
git branch -M main
git remote add origin https://github.com/你的用户名/econoscout.git
git push -u origin main
```

> 如果 `git push` 弹出登录窗口，用浏览器登录 GitHub 完成授权即可。  
> 若要求输入密码，需用 **Personal Access Token** 而非账号密码：  
> GitHub 右上角头像 → Settings → Developer settings → Personal access tokens → Tokens(classic) → 勾选 `repo` 权限生成，把它当密码粘贴。



---

## 第四步：配置模型 Key（Secrets）

1. 进入你的仓库页面 → 顶部 **Settings** 标签
2. 左侧 **Secrets and variables** → **Actions** → **New repository secret**
3. 添加这一条（其他两条可选）：

| Name                | Value                         | 说明                |
| ------------------- | ----------------------------- | ----------------- |
| `DEEPSEEK_API_KEY`  | `sk-你复制的那串`                   | **必填**，第一步拿到的 Key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | 可留空，默认就是这个        |
| `DEEPSEEK_MODEL`    | `deepseek-chat`               | 可留空，默认就是这个        |

点 **Add secret** 保存。

---

## 第五步：开启 GitHub Pages（发布网站）

1. 仓库页面顶部 **Settings** → 左侧 **Pages**
2. **Build and deployment** 里：
   - **Source** 选 **Deploy from a branch**
   - **Branch** 选 `main`，目录选 `/ (root)`
3. 点 **Save**
4. 等 1-2 分钟，刷新页面顶部会出现一个绿色提示：**"Your site is live at <https://你的用户名.github.io/econoscout/>"**
5. 这个链接就是你的**演示链接**，可以交作业、发同学试用

> 首次部署后，进入仓库的 **Actions** 标签，点左侧 **「每日经济简报更新」** → 右上角 **Run workflow** → **Run workflow**，手动跑一次，验证能正常生成简报并自动提交。

---

## 日常维护（几乎为零）

- **自动更新**：每天北京时间 08:00 自动跑，无需干预
- **查看运行状态**：仓库 **Actions** 标签能看到每次运行的日志，绿勾表示成功
- **改新闻源**：编辑 `scripts/fetch_news.py` 里的 `SOURCES` 列表，提交即可
- **改解读规则**：编辑 `scripts/generate_brief.py` 里的 `SYSTEM_PROMPT`，这就是你的岗位说明书数字化版本
- **注意**：GitHub 会在仓库静置 60 天后暂停定时任务，期间只要你 push 过任何改动（或手动跑一次）就会恢复

---

## 本地测试（不联网也能验证流程）

```bash
cd gitops
python scripts/fetch_news.py --verbose      # 测试抓取
python scripts/generate_brief.py --dry-run  # 用种子数据测试生成合并流程
python -m http.server 8791                  # 本地预览，浏览器打开 http://localhost:8791
```

真要本地生成一期（需先设好环境变量）：

```bash
# Windows PowerShell
$env:DEEPSEEK_API_KEY="sk-你的key"
python scripts/generate_brief.py --verbose
```

---

## 常见问题

**Q：Actions 跑失败了怎么办？**  
点进那次运行看日志。最常见是 Key 没填或填错（Secrets 第三步），或某天所有 RSS 源都超时（脚本会保留上一期简报，不会出错报，第二天通常自愈）。

**Q：为什么 F12 搜 Key 是 0 处？**  
Key 只存在 GitHub 服务器端 Secrets，生成时通过环境变量注入 Python 脚本，从不进入 `index.html`。前端只有展示逻辑。

**Q：和之前 workbuddy.host 那个链接是什么关系？**  
是两个独立部署。workbuddy.host 那版用的是平台免密钥问答；这版是 GitHub 全自动更新版，二者前端页面几乎一样，数据都来自同一套 8 字段结构。作业提交用哪个都可以，GitHub 版更能体现"真正在运营"。

**Q：中文新闻源在本地抓不到？**  
本地网络对 Google/Bing 有限制，但 GitHub Actions 在海外服务器运行，这些源正常可用。本地测试时用 36氪/爱范儿源验证解析逻辑即可。
