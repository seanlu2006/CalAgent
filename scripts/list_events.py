#!/usr/bin/env python3
"""讀出一段期間內的行事曆事件(唯讀)。

效能注意:Calendar.app 的 AppleScript `whose` 查詢會逐筆掃事件,不走索引。
事件累積多的行事曆(這台機器上是「個人」)單獨查一天就要 ~13 秒,查 7 天要 ~99 秒。
用 --calendar 指名想看的那本可以壓到 1 秒上下。
建立事件時的衝突偵測不受影響(窄時間窗 + 單一行事曆,~0.8 秒)。
"""
import argparse
import subprocess
from datetime import datetime, timedelta

SKIP = {"生日", "台灣節日", "Siri建議", "已排程的提醒事項"}


def _as(dt: datetime, var: str) -> str:
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


def main():
    p = argparse.ArgumentParser(description="列出行事曆事件")
    p.add_argument("--days", type=int, default=1, help="從今天起算幾天,預設 1(今天)")
    p.add_argument("--from", dest="start", help="起始日 YYYY-MM-DD,預設今天")
    p.add_argument("--all-calendars", action="store_true", help="連生日/節日等訂閱行事曆也列")
    p.add_argument("--title", help="只列標題包含這段字的事件(查同名活動的前例用)")
    p.add_argument("--calendar", action="append",
                   help="只查指定行事曆(可重複)。「個人」累積的事件多,單獨查它約 13 秒,"
                        "只看學校/工作約 1 秒——想快就指名。")
    a = p.parse_args()

    start = datetime.strptime(a.start, "%Y-%m-%d") if a.start else datetime.now()
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=a.days)

    # 先取得行事曆清單,在 AppleScript 層就只查要看的那幾本。
    # 不能「撈全部再用 Python 過濾」——「台灣節日」「生日」這類訂閱行事曆動輒上千筆,
    # 全撈會讓 osascript 卡住好幾分鐘。
    names = subprocess.run(
        ["osascript", "-e",
         'with timeout of 300 seconds\ntell application "Calendar" to get name of every calendar\nend timeout'],
        capture_output=True, text=True, timeout=330,
    ).stdout.strip()
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    if a.calendar:
        wanted = [n for n in wanted if n in a.calendar]
    elif not a.all_calendars:
        wanted = [n for n in wanted if n not in SKIP]
    if not wanted:
        print("沒有可讀的行事曆。")
        return 1

    cal_list = ", ".join(f'"{n}"' for n in wanted)
    title_clause = ""
    if a.title:
        safe = a.title.replace("\\", "\\\\").replace('"', '\\"')
        title_clause = f' and summary contains "{safe}"'
    script = f"""
{_as(start, "s")}
{_as(end, "e")}
set output to ""
tell application "Calendar"
  repeat with calName in {{{cal_list}}}
    try
      tell calendar calName
        repeat with ev in (every event whose start date < e and end date > s{title_clause})
          set output to output & calName & "\t" & (summary of ev) & "\t" & ¬
            (start date of ev as string) & "\t" & (end date of ev as string) & linefeed
        end repeat
      end tell
    end try
  end repeat
end tell
return output
"""
    script = f"with timeout of 300 seconds\n{script}\nend timeout"
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=330)
    if r.returncode != 0:
        print("讀取失敗:", r.stderr.strip())
        return 1

    rows = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        if not a.all_calendars and parts[0] in SKIP:
            continue
        rows.append(parts)

    if not rows:
        print(f"{start:%Y-%m-%d} 起 {a.days} 天內沒有行程。")
        return 0

    rows.sort(key=lambda r: r[2])
    print(f"{start:%Y-%m-%d} 起 {a.days} 天,共 {len(rows)} 件：\n")
    for cal, summary, st, en in rows:
        print(f"  [{cal}] {summary}\n        {st}  →  {en}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
