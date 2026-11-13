# OpenHarmony QEMU mirror synchronization

本仓库集中维护 `openharmony-qemu` 独立镜像的同步脚本、上游配置和冲突 issue。
组件镜像仓库维护源代码；不安装本项目的同步工作流或同步脚本。

参考 [eclipse-oniro-mirrors/mirror-sync](https://github.com/eclipse-oniro-mirrors/mirror-sync)
的独立调度仓库结构。同步实现为本项目重新编写，采用快进更新和冲突报告。

## 调度与分支规则

- 每天 UTC 19:23（北京时间次日 03:23）触发，也可手动运行 `Synchronize mirrors`。
  GitHub 定时任务可能延迟；以 Actions 的实际运行记录为准。
- 同步每个已启用仓库上游的**全部分支**，保留原分支名称和完整 Git 历史。
  不使用 `push --mirror`、强制推送或自动删除目标分支。
- `OpenHarmony-*-Release-qemu` 是受保护的开发分支，同步任务不会写入。
  上游出现同名分支也会报告冲突。
- 每个固定基线同时保存到 `upstream/baseline-7.0`（多个基线使用数字后缀），
  为后续通过本地 Git 创建 QEMU 开发分支提供来源。已有 QEMU 分支由维护者提交修改。
- 无法快进或发生并发写入冲突时保留目标分支，在**本仓库**创建 issue；
  相同仓库和分支复用已有的未关闭 issue。其他分支和其他仓库继续同步，任务最终标红。
  解决后由维护者关闭 issue。网络、认证或容量错误在 Actions 中保留原始错误。
- 旧版同步形成的 `upstream/*` 分支保留作为历史证据；当前上游分支使用原名。

## 仓库清单与授权

`repositories.json` 来自 QEMU 7.0 的锁定源码清单，包括 OHOS 缺失镜像、SIG、TPC 和外部源。
`enabled: false` 表示目标仓库和授权仍待迁移，不参与定时任务。
已有 OpenHarmony GitHub fork 不纳入这个独立镜像清单。
TPC 使用 GitCode 官方源，避免依赖已检查过、缺失所需版本的 GitHub TPC 镜像。

组织禁用了 deploy key，因此使用 GitHub App 或限定目标仓库的 fine-grained PAT。
中央任务的 `GITHUB_TOKEN` 只负责读取本仓库及管理本仓库 issue。
任务不执行被镜像仓库中的代码。

推荐安装具有 **Contents: read/write、Workflows: read/write** 权限的 GitHub App，
授权清单中的目标镜像仓库。在本仓库设置 Actions 变量 `MIRROR_APP_ID` 和 secret
`MIRROR_APP_PRIVATE_KEY`。工作流完成上游下载和历史转换后，再为当前目标仓库
申请短期 token，以免下载大型仓库时消耗 token 的有效期。

也可配置 secret `MIRROR_TOKEN`，使用只授权目标镜像仓库、具有上述权限的
fine-grained PAT。若上游含 `.github/workflows`，同步这些源文件需要 Workflows 权限；
本项目的同步工作流仍只存在于本仓库。凭据不提交 Git，也不要放入聊天记录。

管理员添加镜像时先创建目标仓库并为 App/PAT 授权，再将清单条目设为
`enabled: true`。缺少凭据会在下载前明确失败，不会显示为同步成功。

## 大文件与可追溯性

GitHub 拒收历史中超过 100 MiB 的 Git blob。配置 `large_files: git-lfs` 的镜像
使用 `git lfs migrate import --everything --above=99MB` 保留文件内容并转换历史。
转换后 Git SHA 可能变化；每次同步上传原始分支 SHA、托管 SHA、固定基线和完整转换表，
作为保留 90 天的 `mirror-evidence-*` Actions artifact。长期使用的基线转换记录
应从 artifact 提交到 `provenance/`；该目录已保存首批三个镜像的基线记录。
转换和推送都保留完整历史，失败时不会改成浅克隆或丢弃大型文件。

本轮迁移只验证源码同步，不表示六架构包已构建或通过运行验证。

## 验证

```sh
python3 -m unittest discover -s tests -v
```

测试使用临时 Git 仓库验证全部分支、快进、重复运行、分支冲突、受保护分支、
目标分支保留、大文件历史转换与冲突 issue 去重，不访问生产仓库。
