# GitHub App 一次性配置

已选择 GitHub App 作为长期同步身份。GitHub 的 App 注册和安装需要组织所有者
在网页中完成；之后的配置、同步及仓库迁移继续使用本地 CLI 和 Actions。
本组织现已配置并安装 App `5744796`，首个 `global_i18n` 生产同步及远端 SHA
核对已通过，记录见 `provenance/global_i18n-production-sync.json`。
以下步骤用于首次安装或后续重建授权。

1. 打开[预填配置的 App 注册页面](https://github.com/organizations/openharmony-qemu/settings/apps/new?name=OpenHarmony%20QEMU%20Mirror%20Sync&url=https%3A%2F%2Fgithub.com%2Fopenharmony-qemu%2Fmirror-sync&description=Daily%20OpenHarmony%20QEMU%20source%20mirror%20synchronization&public=false&webhook_active=false&request_oauth_on_install=false&contents=write&workflows=write)。
   确认归属 `openharmony-qemu`，权限为 Contents / Workflows 读写，Metadata 只读。
   不需要 webhook、OAuth 回调或组织管理权限。若名称已占用，可修改名称。
2. 创建后记录数字 **App ID**，在 Private keys 区域生成并下载 PEM 私钥。
3. 在 **Install App** 中安装到 `openharmony-qemu`，授权 registry 中已启用的目标仓库：
   `global_i18n`、`developtools_smartperf_host`、`device_soc_rockchip`、
   `multimedia_av_codec`、`kernel_linux_6.6`、`developtools_profiler`、
   `multimedia_audio_framework`、`xts_acts`。本组织当前安装已授权全部组织仓库，
   每个运行任务再收窄 token 到单个目标仓库。
   后续新增镜像需加入安装授权，随后启用对应的 registry 条目。
4. 在本地 mirror-sync checkout 执行（替换 ID 和私钥路径）：

   ```sh
   python3 scripts/configure_app.py --app-id 123456 --private-key /private/path/app.private-key.pem
   gh workflow run sync.yml --repo openharmony-qemu/mirror-sync -f repositories=global_i18n
   ```

脚本用 stdin 将私钥交给 `gh secret set`，写入 secret `MIRROR_APP_PRIVATE_KEY`，
并设置 Actions 变量 `MIRROR_APP_ID`。不要提交 PEM，也不要把密钥内容发送到聊天。
App ID 和本地 PEM 路径可以提供给本地迁移代理，以便由代理执行第 4 步。

首次生产同步成功后，再触发其余已启用仓库。检查 Actions 中的每个目标分支结果，
保留 artifact 内的原始/托管 SHA 映射。`prepare_only` 成功仅说明源下载和转换通过，
不说明 GitHub App 已获得目标仓库权限。

参考：[GitHub 官方预填注册参数](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-using-url-parameters)、
[官方 App token Action](https://github.com/actions/create-github-app-token)。
