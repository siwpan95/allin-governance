# P0 报告 · 公共核提仓 dry-run

> 生成于 2026-11-25，`@SIWPAN`。本报告是只读盘点的产物，**未改动 allin 仓任何文件**。

## 一、要搬什么

| 源目录（allin 仓内） | 文件 | 体积 | → 包内落点 |
| --- | --- | --- | --- |
| `tools/gate/governance` | 52 | 512 K | `src/allin_governance/_gatesuite` |
| `tools/repocheck` | 36 | 299 K | `src/allin_governance/_repocheck` |
| `tools/_shared` | 7 | 51 K | `src/allin_governance/_toolkit` |
| `tools/github` | 3 | 34 K | `src/allin_governance/_github` |
| `tools/secret_scan` | 2 | 18 K | `src/allin_governance/_secret_scan` |
| `tools/facts` | 12 | 114 K | `src/allin_governance/_facts` |
| `packages/allin-governance/src/allin_governance` | 5 | 77 K | `src/allin_governance` |

**合计 117 文件 / 1.08 MB**（不含 `__pycache__`）。

## 二、代码改造量：**极小**

跨目录绝对导入（`from tools...`）**只有 1 处**：
- `tools/github/github_release.py:68` — `from tools`

其余都是包内相对导入或独立模块 → 提仓后**不需要改代码**，只需重写 pyproject（映射清零）。

## 三、真正的成本：allin 侧引用

| 目录 | 被引用文件数 |
| --- | --- |
| `tools/gate/governance` | 59 |
| `tools/repocheck` | 49 |
| `tools/_shared` | 14 |
| `tools/github` | 8 |
| `tools/secret_scan` | 5 |
| `tools/facts` | 42 |
| `packages/allin-governance/src/allin_governance` | 0 |

按位置分组（去重后合计 **110 处文件**）：

| 位置 | 文件数 | 性质 |
| --- | --- | --- |
| 其他 | 48 | 必须改（真实调用） |
| 文档引用（P1 后按需改指针） | 45 | 按需改指针或留旧路径 README |
| 仓测试 | 6 | 必须改（真实调用） |
| CI | 2 | 必须改（真实调用） |
| DSH preset 治理段 | 4 | 必须改（真实调用） |
| 门禁编排主链 | 1 | 必须改（真实调用） |
| platform 门户/CLI 总线 | 4 | 必须改（真实调用） |

**结论：我原计划里写的「约 25 处要改」低估了 4 倍。**
准确说法是——**代码几乎不用改（1 处），但调用方引用约 99 处**，其中：

- 硬调用（CI / pre-commit / skills / scripts / platform / tests）约 54 处：**必须改**；
- 文档引用 45 处：可在旧路径留一份 `README.md` 指针（"源码已迁至 allin-governance 仓"），
  把 45 处一次性降级为「指针对指针」，不必逐条改。

## 四、P1 的执行顺序（据此修正）

1. 复制 117 文件到 `src/allin_governance/{_gatesuite,_repocheck,_toolkit,_github,_secret_scan,_facts}/`；
2. 重写 `pyproject.toml`：**删除全部 `[tool.setuptools.package-dir]` 映射**（这是提仓的核心收益）；
3. 改那 1 处 `from tools`；
4. 加旧路径指针（allin 仓 6 个目录各留一个 `README.md` 指向新仓 + 迁移日期）；
5. 构建 + 解包自检（无断链、8 子命令可跑）；
6. 在 allin 仓把硬调用 54 处改成 `allin-governance` CLI；
7. 全量门禁绿 → 才算 P2 完成。
