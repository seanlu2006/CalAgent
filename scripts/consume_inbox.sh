#!/bin/bash
# CalAgent — 被 launchd 在 cal_inbox.txt 變動時自動叫起來,消化收件匣。
#
# 這支腳本自己不解析任何東西,它只負責:
#   1. 確認收件匣不是空的(只看檔案大小;空的就直接退出,不燒 token)
#   2. 上鎖(iCloud 同步會連續觸發好幾次,不鎖會重複建事件)
#   3. 把工作交給 headless Claude,規則在 CLAUDE.md 裡
#   4. 留下 log
set -uo pipefail


# 從腳本自己的位置推回專案根目錄,repo clone 到哪都能跑
REPO="$(cd "$(dirname "$0")/.." && pwd)"
INBOX="$HOME/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/cal_inbox.txt"
LOCK="$REPO/.consume.lock"
LOG="$REPO/data/run_log.md"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

mkdir -p "$REPO/data"
stamp() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$1" >> "$LOG"; }

# --- 1. 有東西要做嗎 ---
#
# ⚠️ 這裡刻意「不讀內容」,原因是 macOS TCC:
# launchd 生成的程序自己就是 responsible process,你給終端機的權限對它無效。
# 實測:沒授權時 stat 拿得到檔案大小,但讀內容回 Operation not permitted。
#
# 最直覺的解法是給 /bin/bash 完全取用磁碟,但那等於讓這台機器上每一支
# shell 腳本都能讀 Mail、Messages、瀏覽器資料。所以分工改成:
# bash 只看「檔案是不是空的」(stat,不需要權限),內容交給 claude 自己讀,
# 要授權的只有 claude 這一個 binary。
[ -f "$INBOX" ] || exit 0

SIZE=$(stat -f%z "$INBOX" 2>/dev/null || echo 0)
if [ "${SIZE:-0}" -le 1 ]; then
  exit 0   # 空檔(或只剩一個換行),沒事做,不燒 token
fi

# --- 2. 上鎖:iCloud 同步一次會觸發多次 WatchPaths ---
if ! mkdir "$LOCK" 2>/dev/null; then
  # 鎖存在超過 10 分鐘視為上次跑掛了,清掉
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null && mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# 等 iCloud 把這一波同步寫完,避免讀到半截
sleep 3

# --- 3. 交給 Claude,規則在 CLAUDE.md ---
if [ ! -x "$CLAUDE_BIN" ]; then
  log ""
  log "## $(stamp) — 跳過"
  log "- 收件匣有 ${SIZE} bytes 待處理,但找不到 claude CLI(\`$CLAUDE_BIN\`)。"
  log "- 自動消化沒跑。回電腦後在 CalAgent 裡說一聲「整理行程」即可手動消化。"
  exit 0
fi

cd "$REPO" || exit 1
# 權限:純白名單,這裡踩過坑,改之前先讀完。
# - 不用 --permission-mode acceptEdits。那個模式會自動核准工作目錄裡「任何」檔案的
#   編輯,包括 scripts/create_event.py 本身;agent 改掉它再執行,「只新增」就破功了。
#   改成只准編輯 data/ 和收件匣,其他路徑的編輯會觸發詢問,而
#   --permission-prompts none 讓詢問直接變成拒絕(需 Claude Code v2.1.259+)。
# - 不開 Bash(osascript *)。任意 AppleScript 能刪事件,也能操控 Mail、Messages。
#   查行程只能透過唯讀的 list_events.py,建事件只能透過只會新增的 create_event.py。
# - --add-dir:收件匣在工作目錄外,不加的話連讀都要核准。
# - 不要加 --bare:那會跳過 CLAUDE.md,而且不讀 OAuth 憑證、需要 ANTHROPIC_API_KEY。
OUT=$("$CLAUDE_BIN" -p "讀 CLAUDE.md 第 0 節,消化 cal_inbox.txt。
你現在是在無人監督的情況下自動執行,務必遵守第 4 節的護欄:
單次最多建 10 件、只新增不刪改、有疑慮的不建並搬進 data/pending.md、
時間已過的不建、相對時間用每行開頭的捕捉時間戳當錨點。
處理完每一行都要從 cal_inbox.txt 移除(成功的進 archive、有疑慮的進 pending)。
最後把這次的結果 append 到 data/run_log.md。" \
  --append-system-prompt "你是被排程叫起來的自動任務,沒有人在看。所有事情自己做完,不要派 subagent,不要問問題,沒有人能回答。讀寫收件匣請用 Read/Write 工具(該目錄已用 --add-dir 納入)。你只能編輯 data/ 底下的檔案和收件匣,碰不到的就不要試。有疑慮的就搬進 pending.md 並記進 run_log.md。" \
  --add-dir "$(dirname "$INBOX")" \
  --permission-prompts none \
  --allowedTools "Read,Edit(/data/**),Edit(~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/cal_inbox.txt),Bash(python3 scripts/create_event.py *),Bash(python3 scripts/list_events.py *),Bash(date *)" \
  2>&1)
RC=$?

log ""
log "## $(stamp) — 自動消化(收件匣 ${SIZE} bytes,exit=$RC)"
log ""
log '```'
log "$OUT"
log '```'

# 明確回傳 claude 的結果。沒有這行的話,腳本的 exit code 會是 EXIT trap 裡
# rmdir 的回傳值——成功跑完卻回 1,以後看 launchctl 的 last exit code 會被誤導。
exit "$RC"
