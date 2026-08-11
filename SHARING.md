# 免费小范围分享部署

本项目使用本机Python服务、浏览器基础认证和Cloudflare Quick Tunnel。服务只监听
`127.0.0.1`，公网访问由Cloudflare提供临时HTTPS地址；重启隧道后地址可能变化。

## 1. 设置分享密码

推荐使用交互式脚本，输入内容不会显示在屏幕上：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\set_share_credentials.ps1
```

也可以手动在项目根目录现有的 `.env` 中加入：

在项目根目录现有的 `.env` 中加入：

```dotenv
SHARE_USER=viewer
SHARE_PASSWORD=请替换为至少12位的随机密码
```

不要把真实密码写进源码、脚本、文档或日志。修改 `.env` 后需要重启分享服务。

## 2. 安装 cloudflared

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_cloudflared.ps1
```

安装后重新打开终端，并确认：

```powershell
.\.runtime\cloudflared.exe --version
```

## 3. 手动测试

终端一：

```powershell
python app.py share --no-browser
```

终端二：

```powershell
.\.runtime\cloudflared.exe tunnel --url http://127.0.0.1:8000
```

将输出的 `https://...trycloudflare.com` 发给访问者。浏览器会要求输入 `.env` 中的用户名和密码。

也可以使用项目脚本，它会持续重连并把当前地址保存到 `logs/share-url.txt`：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_share.ps1
```

## 4. 每日自动更新与开机启动

安装两个当前用户计划任务：登录后启动分享服务，每天18:30更新数据。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_tasks.ps1
```

手动刷新：

```powershell
python app.py refresh --latest-only
```

日志：

- `logs/share-url.txt`：当前分享网址。
- `logs/cloudflared.log`：隧道日志。
- `logs/share-server-error.log`：服务启动错误。
- `logs/refresh.log`：每日邮箱下载、整理和构建结果。

停止服务可在任务计划程序中结束并禁用“FOF Valuation Share”；停止自动更新则禁用
“FOF Valuation Daily Refresh”。

## 安全说明

- 分享页包含财务数据，建议使用至少12位随机密码并只发给必要人员。
- Quick Tunnel网址不可作为认证手段，真正的访问控制来自页面密码。
- 浏览器基础认证没有网页内“退出”按钮；关闭全部浏览器窗口，或清除该站点认证缓存后退出。
- 不要将本机8000端口开放到防火墙，也不要把服务绑定到 `0.0.0.0`。
