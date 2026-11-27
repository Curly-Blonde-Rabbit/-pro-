# 系统安装候选包下载与校验

## 下载入口

| 网盘 | 分享链接 |
| --- | --- |
| 123 云盘 | [打开下载](https://1832518705.share.123pan.cn/123pan/Be3vTd-muTah) |
| 夸克网盘 | [打开下载](https://pan.quark.cn/s/cb8cbe78ef3a) |

分享链接由项目维护者提供；未提供单独提取码。夸克分享口令为 `/~20173caAyv~:/`，不是另一个提取码。网盘可能要求浏览器、客户端或登录；链接登记不等于已从两家网盘完整下载核验。

## 本地已生成的对应文件

- 版本：`v1.0.0-rc2 / install-candidate.1`
- 文件：`blackshark2pro-linux-v1.0.0-rc2-install-candidate.1.zip`
- 大小：`572723212` 字节（约 `546.19 MiB`）
- SHA-256：`8c125c0f83cf461d47d8ad9d59821f2c1fdb077f8218db8ea837149e3a982e10`

以该文件名、完整字节数及 SHA-256 识别版本，而不是仅凭网盘文件夹名称。约 1 MiB 的 `*-source.zip` 是源码，不是本安装包。

Windows PowerShell 校验：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '.\blackshark2pro-linux-v1.0.0-rc2-install-candidate.1.zip'
```

Linux 校验：

```sh
sha256sum blackshark2pro-linux-v1.0.0-rc2-install-candidate.1.zip
```

校验结果应与上方摘要完全一致。解压后运行 `python tools/install.py --verify` 可进一步校验包内构件；该命令不连接或修改手机。

## 包含与验收边界

包含 Ubuntu ARM64 根系统模板、无发布者身份的启动模板、A/B/机内救援生成与安装工具、新用户初始化、对应源码和校验文件。

**这是完整构件的安装候选包，不是已经完成整机安装验收的稳定 ROM。** 当前只接受安装说明中精确的 DLT-A0 存储几何和既有启动条件，不能用于其他机型或任意原厂布局。已执行文件镜像生成、检查和隔离认证测试；完整 Fastboot 安装、首次启动、冷启动、其他实机及全部恢复流程仍有未执行项。详见 [安装说明](installer-candidate.txt) 与 [验收状态](acceptance.md)。

保留 README 中 AI 辅助开发、实现可能存在问题的说明。作者可独立授权的原创部分仅限非商业使用，第三方内容仍按各自原许可证。
