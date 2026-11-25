# allin-governance

工作区治理中枢的**公共核**：可执行门禁 + 文档枢纽契约 + 仓库体检 + 机器事实采集。

面向「一人多仓 + AI 协作者」的场景：把「换台机器还能用」的部分装成一个包，
让消费仓（如 [`allin`](https://gitee.com/siwpan/allin)）只通过版本号消费它。

## 为什么独立成仓

参照 `3dworld`（库仓）↔ `3dworld-demos`（消费仓，`pip install 3dworld==0.9.0` 锁版）的形态：

**结构即判据** —— 库仓不读消费仓任何文件，消费仓只通过版本号消费库。
此前公共核与治理中枢/事实源/记录住在同一个仓里，边界只能靠目录约定 + 门禁事后拦。

分仓后有一个直接的构建收益：**跨目录 `package-dir` 映射全部取消**，
随之「包内副本是否与仓内真身一致」这一整类对账问题从根上不存在
（`check_pkg_parity.py` 因此可以删）。

## 包里有什么

| 子命令 | 职责 |
| --- | --- |
| `repocheck` | 仓库体检：30 条规则 + 0–10 健康分 |
| `init` / `verify` | 生成 / 复核治理骨架 |
| `scan` | 发布区扫盘：项目指纹、重复检出、迁移残留 |
| `deps` | 依赖三方对账（声明 / 实装 / import） |
| `github` | 建仓、推送、三端同步巡检（**镜像端**） |
| `gitee` | 建仓、转公开、发发行版（**权威端**，与 `github` 配对成双核） |
| `publish` | **PyPI 发版链路**：`check` / `build` / `verify` / `upload` / `release` |
| `secrets-scan` | 离线凭据扫描（21 规则 + 熵闸，零外部二进制） |
| `facts` | 机器事实：采集本机 + 校验分区文件断言（**支持无仓模式**） |

所有子命令接受 `--root <目录>` 与位置参数两种写法（1.1.2 起统一）。

### 发布链路 → [`PUBLISHING.md`](PUBLISHING.md)

建仓、推送、发版原本散在三处（allin 的 gitee 工具 + 包内 github + 包仓脚本），
2.1.0 因此只在 PyPI 发了 wheel、漏了 sdist —— **纪律没有统一入口就会被当作结果而不是依据**。
现在统一成 `gitee` / `github` / `publish` 三个子命令，流程与判据见 [`PUBLISHING.md`](PUBLISHING.md)。

```bash
allin-governance publish release --upload   # 发版的唯一入口（双产物是硬判据）
```

## 安装

```bash
pip install allin-governance
allin-governance --version
allin-governance facts measure          # 换机第一件事：这台机器长什么样
```

当前版本 `2.0.2`（以 [PyPI](https://pypi.org/project/allin-governance/) 为准）。

运行时零第三方依赖——治理工具要能在「一台什么都没装的机器」上跑起来。

### 发版前校验（维护者）

```bash
python -m build                            # 构建 sdist + wheel
python scripts/release_preflight.py --install   # 版本对账 / dist 干净 / sdist 自包含 / 双产物各装一次
```

`--install` 是 1.0.0 事故（wheel 好、sdist 坏）的唯一鉴别实验，发版前必跑；
`twine check` 与 `--install` 在无人值守场景记为「未验证」，不等于通过。

## 边界

本仓只装**判定器 / 采集器 / 骨架执行器 / 只读模板 / 接口契约**；
实例台账、机器特化数据、记录类、运行产物一律不进这里
（判据见消费仓的 `SPEC-206 公共核边界`）。

## 许可

MIT-0
