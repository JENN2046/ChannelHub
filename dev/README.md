# ChannelHub 本机开发与宿主兼容试点

仅用于固定 `0.4.1` 插件的开发底座验收。不是生产启动器，不连接真实渠道、模型服务或生产数据。
稳定插件的 46 个文件保持不变；跨端连续性不属于本试点。

## 日常使用

在本仓库根目录运行：

```bash
python3 -B dev/devctl.py doctor
python3 -B dev/devctl.py check
python3 -B dev/devctl.py start
```

默认宿主为 `reference`，保留第一期行为。第二期支持两个固定宿主：

| `--host` | 固定源码 | 验证范围 |
|---|---|---|
| `reference` | `fff563b2d33ad8aa93474714e050619a07e353e4` | 原有完整 ChannelHub/OneBot 回归、原生管理与模拟链路 |
| `tencent-source` | `a4ab674a403f10b7f91fcfc242719204621b2bb9` | 目标宿主原生 PluginManager、管理与模拟链路、禁用/缺配置/503/超时/drain 兼容场景 |

```bash
python3 -B dev/devctl.py doctor --host tencent-source
python3 -B dev/devctl.py check --host tencent-source
python3 -B dev/devctl.py start --host tencent-source
```

`tencent-source` 对应已核对的开发机登记源码，尚未确认它就是实际运行版本。
这里只在本机执行其隔离副本，不安装或切换开发机。任意 URL、Commit 或其他 profile 名称不被接受。
一次只运行一个可变实例；`stop/reset` 根据已记录实例工作，不接受宿主选择参数。

`start` 在前台运行，完成模拟消息和管理页自检后打印本机 URL。
浏览器打开该 URL 即可使用原生管理页；这里只能操作隔离测试状态。
终端显示源码 Commit、是否有未提交修改，以及本次源码快照的哈希。
关闭时按 `Ctrl-C`，也可以在另一个终端执行：

```bash
python3 -B dev/devctl.py stop
python3 -B dev/devctl.py reset
```

`stop` 等待原生 drain/shutdown，保留测试现场。
`reset` 在确认停止后删除该实例的私有宿主和测试状态，保留依赖缓存与候选包。
再次 `start` 前须处理上一实例的 `reset`，避免覆盖现场记录或堆积大型测试宿主。
重复启动、运行中重置、未知进程或失效实例记录均拒绝自动接管。
若停止无法确认，不按端口杀进程，不自行删除 `active.json`；保留现场检查。

## 工具链与首次准备

- Linux、Python 3 标准库、Git、bubblewrap、zip/unzip、GNU cp。
- 固定 Node `24.22.1` 和 npm `10.9.8`。当前本机路径由 `devctl.py` 明确绑定；不修改默认 Node。
- ChannelHub 源基线 `001e50997d94ed108382a09d1d968cffee6b8919`。
- 测试宿主 `JENN2046/VCPToolBox@fff563b2d33ad8aa93474714e050619a07e353e4`。
- 目标源码宿主 `JENN2046/VCPToolBox@a4ab674a403f10b7f91fcfc242719204621b2bb9`。

`doctor` 只检查，不安装或修复。
首次 `check/start/prepare` 获取固定宿主并准备依赖：这个阶段允许网络，但在隔离文件系统中安装，
固定 lockfile，禁用生命周期脚本、审计和用户 npm 配置，不继承凭据。
准备完成后，回归测试和管理页在独立网络命名空间运行。
隔离能力不可用时直接失败，不降级为普通宿主运行。

资源归属：

| 位置 | 用途与退出条件 |
|---|---|
| `$HOME/.cache/agents-os-dev/channelhub/host.git` | 固定宿主普通源码来源；后续试点可复用 |
| 同目录 `deps-r2-<宿主SHA>-<lock哈希>-node24.22.1-npm10.9.8/` | 分宿主、lockfile 和工具链的依赖缓存，仅作为测试输入；后续试点可复用 |
| 同目录 `tasks/` | 任务私有测试宿主；成功检查/打包自动回收，正常启动由停止后的 reset 回收 |
| 同目录 `candidates/` | 已验证候选及结果；保留供审查，不自动发布 |
| `$HOME/.local/share/agents-os-dev/channelhub/` | 归属标记、锁、当前实例和最近一次检查结果 |

