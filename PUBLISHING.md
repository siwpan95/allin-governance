# 发布流程（PUBLISHING）

> 建仓、推送、发版三件事的统一入口与本仓**自己的**发版纪律。
>
> **为什么有这份文件**：2026-11-25 之前，建仓推送与发版散在
> 「allin 的 `gitee_release.py` + 包内 `_github/` + 包仓 `release.sh`」三处，
> 而纪律只写在文档与 CI 里 —— 结果 2.1.0 发了**只有 wheel 没有 sdist** 的版本，
> `.github/workflows/release.yml` 里明明写着「构建 sdist + wheel」。
> **纪律没有统一入口，就必然被当作结果而不是依据。**
> 所以本文件配套的是 `allin-governance publish` 这个**可执行入口**，不是一段建议。

---

## 0. 三条链路一张图

```
                    ┌──────────────────────────────────────────┐
   建仓/推送         │  allin-governance gitee  <cmd>           │  ← 权威端
                    │  allin-governance github <cmd>           │  ← 镜像端
                    └──────────────────────────────────────────┘
   发版（PyPI）      │  allin-governance publish <check|build|  │
                    │                          verify|upload|release>
                    └──────────────────────────────────────────┘
```

| 链路 | 工具 | 权威性 |
| --- | --- | --- |
| **Gitee** | `allin-governance gitee`（`_gitee/release.py`） | **权威** —— 建仓、转公开、发发行版 |
| **GitHub** | `allin-governance github`（`_github/github_release.py`） | 镜像 —— 从本地推，不从 Gitee 拉 |
| **PyPI** | `allin-governance publish`（`_publish/pypi.py`） | 独立 —— 有自己的一套判据 |

> **推送方向是单向的**：Gitee（权威）→ 本地 → GitHub（镜像）。
> 不要反向拉，也不要在 GitHub 上直接改。

---

## 1. 建仓（新项目从 0 到双核）

```bash
# ① Gitee 建仓（默认私有，推送后再转公开）
allin-governance gitee --name <repo> --repo . --desc "<一句话>" create

# ② 加远端并推
git remote add origin https://gitee.com/siwpan/<repo>.git
git push -u origin master

# ③ 转公开
allin-governance gitee --name <repo> --repo . public

# ④ GitHub 建仓 + 推镜像
allin-governance github --token "$GITHUB_TOKEN" create --name <repo> --desc "<一句话>"
git remote add github git@github.com:siwpan95/<repo>.git
git push -u github master
allin-governance github --token "$GITHUB_TOKEN" public --name <repo>

# ⑤ 三方对账（必须相等）
git rev-parse HEAD
git ls-remote origin refs/heads/master
git ls-remote github refs/heads/master
```

**四条硬规矩**（都吃过大亏）：

1. **远端 URL 里不许嵌令牌**。`git remote add` 时用
   `https://gitee.com/<owner>/<repo>.git`，认证交给
   `credential.https://gitee.com.helper=store`。嵌了令牌的 URL 会明文躺在
   `.git/config` 里（2026-11-25 建 allin-governance 时踩过，已改回）。
2. **建仓先私有，推送后再转公开**。半成品公开出去再收回是二次事故。
3. **转公开后做匿名复核**：`gitee ... status` / `github ... verify`，
   要看到 `private=False` 且匿名可读。
4. **起新仓第一步就把四件事做齐**：`git init` + README + LICENSE + registry 登记。
   发布区门禁 `publish_area_gate` 会拦「裸目录」——只建了目录没初始化的，
   任何机器跑巡检都会看到它。

---

## 2. PyPI 发版

### 2.1 四道判据（对应四个子命令）

| 子命令 | 判什么 | 不过会怎样 |
| --- | --- | --- |
| `publish check` | 版本**三处一致**：`pyproject.toml` / `__init__.py` / `VERSION` | 发出错版本号（用户装到旧内容） |
| `publish build` | 产出 **wheel + sdist 双产物** | **2.1.0 的坑**：只有 wheel 也照样能上传成功 |
| `publish verify` | dist 里**每个产物各装一次**，版本必须对 | sdist 缺文件时，只有走 sdist 的客户端才炸 |
| `publish upload` | `twine check` + 上传双产物 | 元数据错、README 渲染坏 |

### 2.2 标准发版（一条命令）

```bash
# 在包仓根
allin-governance publish release            # check → build → verify（不上传）
allin-governance publish release --upload   # 上面三步 + 真的上传
```

**这就是唯一入口。** 不要手打 `python -m build --wheel` —— 那正是 2.1.0 漏 sdist 的原因。

### 2.3 发版后

```bash
# 索引可能要等一会儿（实测 20~60 秒）
curl -s https://pypi.org/pypi/allin-governance/json | python -c "
import json,sys; d=json.load(sys.stdin)
print(sorted(d['releases'])[-1], [f['filename'] for f in d['releases'][sorted(d['releases'])[-1]]])"

# 双产物都要在，不是只有一个
```

> **同版本不可覆盖**。发错了只能升版本号再发，或者 `twine delete`（有窗口期）。
> 所以 `check` 放在第一步。

---

## 3. 消费方怎么升级（allin 仓一侧）

```bash
.venv/Scripts/python.exe -m pip install --force-reinstall --no-deps \
  /d/projects/repos/allin-governance/dist/allin_governance-<ver>-py3-none-any.whl
```

然后**同步版本自述**（`check_readme_refs` 会红，它是故意的）：

- `README.md` 的「当前版本 `<ver>`
- `architecture/system-architecture.svg` 的 `packages/allin-governance（PyPI，<ver>）`

---

## 4. 踩过的坑（都是真的，不是假设）

| 坑 | 现象 | 根因 |
| --- | --- | --- |
| **只发 wheel** | PyPI 上只有 `.whl` | 手打 `build --wheel`；CI 的「双产物」判据因为人没走 CI 而失效 |
| **令牌进 URL** | `.git/config` 里有明文 token | `git remote add` 时把凭据拼进了 URL |
| **`[\/]` 只匹配正斜杠** | Windows 路径下判据恒 false | JS 正则里 `[\/]` 等价 `[/]`，反斜杠要用 `[/\\]` 或干脆 `endsWith()` |
| **`run.ts` 用错解释器** | 装了新版却报旧路径 | 它自己找 `python3`（PATH 上另一份旧包）；要传 `DSH_GOVERNANCE_PYTHON` |
| **裸 curl 建 GitHub 仓失败** | HTTP 000 | 本机直连 `api.github.com` 不通，但 SSH 与包内 `_github` 的 API 路径都通 |
| **数据目录警告刷屏** | 每次构建 4 条 `Package would be ignored` | `baseline`/`preset`/`ledger` 没登记进 `packages` |

---

## 5. 这份文件怎么被 enforcing

光写不判就是纸。当前有三层：

| 层 | 手段 | 管什么 |
| --- | --- | --- |
| **命令层** | `publish release` 自带四道判据，任一步不过就停 | 走入口就不会漏 |
| **CI** | `.github/workflows/release.yml`：构建双产物 + 各装一次 | 走 CI 的路径 |
| **文档层** | 本文件 + README 指向它 | 让人知道入口在哪 |

> **注意第 1 层与第 2 层的关系**：CI 只保护「走 CI 的路径」。
> 人力发版绕过 CI 时，只有**命令层**能兜住 —— 这也是为什么把判据做进
> `publish` 子命令，而不是只写在 workflow 里。
