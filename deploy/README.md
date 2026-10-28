# PatchTrail独立部署

控制面以专用账户运行，检查任务全部进入无网络容器。容器不可用时服务应报告不可用，不能转为宿主执行。这里提供部署模板；模板写入不代表服务器已经安装或部署。

## 参考环境

部署模板针对Debian13、Python3.13、cgroup v2与rootless Podman验证。先检查目标机器资源、权限、用户namespace和现有服务，再准备独立账户、目录与端口。实际机器、运行基线和密钥保存在操作者私有记录中，不放入公开源码。

首版控制面默认监听回环3110；启动前核对端口，保留现有应用和网络配置。Podman及Python虚拟环境组件通过发行版软件包安装，先查看模拟计划，不自动清理或升级无关组件。

## 隔离运行选择

采用专用`patchtrail`账户的rootless Podman，应用通过CLI启动容器，无需启用Podman API socket。rootless Podman可用普通用户运行，且与Docker CLI有兼容接口；本项目显式使用`--network=none`，不创建供业务访问的容器网络。[Podman官方说明](https://docs.podman.io/en/latest/markdown/podman.1.html)

这比在现有代理服务器上新增rootful Docker daemon更容易控制影响范围。Docker默认会改变网络与转发规则；官方文档也说明`iptables=false`并非完全禁止改规则的保证，并可能破坏容器联网。[Docker防火墙说明](https://docs.docker.com/engine/network/packet-filtering-firewalls/)

不启用`podman.socket`，不暴露容器控制API。该API可执行运行账户权限下的任意操作，官方明确要求限制其访问。[Podman服务说明](https://docs.podman.io/en/latest/markdown/podman-system-service.1.html)

## 部署前准备

实际执行由操作者完成。先记录现有三个服务的状态、启动时间、配置哈希，以及防火墙规则和监听端口；部署后与同一基线比较。不要把其中的私钥或凭据写进仓库或公开报告。

查看安装计划后，使用发行版软件包安装`podman uidmap fuse-overlayfs`。`dbus-user-session`在当前候选机上已有；其他环境需另外确认。不要安装或启动rootful Docker，也不要以网络配置变更解决`--network=none`检查的失败。

新建独立服务账户时可采用以下命令。若同名账户已存在，先核查，不改变既有UID、home或subuid范围。

```sh
sudo useradd --create-home --home-dir /var/lib/patchtrail --shell /usr/sbin/nologin patchtrail
sudo chmod 0700 /var/lib/patchtrail
sudo install -d -m 0700 -o patchtrail -g patchtrail /var/lib/patchtrail/.config /var/lib/patchtrail/.config/systemd /var/lib/patchtrail/.config/systemd/user /var/lib/patchtrail/.config/patchtrail /var/lib/patchtrail/data
sudo loginctl enable-linger patchtrail
```

中间目录也必须属于专用账户。仅对深层目标执行`install -d -o patchtrail`可能留下root持有的`.config`父目录，Podman会拒绝初始化；不要通过放宽为全局可写来解决。

确认该账户在`/etc/subuid`和`/etc/subgid`各有至少65536个不重叠的从属ID，且`newuidmap`、`newgidmap`可执行。不要复制或覆盖其他账户范围。用户级服务需要cgroup v2与资源控制委派，容器预检必须实际验证资源限制生效。

将审核后的应用发布到独立的`/opt/patchtrail/releases/<release-id>`，在Linux上建立虚拟环境并按`requirements.lock.txt`安装依赖。不要复制macOS的`.venv`。源码release由管理员管理，应用数据存入`/var/lib/patchtrail/data`；密钥文件放在`/var/lib/patchtrail/.config/patchtrail/patchtrail.env`，由服务账户持有、权限0600。home和数据父目录保持0700。

把`patchtrail.env.example`复制到该外部配置位置后由操作者填写DeepSeek新密钥及独立随机管理员令牌。不要将密钥放进命令行、Git、前端配置、任务fixture、运行trace或检查容器。配置示例中的模型应与当次确认的DeepSeek官方接口匹配。

## 镜像与实际容器预检

仅在部署准备阶段，以服务账户拉取审核后的Python镜像；记录仓库digest，并把完整`docker.io/library/python@sha256:...`写入`PATCHTRAIL_CONTAINER_IMAGE`。不要只依赖可变tag。应用每次启动检查容器都必须使用`--pull=never`；缺镜像时失败，不能由公开请求触发拉取。

在专用账户的正常用户systemd会话中运行：

```sh
podman info --format json
podman pull docker.io/library/python:3.12-slim
podman image inspect --format '{{index .RepoDigests 0}}' docker.io/library/python:3.12-slim
python3 /opt/patchtrail/current/deploy/container_preflight.py --image '填写已记录的完整镜像digest'
```

`container_preflight.py`不安装或拉取镜像，只创建一个临时探针容器，检查非root、无外部网络接口、只读根和工作区、capabilities清空、no-new-privileges、seccomp，以及CPU、内存和PID限制实际生效。失败即停止发布，保留错误，不转为宿主测试。它是集成检查，不是任意代码沙箱的安全认证。容器参数依据[Podman run文档](https://docs.podman.io/en/latest/markdown/podman-run.1.html)。

预检通过后仍须用项目实际`DockerRunner`分别运行故障fixture与修复fixture，确认前者失败、后者成功；不能用探针或mock替代应用容器集成测试。

## 安装用户服务

`patchtrail.service`为用户级unit，安装在`/var/lib/patchtrail/.config/systemd/user/patchtrail.service`，不要放入系统服务目录直接以root启动。模板监听`127.0.0.1:3110`、单worker；它依赖`/opt/patchtrail/current`指向已通过检查的release。

以管理员从SSH操作专用账户的user manager时，可使用：

```sh
PT_UID="$(id -u patchtrail)"
sudo -u patchtrail -H env XDG_RUNTIME_DIR="/run/user/$PT_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$PT_UID/bus" systemctl --user daemon-reload
sudo -u patchtrail -H env XDG_RUNTIME_DIR="/run/user/$PT_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$PT_UID/bus" systemctl --user enable --now patchtrail.service
```

模板的`Delegate=yes`供rootless Podman使用；不要给承载映射助手的宿主服务添加`NoNewPrivileges=true`、`RestrictNamespaces`或`PrivateUsers`。检查容器本身必须保留这些边界所需的`--security-opt=no-new-privileges`等参数。UMask使用0022，保证仅被挂载的公开fixture工作区对容器非root UID可读；密钥和数据库依靠0700父目录与0600文件隔离。

控制面资源限制不等于所有Podman sibling scope的合计限制。每个检查容器另有限额，首版仅允许运行一个检查任务，并验证队列上限及公开请求限流。不要在2核主机上并发大量模型任务和容器。

## 对外入口与验收

默认只部署回环端口。公网入口应另外配置独立HTTPS代理和域名/端口，保留原443及8443；当前模板没有授权修改旧数学站代理。代理应限制请求体大小和连接时长，保护管理员Bearer令牌，转发`/api/`及前端静态文件。未完成HTTPS与公网验收时，只报告本地或SSH隧道访问，不称公开部署完成。

发布至少记录：

1. 代码commit、依赖版本、镜像digest、容器预检及实际fixture检查结果。
2. `/api/health`、任务列表、脚本演示到补丁审批和容器验证的实际结果。
3. 未授权live请求被拒，管理员令牌不进入网页、报告或trace。
4. DeepSeek真实调用的成功和失败，与脚本演示统计分开。
5. 原Xray、数学服务、监听端口与防火墙基线比较，确认没有被本项目重启或覆盖。

模板和本地测试通过不能代替这些服务器事实。

## 切版与回退

每个release独立保留源码和虚拟环境；完成候选检查后原子切换`/opt/patchtrail/current`软链接，只重启`patchtrail.service`。切版前记录旧目标。若健康检查失败，切回旧release并重启本服务，保留失败日志和新release以便定位。

数据库、运行工件和外部密钥文件不放入release目录，回退代码不会撤销业务状态。首版没有数据库回滚方案，发生schema不兼容时先停止应用并使用已核对的备份恢复流程，不能盲目启动旧代码。卸载或暂停时仅停止专用账户的`patchtrail.service`，不要停止其他服务，也不删除数据和镜像以“清理”。