测试宿主中的 `node_modules` 是独立复制，支持时采用 reflink；原生测试可在其下创建临时 fixture。
共享缓存和日常源码不会被测试写入。失败任务保留在 `tasks/`，排查后仅按其归属标记和实际进程状态回收，
不把整个缓存目录交给任意清理命令。辅助程序没有自定义删除路径参数。
第一期旧依赖缓存和候选继续保留，不因第二期新增 profile 自动迁移或清理。

## 候选包

先审查并提交本项目内的开发辅助代码，使工作树干净，然后运行：

```bash
python3 -B dev/devctl.py prepare
```

默认仍仅验证 `reference`。第二期双宿主交付使用：

```bash
python3 -B dev/devctl.py prepare --host all
```

`all` 仅供 `prepare` 使用：生成一次 ZIP，分别安装到两个独立宿主，逐项验证相同 ZIP 哈希。
只有两个宿主都通过，才产生 `DUAL_HOST_CANDIDATE_VERIFIED_ONLY` 候选；任一失败则返回非零，
在任务私有目录保留 `matrix-failure.json`，区分成功、失败与未运行项，不创建成功候选。
验收记录保留原有字段，并增加 `verified_profiles` 和 `verification_matrix`，每行绑定宿主、依赖、同一产物哈希和实际验证范围。

该命令从 Commit 导出到任务私有目录，调用现有打包脚本，并验证最终 ZIP：

1. 文件集合为固定插件的 46 个文件，无额外运行配置、数据库、符号链接或越界路径。
2. 解包到干净的固定宿主，运行源码布局、ChannelHub 和 OneBot 回归。
3. 在原生 PluginManager 中检查管理页、合成 Agent、模拟消息、去重、伪造身份拒绝、空 outbox 和 drain/shutdown。
4. 核对安装内容和 ZIP 哈希未变化，保存 ZIP、校验和及绑定源码/宿主/工具链的验收记录。

目标 profile 不包含三套渠道适配器，因此其完整适配器回归由 reference profile 承担；
目标 profile 只安装 ChannelHub，并运行上述针对性兼容检查与原生管理/模拟场景。
目标宿主核心文件只读挂载，验收前后核对原始核心文件哈希；不复制参考宿主核心文件来补齐接口。

打包脚本在独立导出树中执行，其固定 `dist` 路径只属于该任务，不会覆盖日常源码或旧候选。
候选记录不包含真实数据、凭据或本机绝对路径。

## 隔离边界

沙盒只获得指定工具链、普通源码、私有测试宿主和合成状态；不挂载用户 Home、SSH 或生产目录。
原生 API 与回调只在沙盒内部回环网络运行；外层回环转发仅允许 ChannelHub 管理路由，
检查 Host/Origin，不暴露模型调用、B2 回调或内部关闭路由。
开发宿主只加载 ChannelHub，配置值为合成测试值。

目标源码没有独立的 `modules/apiBearerAuth.js`，其真实 Bearer 中间件位于 `server.js`。
目标 profile 的开发夹具使用明确标记的合成 Bearer 门禁，`authMode=synthetic`；
真实 `server.js` 仅做静态接口核对，单独记录源码哈希与 `full_server_executed=false`。
reference profile 使用它原有的鉴权模块，`authMode=native`，但同样不运行完整服务器。
两种情况都不构成真实服务器权限、配置或真实渠道验收。

开发辅助测试只检查新增命令的资源归属、转发限制、重置边界和候选完整性；业务回归直接复用既有测试。
通过本试点不代表真实渠道、完整 VCP、开发机部署或生产验收通过。
