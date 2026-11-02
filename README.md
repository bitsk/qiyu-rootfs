# Qiyu rootfs

多系统 rootfs 的准备和组件组装。系统、型号与使用场景分别扩展，首批为 Debian / SteamOS 和 MIX A081。

```sh
python3 tools/rootfs.py plan --system debian --device a081 --profile debug
python3 tools/rootfs.py plan --system steamos --device a081 --profile vr
python3 tools/rootfs.py prepare --system steamos --source /archives/official-steamos.zip --output /new/prepared
python3 tools/rootfs.py prepare --system debian --source https://deb.debian.org/debian --output /new/prepared
python3 tools/rootfs.py assemble --system steamos --base-archive /prepared/rootfs.tar.zst --artifacts /private/artifact-map.json --output /new/bundle
```

prepare 仅在主机准备新目录：SteamOS 核对官方 archive/image/partition 摘要并保留元数据；Debian 使用 debootstrap foreign-stage（需要 debootstrap 和相应主机权限），第二阶段必须在目标架构完成。SteamOS Python 依赖版本见 systems/steamos/legacy/profile.json。

assemble 生成基础归档的原样副本、设备配置 overlay.tar 和锁定提交/摘要的 manifest.json。artifact-map 是本地 JSON 数组，每项含 source、根相对 destination、sha256，用于输入各组件构建产物；不会自动启用服务、安装厂商库、封装 recovery 或刷写设备。没有 --artifacts 时只有板级配置，不构成完整 VR 运行系统。

新系统：在 systems/<name>/ 添加 adapter.json、adapter.py；prepare.program/arguments 是统一入口，输出 rootfs 归档。新型号：devices/<model>/model.json 声明兼容系统、架构与文件 overlay。新组合：profiles/<name>.json 选择组件，components.lock.json 固定 Git 提交。计划与组装自动使用新增条目，不需要创建更多 OS 仓库。

systems/*/legacy/ 保留原有设备侧初始化/配置和镜像配方；其中 /mnt/osstore 等路径属于既有 A081 安装流程，不能当作通用主机命令直接运行。devices/a081/ 保存已用的音频、传感器、热控来源；网络凭据、厂商固件和库不进入 Git。

这次是源码拆分和离线构建迁移，未重新验收硬件；已验证系统与 recovery 回退保持原状。工程架构、迁移状态、Wiki 与 Blog 见 [qiyu-docs](https://github.com/bitsk/qiyu-docs)。
