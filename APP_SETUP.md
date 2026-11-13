# GitHub App 配置

1. 打开[预填配置的注册页面](https://github.com/organizations/openharmony-qemu/settings/apps/new?name=OpenHarmony%20QEMU%20Mirror%20Sync&url=https%3A%2F%2Fgithub.com%2Fopenharmony-qemu%2Fmirror-sync&description=Daily%20source%20mirror%20synchronization&public=false&webhook_active=false&request_oauth_on_install=false&contents=write&workflows=write)，创建归属 `openharmony-qemu` 的私有 App。
2. 授予 **Contents / Workflows 读写**权限；Metadata 只读。不需要 webhook 或 OAuth 回调。
3. 记录数字 **App ID**，生成并下载 PEM 私钥。
4. 将 App 安装到 `openharmony-qemu`，授权 `repositories.json` 中启用的目标仓库。
5. 在本地 checkout 执行，替换 ID 和私钥路径：

   ```sh
   python3 scripts/configure_app.py --app-id 123456 --private-key /private/path/app.private-key.pem
   ```

脚本设置 Actions 变量 `MIRROR_APP_ID` 和 secret `MIRROR_APP_PRIVATE_KEY`，
通过 stdin 传递私钥，不将密钥写入命令参数或 Git。

工作流完成上游下载后，为单个目标仓库生成短期写入 token。Git credential helper
在有效期不足五分钟时续期，Git LFS 不缓存旧凭据；长时间上传和后续分支推送使用
同一认证方式。任务结束时撤销当前 token。本仓库的 `GITHUB_TOKEN` 负责冲突 issue。

也可使用具备上述权限、限定目标仓库的 fine-grained PAT，配置为 secret
`MIRROR_TOKEN`；配置 App 时优先使用 App。

参考：[GitHub App 注册参数](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-using-url-parameters)、
[GitHub App 安装令牌 API](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app)。
