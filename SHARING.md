# 免费固定地址分享

本项目默认使用本机Python服务、浏览器基础认证和Tailscale Funnel。Python只监听
`127.0.0.1`，公网通过固定的 `https://电脑名.tailnet名.ts.net` 地址访问。电脑关机、
休眠、断网或Python服务停止时，固定域名仍存在，但网页无法访问。

GitHub Pages不用于本项目：普通Pages是静态托管，无法运行月报下载等Python接口，
也不应公开承载财务网页。Cloudflare Quick Tunnel继续作为临时应急方案，其网址会变化。

浏览器不会直接连接数据库。`build/refresh`在本机读取邮件、估值表和行情缓存，生成轻量HTML及只读JSON；浏览器先取首屏数据，进入市场研究、多因子、底层资产等页面时再取相应模块。所有接口继续要求同一Basic Auth，只允许代码内固定模块名，并使用gzip和ETag减少重复传输。

## 1. 设置网页密码

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\set_share_credentials.ps1
```

凭据只写入Git忽略的`.env`。密码至少12位，不要把真实密码写进源码、文档或日志。

## 2. 首次配置Tailscale固定地址

在项目目录运行以下命令；脚本会请求管理员权限，并在尚未安装时通过winget安装Tailscale：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_tailscale_share.ps1
```

首次运行需要在浏览器完成Tailscale登录和Funnel授权。脚本随后启动密码保护的Python网页，
启用持久后台Funnel，并把固定地址写入：

```powershell
Get-Content .\logs\share-url.txt
```

访问固定地址时仍需输入`.env`中的`SHARE_USER`和`SHARE_PASSWORD`。

## 3. 手工启动和故障恢复

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_tailscale_share.ps1
```

脚本会检查Tailscale登录、MagicDNS、网页凭据、8000端口、本机网页和Funnel；已有网页服务
时不会重复启动。常用检查：

```powershell
Get-Content .\logs\share-url.txt
Get-Content .\logs\tailscale-share.log -Tail 30
tailscale status
tailscale funnel status
```

如果提示未登录，打开Tailscale客户端完成登录；首次Funnel授权失败时，在管理员PowerShell
重新运行首次配置脚本。

## 4. 开机分享与每日18:30刷新

首次配置成功后安装或覆盖两个当前用户计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_tasks.ps1
```

- `FOF Valuation Share`：登录Windows后恢复本机Python网页和Tailscale Funnel。
- `FOF Valuation Daily Refresh`：每天18:30更新数据；错过后在下次开机登录时补跑。

安装脚本会申请管理员授权，并将分享任务注册为“使用最高权限运行”，因为部分Windows安装的Tailscale本地管道仅允许管理员访问。任务计划程序中的最近结果若为`1`，请重新运行上述安装命令，并查看`logs/tailscale-share.log`记录的失败行号和异常类型；日志不会写异常正文、授权地址或凭据。

每日刷新不会主动重算风控日报全资产VaR。手工刷新仍使用：

```powershell
python app.py refresh --latest-only
```

## 5. Cloudflare临时备用

Tailscale不可用时，可临时运行原Cloudflare Quick Tunnel：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_share.ps1
Get-Content .\logs\share-url.txt
```

备用地址为随机 `*.trycloudflare.com`，隧道重启后可能变化。首次使用前如缺少cloudflared：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_cloudflared.ps1
```

## 日志与安全边界

- `logs/share-url.txt`：当前固定地址或最近一次临时地址。
- `logs/tailscale-share.log`：Tailscale分享启动状态。
- `logs/share-server-error.log`：本机Python服务错误。
- `logs/cloudflared.log`：Cloudflare备用隧道日志。
- `logs/refresh.log`：每日刷新步骤、耗时和退出码。

以上运行文件均由Git忽略，不得写入密码、令牌或登录链接。不要开放本机8000端口，
也不要把Python服务绑定到`0.0.0.0`。停止公开分享可运行：

```powershell
tailscale funnel off
```
