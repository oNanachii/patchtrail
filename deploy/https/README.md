# 独立HTTPS预览部署

目标入口为`https://math.madokanun.top:8444`，只反代既有PatchTrail回环后端`127.0.0.1:3110`。这是独立预览，不改原443、80、8443监听、原数学站Caddy配置或进程，也不改变DNS。

模板已准备，但模板本身不等于服务器部署或公网验收通过。最终执行、端口放行和切版由主线程在前端独立QA通过后进行。

## 固定代理版本与校验

2026-10-28读取Caddy官方最新稳定release为`v2.11.7`（发布于2026-10-03）。`release.json`记录官方Linux amd64归档、校验清单地址和固定摘要。**官方清单使用SHA512，不是SHA256。**

`install_caddy.py`下载官方清单，要求该条目同时匹配本仓库已核对的固定值，再下载并计算归档SHA512。只取归档中的`caddy`普通文件，以原子且不覆盖的方式写入明确指定的新路径。它不安装或启动系统服务，也不会覆盖现有2.6.2二进制。校验清单验证不等同于签名验证；本流程没有声称已验证官方`.sig/.pem`。

在目标Linux amd64主机上，操作者可运行：

```sh
sudo python3 deploy/https/install_caddy.py --destination /opt/patchtrail-proxy/bin/caddy-2.11.7
/opt/patchtrail-proxy/bin/caddy-2.11.7 version
```

保留helper打印的归档SHA512、二进制SHA256和版本作为部署证据。不要将旧`/srv/math-practice/bin/caddy`替换成这个文件。

## 配置与账户

继续使用已存在、已能读取原受管证书的`math-proxy`账户。原证书及其配对key权限保持原样；本代理环境文件只放证书路径，不放DeepSeek密钥或管理员令牌。独立配置、状态及运行目录分别为`/etc/patchtrail-proxy`、`/var/lib/patchtrail-proxy`及`/run/patchtrail-proxy`。

公开证书当前SAN仅覆盖`math.madokanun.top`，到期为2027-01-20 14:34:57 UTC；它不覆盖不存在DNS记录的`patchtrail.madokanun.top`。这个端口方案不需要新证书或改DNS。

安装前先保存旧服务PID/启动时间、监听口及必要的网络基线。仅安装本目录的独立unit：

```sh
sudo install -d -m0750 -o root -g math-proxy /etc/patchtrail-proxy
sudo install -m0640 -o root -g math-proxy deploy/https/Caddyfile /etc/patchtrail-proxy/Caddyfile
sudo install -m0640 -o root -g math-proxy deploy/https/proxy.env.example /etc/patchtrail-proxy/proxy.env
sudo install -m0644 deploy/https/patchtrail-proxy.service /etc/systemd/system/patchtrail-proxy.service
sudo install -m0644 deploy/https/patchtrail-proxy-cert-reload.service /etc/systemd/system/patchtrail-proxy-cert-reload.service
sudo install -m0644 deploy/https/patchtrail-proxy-cert-reload.path /etc/systemd/system/patchtrail-proxy-cert-reload.path
```

`Caddyfile`保持`admin off`、`auto_https off`、`persist_config off`，只配置8444的HTTP/1.1与HTTP/2，不开HTTP/3 UDP监听。请求体上限4096字节，与后端控制API一致。反代默认保留后端CSP、Referrer-Policy、nosniff及API的no-store响应；添加noindex预览标记，不记录访问日志。

新入口明确不发送HSTS，也不发送`max-age=0`清除策略；它不试图改变同一域名其他端口或浏览器已有的HSTS政策。管理员Bearer令牌只经HTTPS转发到回环后端。

## 必须执行的验证与启动

先由systemd运行新unit的`ExecStartPre`验证（会以既有代理账户私下加载证书与key，不输出其内容），失败时不发布。可以先显式测试适配语法，再启动：

```sh
sudo systemctl daemon-reload
sudo systemctl start patchtrail-proxy.service
sudo systemctl is-active patchtrail-proxy.service
curl --fail --silent --show-error --resolve math.madokanun.top:8444:127.0.0.1 https://math.madokanun.top:8444/api/health
```

不要使用`curl -k`替代证书验收。确认实际新进程只监听8444、旧进程及端口不变，并确认新proxy可以读取证书文件；不要因权限问题放宽原私钥权限。

本机TLS通过后，主线程再单独核对并添加**仅新端口**的UFW允许规则，例如`sudo ufw allow 8444/tcp comment 'PatchTrail HTTPS preview'`。不要重置UFW、刷新全量规则或改原443/8443。然后从机器外部通过正常DNS验证页面、JS/CSS/font、API、CSP/noindex响应头、匿名live拒绝和脚本demo到实际OCI结果。云厂商网络策略也可能影响外部连通，只有外部请求成功才能称公网可访问。

验证通过后再启用开机启动及证书监视：

```sh
sudo systemctl enable patchtrail-proxy.service
sudo systemctl enable --now patchtrail-proxy-cert-reload.path
```

## 证书更新后只reload新代理

该证书仍由原数学代理的既有续期流程管理。新代理加载同一文件，因此配置了两个具体文件的`PathChanged`监视。触发的oneshot先检查新proxy正在运行，短暂合并相邻更新事件，再调用`systemctl reload patchtrail-proxy.service`；不启动已停用的proxy，不调用或重启旧数学站服务。

新proxy的reload先验证自己的配置与证书，然后仅给自己的`$MAINPID`发`SIGUSR1`。Caddy官方从v2.11支持此方式：必须以`caddy run --config`启动，不能使用`--resume`，也不能经admin API改变配置。强制同源reload会重新读取证书文件，即使配置文本未变。模板关闭admin API并保持同一路径，满足此使用条件。[官方Signals说明](https://caddyserver.com/docs/command-line#signals)、[官方运行说明](https://caddyserver.com/docs/running)

部署时应手动启动一次`patchtrail-proxy-cert-reload.service`验证信号路径，检查新proxy PID保持、TLS继续可访问及日志无reload错误。实际未来ACME续期没有在本步骤被强制触发，不能把手动reload测试当成真实续期事件验收。原证书续期、文件损坏或reload失败仍需查看相关服务日志。

## 配置更新与回退

保留新proxy上一个已验证Caddyfile。配置通过验证后原子替换到同一位置，再`systemctl reload patchtrail-proxy.service`，不使用admin API或`caddy reload`改变加载方式。

回退仅停止或恢复这组新unit：

```sh
sudo systemctl disable --now patchtrail-proxy-cert-reload.path
sudo systemctl disable --now patchtrail-proxy.service
```

若8444允许规则确为本次新增，操作者可删除这一条精确规则；预先存在的规则不动。无需删除证书、旧Caddy、数据库、模型配置或应用release来回退HTTPS入口。恢复新proxy时重新验证配置并只启用对应unit。
