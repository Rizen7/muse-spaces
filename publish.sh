#!/usr/bin/env bash
# 把伙计的 markdown 渲染成静态页，推到 Rizen7/muse-spaces，并等到 GitHub Pages 返回本次「更新于」时间。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

usage() {
  echo "用法: $0 <news|deals> <源md路径> [--sample]" >&2
  exit 2
}

log() { echo "$@" >&2; }

TYPE="${1:-}"
SRC="${2:-}"
SAMPLE=0
shift $(( $# > 0 ? 1 : 0 ))
shift $(( $# > 0 ? 1 : 0 )) || true
for arg in "$@"; do
  case "$arg" in
    --sample) SAMPLE=1 ;;
    *) usage ;;
  esac
done

if [[ "$TYPE" != "news" && "$TYPE" != "deals" ]]; then
  usage
fi
if [[ -z "$SRC" ]]; then
  usage
fi

if [[ ! -d .git ]]; then
  log "错误：${ROOT} 还不是 git 仓库。"
  exit 1
fi

REMOTE="$(git remote get-url origin)"
if ! [[ "$REMOTE" =~ github.com[:/][Rr]izen7/muse-spaces(\.git)?$ ]]; then
  log "错误：origin 不是 Rizen7/muse-spaces，已中止，以免改到其他仓库。当前 origin: ${REMOTE}"
  exit 1
fi

RENDER_ARGS=("$TYPE" "$SRC")
if [[ "$SAMPLE" -eq 1 ]]; then
  RENDER_ARGS+=(--sample)
fi

log "正在渲染 ${TYPE} ..."
python3 "$ROOT/render.py" "${RENDER_ARGS[@]}"
# 首页入口一并保持最新（试跑标记与内容页一致）
INDEX_ARGS=(index)
if [[ "$SAMPLE" -eq 1 ]]; then
  INDEX_ARGS+=(--sample)
fi
python3 "$ROOT/render.py" "${INDEX_ARGS[@]}"

OUT="$ROOT/$TYPE/index.html"
if [[ ! -f "$OUT" ]]; then
  log "错误：没有生成 $OUT"
  exit 1
fi

UPDATED="$(python3 - "$OUT" << 'PY'
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r"更新于\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2})", text)
print(m.group(0) if m else "")
PY
)"
if [[ -z "$UPDATED" ]]; then
  log "错误：生成的页面里没有「更新于」时间，无法验收。"
  exit 1
fi
log "页面时间：${UPDATED}"

if [[ -z "$(git config --get user.name)" ]]; then
  git config user.name "Rizen7"
fi
if [[ -z "$(git config --get user.email)" ]]; then
  git config user.email "338553914+Rizen7@users.noreply.github.com"
fi

git add -- "index.html" "$TYPE/index.html" ".nojekyll"
# 配图目录一并纳入本次发布（只加 img/，不加其他文件）
if [[ -d img ]]; then
  git add -- "img/"
fi
if git diff --cached --quiet; then
  log "工作区没有新的页面改动，仍去检查线上是否已是这一版。"
else
  git commit -m "publish ${TYPE}: ${UPDATED}"
  log "正在推送到 Rizen7/muse-spaces ..."
  GIT_TERMINAL_PROMPT=0 git push origin HEAD:main
fi

# GitHub Pages 用户名在 URL 里是小写
PAGE_URL="https://rizen7.github.io/muse-spaces/${TYPE}/"
TMP_HTML="$(mktemp)"
cleanup() { rm -f "$TMP_HTML"; }
trap cleanup EXIT

DEADLINE=$((SECONDS + 300))
HTTP_CODE="000"
REASON="timeout waiting for GitHub Pages to serve ${UPDATED}"
log "等待线上页面就绪（最多 5 分钟）：${PAGE_URL}"
while (( SECONDS < DEADLINE )); do
  HTTP_CODE="$(curl -sS -L --max-time 20 -o "$TMP_HTML" -w "%{http_code}" "$PAGE_URL" || echo "000")"
  if [[ "$HTTP_CODE" == "200" ]] && grep -q -F "$UPDATED" "$TMP_HTML"; then
    printf '{"ok":true,"url":"%s"}\n' "$PAGE_URL"
    exit 0
  fi
  sleep 8
done

printf '{"ok":false,"url":"%s","http_code":"%s","reason":"%s"}\n' "$PAGE_URL" "$HTTP_CODE" "$REASON"
exit 1
