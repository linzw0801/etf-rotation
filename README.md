# ETF 轮动选股器 — 云端部署指南

> 将你的 Scriptable ETF 选股器部署到 GitHub Actions，每天收盘后自动运行并通过 QQ 邮箱推送结果。

## 功能

- ✅ **免费部署** — 使用 GitHub Actions 免费额度
- ✅ **定时运行** — 每个交易日 15:30 (A股收盘后) 自动执行
- ✅ **邮件推送** — 通过 QQ 邮箱 SMTP 发送每日选股报告
- ✅ **与原版一致** — 完全复刻你的 Scriptable 方案 v5 算法

## 快速开始

### 1. 创建 GitHub 仓库

1. 登录 [github.com](https://github.com)，点右上角 **+** → **New repository**
2. 仓库名随意（如 `etf-rotation`），选 **Public** 或 **Private**
3. 创建完成后，按下面步骤上传代码

### 2. 上传代码

**方法一：命令行**
```bash
# 在本地初始化仓库
cd 本文件夹所在目录
git init
git add .
git commit -m "Initial commit: ETF rotation cloud版"
git branch -M main
git remote add origin https://github.com/你的用户名/etf-rotation.git
git push -u origin main
```

**方法二：GitHub 网页上传**
1. 在仓库页面点 **Add file** → **Upload files**
2. 把以下文件拖入：
   - `etf_rotation_cloud.py`
   - `.github/workflows/etf_daily.yml`
3. 提交

### 3. 配置 QQ 邮箱授权码

> ⚠ 这是最重要的一步！QQ 邮箱不能用登录密码发信，必须获取 **授权码**。

**获取 QQ 邮箱授权码：**
1. 登录 QQ 邮箱 → 点 **设置** (齿轮图标)
2. 点 **账户** 选项卡
3. 找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务**
4. 点击 **生成授权码**
5. 按短信指引操作，会得到一串 **16位授权码**（形如 `xxxxxxxxxxxxxxxx`）
6. **复制保存好这个授权码**

### 4. 设置 GitHub Secrets (加密配置)

在仓库页面操作：
1. 点 **Settings** → **Secrets and variables** → **Actions**
2. 点 **New repository secret**，添加以下三个密钥：

| 密钥名 | 值 |
|--------|------|
| `EMAIL_FROM` | 你的 QQ 邮箱地址，如 `123456789@qq.com` |
| `EMAIL_PASSWORD` | 上一步获取的 **16位授权码** |
| `EMAIL_TO` | 接收报告的邮箱地址（可以和发件箱相同） |

### 5. 启用 Actions

1. 点仓库顶部 **Actions** 标签
2. 如果看到 "Workflows" 列表中有 `ETF轮动每日选股`
3. 点 **Enable workflow** 启用

### 6. 手动测试运行

1. 点 **Actions** → **ETF轮动每日选股**
2. 点右侧 **Run workflow** → 绿色按钮
3. 等一两分钟，运行成功后会收到邮件！

### 7. 验证定时任务

- 定时表达式: `30 7 * * 1-5` = 北京时间 **周一至周五 15:30**
- 首次自动运行会在下个交易日触发
- 可以在 GitHub 仓库 **Actions** 标签页看到每次运行的日志

## 结果示例

你每天会收到一封类似这样的邮件：

```
📊 ETF轮动选股报告  06-28 15:30
=============================================
  🥇 创业板 ETF　 0.2288
  🥈 沪深300 ETF 0.0063
  🥉 纳指 ETF　　 -0.0026
      黄金 ETF　　 -0.5801
---------------------------------------------
  vol20: 33.8%  正常
  仓位: 满仓 创业板 ETF
---------------------------------------------
  明日 09:30 开盘执行
  若降仓: 14:50 前买 GC001 / R-001
=============================================
```

## 自定义

### 修改 ETF 池

编辑 `etf_rotation_cloud.py` 中的 `ETF_LIST`:

```python
ETF_LIST = [
    {"code": "510300", "name": "沪深300 ETF", "market": "sh"},
    {"code": "159915", "name": "创业板 ETF",  "market": "sz"},
    # 添加更多...
]
```

### 修改运行时间

编辑 `.github/workflows/etf_daily.yml` 中的 cron 表达式:
- `30 7 * * 1-5` = 周一至五 07:30 UTC = 15:30 北京时间
- 改成 `0 7 * * 1-5` = 15:00 北京时间
- 使用 [crontab.guru](https://crontab.guru/) 生成

### 切换其他邮箱

修改 `etf_rotation_cloud.py` 中的 `send_email()` 函数:
- QQ: `smtp.qq.com:465` (SSL)
- 163: `smtp.163.com:465`
- Gmail: `smtp.gmail.com:587` (TLS)

## 注意事项

- GitHub Actions 免费计划每月 **2000 分钟**，每天运行约 1 分钟，绰绰有余
- 如果某天遇到 API 数据为空，脚本会自动跳过，不会影响后续运行
- 选股结果仅供参考，投资有风险，入市需谨慎

## 技术说明

- 从 **新浪财经 API** 获取日K线数据
- 纯 Python 实现，**零外部依赖** (只用标准库)
- 算法完全复刻原版 Scriptable：线性回归动量 + vol20 波动率风控
- 邮件通过 QQ 邮箱 SMTP/SSL 发送，支持 HTML 格式
