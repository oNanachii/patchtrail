# 固定pytest检查镜像

这是部署准备配方，不是请求中的安装入口。基础Python镜像沿用已验证的完整digest；pytest和四个Linux依赖使用固定wheel版本及SHA256。构建时需要访问官方包源，检查任务运行时仍然禁网、只读、非root且使用`--pull=never`。

由既有专用rootless Podman账户在独立准备步骤构建：

```sh
podman build --pull=never -f deploy/pytest/Containerfile -t localhost/patchtrail-pytest:prepared deploy/pytest
podman image inspect --format '{{.Id}}' localhost/patchtrail-pytest:prepared
```

记录实际构建日志、安装版本和镜像ID。将输出的完整`sha256:...`本地镜像ID或已发布仓库digest配置为服务器`PATCHTRAIL_PYTEST_IMAGE`；不要把上面的可变tag直接作为已固定的运行来源。CLI本来已存在的基础镜像缺失时先由操作者处理，不由任务拉取。

构建成功仍需要通过实际`DockerRunner`执行一份成功、失败和零测试pytest任务，核对`complete`、`passed`、用例数和禁网/权限限制。单元解析检查不代替这项OCI集成。

固定pytest命令由`check_profiles.py`构造：忽略仓库pytest配置，关闭外部插件自动加载和缓存，使用独立容器内临时目录。只允许目录、Python测试文件及简洁`file.py::Class::test_method`节点；参数化测试可以通过选择文件运行，首版不直接接收`[parameter]`节点字符串。测试及`conftest.py`仍是会执行的Python代码，只能导入操作者授权的项目，不能把固定profile称为任意代码安全认证。

版本及wheel摘要来源于2026-10-28核对的官方PyPI JSON：[pytest 9.0.3](https://pypi.org/pypi/pytest/9.0.3/json)、[iniconfig 2.0.0](https://pypi.org/pypi/iniconfig/2.0.0/json)、[packaging 24.2](https://pypi.org/pypi/packaging/24.2/json)、[pluggy 1.5.0](https://pypi.org/pypi/pluggy/1.5.0/json)、[Pygments 2.20.0](https://pypi.org/pypi/pygments/2.20.0/json)。它们是明确选择的固定版本，不声称是最新发布组合。
