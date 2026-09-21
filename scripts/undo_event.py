#!/usr/bin/env python3
"""撤銷 CalAgent 建立過的事件。

只動 data/event_log.csv 裡有記錄的事件——也就是這個 agent 自己建的。
不在 log 裡的事件(Sean 自己建的、別的來源同步進來的)一律不碰。
"""
import argparse
import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENT_LOG = ROOT / "data" / "event_log.csv"


def run_osascript(script: str, timeout_sec: int = 300) -> str:
    # 冷啟動的 Calendar.app 可能超過 AppleEvent 預設的 60 秒,見 create_event.py
    wrapped = f"with timeout of {timeout_sec} seconds\n{script}\nend timeout"
    r = subprocess.run(["osascript", "-e", wrapped], capture_output=True, text=True,
                       timeout=timeout_sec + 30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def load_log() -> list[dict]:
    if not EVENT_LOG.exists():
        return []
    with EVENT_LOG.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_log(rows: list[dict]):
    from create_event import LOG_HEADER
    with EVENT_LOG.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_HEADER)
        w.writeheader()
        w.writerows(rows)


def delete_by_uid(calendar: str, uid: str) -> bool:
    script = f'''
tell application "Calendar"
  tell calendar "{calendar}"
    set matches to (every event whose uid is "{uid}")
    set n to count of matches
    delete matches
  end tell
end tell
return n
'''
    return int(run_osascript(script) or 0) > 0


def main():
    p = argparse.ArgumentParser(description="撤銷 CalAgent 建立過的事件")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--uid", action="append", help="要撤銷的事件 uid(可重複)")
    g.add_argument("--last", type=int, help="撤銷最近 N 筆")
    g.add_argument("--all", action="store_true", help="撤銷 log 裡全部(危險,需加 --yes)")
    p.add_argument("--yes", action="store_true", help="跳過確認")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    rows = load_log()
    if not rows:
        print("event_log.csv 是空的,沒有可撤銷的事件。")
        return 0

    if a.uid:
        targets = [r for r in rows if r["uid"] in a.uid]
        missing = set(a.uid) - {r["uid"] for r in targets}
        if missing:
            print(f"⚠️ 這些 uid 不在 log 裡,不會處理:{', '.join(missing)}")
    elif a.last:
        targets = rows[-a.last:]
    else:
        targets = list(rows)

    if not targets:
        print("沒有符合的事件。")
        return 1

    print(f"要撤銷 {len(targets)} 件：")
    for r in targets:
        print(f"  [{r['calendar']}] {r['title']}  {r['start']} → {r['end']}")

    if a.dry_run:
        print("\n(dry-run,沒有實際刪除)")
        return 0

    if a.all and not a.yes:
        print("\n--all 需要同時加 --yes 才會執行。")
        return 1

    deleted, failed = [], []
    for r in targets:
        try:
            if delete_by_uid(r["calendar"], r["uid"]):
                deleted.append(r)
            else:
                failed.append((r, "行事曆裡找不到這個 uid(可能已被手動刪除)"))
        except RuntimeError as e:
            failed.append((r, str(e)))

    kept = [r for r in rows if r not in deleted]
    save_log(kept)

    print(f"\n✅ 已撤銷 {len(deleted)} 件,event_log 同步更新。")
    for r, why in failed:
        print(f"⚠️ 沒刪成:{r['title']} — {why}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
