# OpenHarmony QEMU Mirror Sync

集中维护 `openharmony-qemu` 镜像仓库的上游同步。同步脚本和工作流仅存放在本仓库。

## 同步规则

- 每天北京时间 **03:23** 定时运行，也支持手动触发。
- 同步已启用仓库的全部上游分支，保留原分支名称和完整历史。
- 仅允许快进更新；不强制覆盖、不删除目标分支。
- 分支冲突在本仓库创建 issue，同一仓库、同一分支复用未关闭的 issue。
- 保留 `OpenHarmony-*-Release-qemu` 开发分支，不由同步任务更新。
- 同一仓库串行执行，不同仓库可并行。
- 优先发布已配置的固定基线；空仓库的大历史沿真实祖先提交分段快进上传。
- Git HTTP 传输使用 HTTP/1.1，避免已观察到的 HTTP/2 流中断。
- 指向相同提交的新分支合并发布；若批量发布被拒绝，逐分支核验并保留冲突内容。
- 先检查历史对象大小；没有超大文件时直接保留原始历史，已有 LFS 对象仍完整同步。
- 先上传准备阶段收集的全部 LFS 对象，再发布分支；避免逐分支重复扫描 LFS 历史。
- GitHub 临时传输错误会核对远端结果后重试，分支变化仍按冲突处理。

## 配置

`repositories.json` 定义镜像目标：

| 字段 | 用途 |
| --- | --- |
| `name` | `openharmony-qemu` 下的目标仓库名称 |
| `upstream_url` | 官方上游 Git 地址 |
| `enabled` | 是否参与自动同步 |
| `large_files` | `git-lfs` 表示将超过 99 MB 的历史文件转换到 Git LFS |
| `baseline_revisions` | 可选的固定上游提交，同时保留为基线引用 |

目标仓库需要提前创建。使用 GitHub App 授权写入，每个任务只申请对应目标仓库的
短期 token。安装和配置步骤见 [APP_SETUP.md](APP_SETUP.md)。

## 使用

```sh
# 同步全部已启用仓库
gh workflow run sync.yml --repo openharmony-qemu/mirror-sync

# 同步指定仓库，可使用逗号分隔多个名称
gh workflow run sync.yml --repo openharmony-qemu/mirror-sync -f repositories=global_i18n

# 只下载和检查上游，不发布目标分支
gh workflow run sync.yml --repo openharmony-qemu/mirror-sync -f repositories=global_i18n -F prepare_only=true

# 手动任务也可选用 GitHub 标准 ARM64 runner；定时任务仍使用默认 Ubuntu x64
gh workflow run sync.yml --repo openharmony-qemu/mirror-sync -f repositories=global_i18n -f runner=ubuntu-24.04-arm
```

每次运行生成 `mirror-evidence-*` artifact，记录分支 SHA、同步结果和 LFS 转换表，
保留 90 天。LFS 历史转换会改变 Git SHA；需要长期保留的记录由使用方归档。

## 验证

```sh
python3 -m unittest discover -s tests -v
```

参考 [eclipse-oniro-mirrors/mirror-sync](https://github.com/eclipse-oniro-mirrors/mirror-sync)
的集中调度方式，本仓库实现快进同步和冲突 issue 报告。
