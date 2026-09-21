#!/usr/bin/env python3
"""把一件事寫進 Apple 行事曆。CalAgent 唯一有權寫入的出口。

設計原則:
- 只新增,不刪除、不修改既有事件(無人監督執行時的護欄)。
- 日期用數值逐項設定,不用 AppleScript 的日期字串——後者依賴系統 locale,
  中文環境下 `date "2026年9月21日"` 的解析行為不穩。
- 每建立一筆就寫進 data/event_log.csv,留下 uid 供事後回溯與撤銷。
"""
import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENT_LOG = ROOT / "data" / "event_log.csv"
LOG_HEADER = ["logged_at", "uid", "calendar", "title", "start", "end", "all_day", "source_line"]


def _as(dt: datetime, var: str) -> str:
    """產生不依賴 locale 的 AppleScript 日期建構片段。

    先把 day 設成 1 再設 year/month,否則在 31 號執行時設定 2 月會溢位到 3 月。
    """
    return f"""
set {var} to current date
set day of {var} to 1
set year of {var} to {dt.year}
set month of {var} to {dt.month}
set day of {var} to {dt.day}
set hours of {var} to {dt.hour}
set minutes of {var} to {dt.minute}
set seconds of {var} to 0
"""


def run_osascript(script: str, timeout_sec: int = 300) -> str:
    """跑 AppleScript,並且把 AppleEvent 的等待上限拉長。

    預設的 AppleEvent timeout 只有 60 秒。互動環境下 Calendar.app 通常是熱的,
    夠用;但從 launchd 叫起來時它是冷的,冷啟動 + 載入所有行事曆(含訂閱的節日、
    生日)常常超過 60 秒,回 -1712 timeout,看起來像壞掉其實只是還沒醒。
    """
    wrapped = f"with timeout of {timeout_sec} seconds\n{script}\nend timeout"
    r = subprocess.run(
        ["osascript", "-e", wrapped],
        capture_output=True, text=True, timeout=timeout_sec + 30,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def list_calendars() -> list[str]:
    out = run_osascript('tell application "Calendar" to get name of every calendar')
    return [c.strip() for c in out.split(",")]


def find_conflicts(calendar: str, start: datetime, end: datetime) -> list[str]:
    """回報同一個行事曆裡與 [start, end) 重疊的既有事件標題。"""
    script = f"""
{_as(start, "s")}
{_as(end, "e")}
set output to ""
tell application "Calendar"
  tell calendar "{calendar}"
    repeat with ev in (every event whose start date < e and end date > s)
      set output to output & (summary of ev) & " (" & (start date of ev as string) & ")" & linefeed
    end repeat
  end tell
end tell
return output
"""
    out = run_osascript(script)
    return [line for line in out.splitlines() if line.strip()]


def create_event(calendar, title, start, end, all_day=False, alarm=None, note="") -> str:
    props = [
        f'summary:"{escape(title)}"',
        "start date:s",
        "end date:e",
    ]
    if all_day:
        props.append("allday event:true")
    if note:
        props.append(f'description:"{escape(note)}"')

    alarm_stmt = ""
    if alarm is not None:
        # 負數 = 事件開始前 N 分鐘
        alarm_stmt = f"""
    tell newEvent
      make new display alarm at end with properties {{trigger interval:{-abs(int(alarm))}}}
    end tell
"""

    script = f"""
{_as(start, "s")}
{_as(end, "e")}
tell application "Calendar"
  tell calendar "{calendar}"
    set newEvent to make new event with properties {{{", ".join(props)}}}
{alarm_stmt}
    set theUid to uid of newEvent
  end tell
end tell
return theUid
"""
    return run_osascript(script)


def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def log_event(uid, calendar, title, start, end, all_day, source_line):
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    is_new = not EVENT_LOG.exists()
    with EVENT_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(LOG_HEADER)
        w.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            uid, calendar, title,
            start.strftime("%Y-%m-%dT%H:%M"),
            end.strftime("%Y-%m-%dT%H:%M"),
            "yes" if all_day else "",
            source_line,
        ])


def parse_dt(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"看不懂的時間格式: {s}(要 YYYY-MM-DDTHH:MM)")


def main():
    p = argparse.ArgumentParser(description="把一件事寫進 Apple 行事曆")
    p.add_argument("--calendar", required=True, help="行事曆名稱(個人/工作/學校)")
    p.add_argument("--title", required=True)
    p.add_argument("--start", required=True, type=parse_dt)
    p.add_argument("--end", type=parse_dt, help="省略則依 --duration")
    p.add_argument("--duration", type=int, default=60, help="沒給 --end 時的時長(分鐘),預設 60")
    p.add_argument("--all-day", action="store_true")
    p.add_argument("--alarm", type=int, help="提前 N 分鐘提醒")
    p.add_argument("--note", default="")
    p.add_argument("--source-line", default="", help="原始語音行,寫進 event_log 供回溯")
    p.add_argument("--allow-conflict", action="store_true", help="就算撞行程也照建")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    from datetime import timedelta

    start = a.start
    end = a.end

    if a.all_day:
        # 全天事件:起點正規化到當天 00:00,沒給 --end 就是整整一天。
        # 不做這個的話,「--all-day 但忘了 --end」會套用 60 分鐘的預設時長,
        # 建出一個聲稱是全天、實際只有一小時的事件。
        start = start.replace(hour=0, minute=0)
        if end is None:
            end = start + timedelta(days=1)
        else:
            end = end.replace(hour=0, minute=0)
            if end <= start:
                end = start + timedelta(days=1)
    elif end is None:
        end = start + timedelta(minutes=a.duration)

    if end <= start:
        print(json.dumps({"ok": False, "error": "結束時間不晚於開始時間"}, ensure_ascii=False))
        return 1

    cals = list_calendars()
    if a.calendar not in cals:
        print(json.dumps({"ok": False, "error": f"沒有這個行事曆: {a.calendar}", "available": cals},
                         ensure_ascii=False))
        return 1

    conflicts = find_conflicts(a.calendar, start, end)

    if a.dry_run:
        print(json.dumps({
            "ok": True, "dry_run": True, "calendar": a.calendar, "title": a.title,
            "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M"),
            "all_day": a.all_day, "conflicts": conflicts,
        }, ensure_ascii=False))
        return 0

    if conflicts and not a.allow_conflict:
        print(json.dumps({
            "ok": False, "error": "時段和既有行程重疊", "conflicts": conflicts,
            "hint": "確認要照建就加 --allow-conflict",
        }, ensure_ascii=False))
        return 2

    uid = create_event(a.calendar, a.title, start, end, a.all_day, a.alarm, a.note)
    log_event(uid, a.calendar, a.title, start, end, a.all_day, a.source_line)

    print(json.dumps({
        "ok": True, "uid": uid, "calendar": a.calendar, "title": a.title,
        "start": start.strftime("%Y-%m-%dT%H:%M"), "end": end.strftime("%Y-%m-%dT%H:%M"),
        "all_day": a.all_day, "conflicts": conflicts,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
