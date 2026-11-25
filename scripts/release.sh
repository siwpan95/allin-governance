#!/usr/bin/env bash
# 发版唯一入口 —— 把「双产物 + 各装一次 + 校验 + 上传」固化成本地一步。
#
# 为什么需要它（2026-11-25 事故）：
#   2.1.0 发版时手打了 `python -m build --wheel`，只产出 wheel 就传了 —— PyPI 上只有
#   wheel 没有 sdist。而 `.github/workflows/release.yml` 里明明写着「构建 sdist + wheel」
#   和「双产物各装一次」，**判据存在但手动发版绕过了它**。
#   手动流程越接近一条命令，越不容易漏。
#
# 用法：
#   bash scripts/release.sh            # 构建 + 校验，不上传
#   bash scripts/release.sh --upload   # 构建 + 校验 + 上传
#
# 需要：python（带 build/twine），默认取仓内 .venv；可用 PY=<解释器> 覆盖。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PY:-$REPO/.venv/Scripts/python.exe}"
[ -x "$PY" ] || PY="$(command -v python)"
# twine 不一定装在建包解释器里（本机就在 E:/Python314）——单独探测，避免发版脚本中途失败
HAVE_TWINE() { "$1" -c "import twine" >/dev/null 2>&1; }
TW="${TW:-$PY}"
if ! HAVE_TWINE "$TW"; then
  for cand in /e/Python314/python.exe python; do
    if HAVE_TWINE "$cand"; then TW="$cand"; break; fi
  done
fi
HAVE_TWINE "$TW" || { echo "[ERR] 找不到带 twine 的解释器（设 TW=<python>）" >&2; exit 3; }
VER="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
echo "== 发版 allin-governance $VER（$PY）"

# ① 版本三处一致
INIT_VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/allin_governance/__init__.py | head -1)"
FILE_VER="$(tr -d '[:space:]' < VERSION)"
[ "$VER" = "$INIT_VER" ] && [ "$VER" = "$FILE_VER" ] || {
  echo "[ERR] 版本不一致：pyproject=$VER __init__=$INIT_VER VERSION=$FILE_VER" >&2; exit 1; }

# ② 构建**双产物**（不带 --wheel / --sdist，让 build 两个都出）
rm -rf dist build
"$PY" -m build --outdir dist . >/dev/null
ls dist/*.whl >/dev/null 2>&1 || { echo "[ERR] 没有 wheel" >&2; exit 1; }
ls dist/*.tar.gz >/dev/null 2>&1 || { echo "[ERR] 没有 sdist —— 这就是 2.1.0 的坑" >&2; exit 1; }
echo "== 产物：$(cd dist && ls | tr '\n' ' ')"

# ③ 校验：twine check 双产物
"$TW" -m twine check dist/* >/dev/null 2>&1 || "$TW" -m twine check dist/* || {
  echo "[ERR] twine check 失败" >&2; exit 1; }
echo "== twine check 通过"

# ④ 双产物各装一次（sdist 自包含的实证）
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
for art in dist/*.whl dist/*.tar.gz; do
  "$PY" -m venv "$TMP/v" >/dev/null 2>&1
  "$TMP/v/bin/python" -m pip install -q "$art" 2>/dev/null \
    || "$TMP/v/Scripts/python.exe" -m pip install -q "$art"
  GOT="$("$TMP/v/bin/python" -c 'import allin_governance;print(allin_governance.__version__)' 2>/dev/null \
    || "$TMP/v/Scripts/python.exe" -c 'import allin_governance;print(allin_governance.__version__)')"
  [ "$GOT" = "$VER" ] || { echo "[ERR] $(basename "$art") 装出来是 $GOT，不是 $VER" >&2; exit 1; }
  echo "== $(basename "$art") 装成 $GOT"
  rm -rf "$TMP/v"
done

if [ "${1:-}" = "--upload" ]; then
  "$TW" -m twine upload --non-interactive --repository pypi dist/* 2>&1 | tail -3
  echo "== 已上传（wheel + sdist）"
else
  echo "== 仅构建校验；加 --upload 才上传"
fi
