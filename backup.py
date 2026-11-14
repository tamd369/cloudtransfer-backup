# -*- coding: utf-8 -*-
"""源目录 -> 目标远端 整份备份(增量, 可反复重跑)

配置来自 Secret BACKUP_JSON:  {"src":"gd6x:out4","dst":"onedrive2:out4"}
特性:
  - rclone copy 天然增量: 已存在且一致的文件自动跳过, 中断后重跑接着传
  - 单轮限时(--max-duration), 跑不完会自动"自接力"再起一轮, 直到 dst 追平 src
  - 幂等: 重复触发不会重复传

用法:
  python3 backup.py                     # 备份一轮(默认限时 300m)
  python3 backup.py --max-duration 120m
  python3 backup.py --status            # 只统计两端, 不传文件
  python3 backup.py --no-relay          # 本轮结束不自接力
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

WF = "backup.yml"
API = "https://api.github.com"


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def cfg():
    raw = os.environ.get("BACKUP_JSON", "")
    if raw:
        try:
            d = json.loads(raw)
            if d.get("src") and d.get("dst"):
                return d["src"], d["dst"]
        except Exception:
            pass
    return os.environ.get("BACKUP_SRC", ""), os.environ.get("BACKUP_DST", "")


def size(spec, allow_missing=False):
    """rclone size --json -> (文件数, 字节)。
    allow_missing=True 时, 目标目录还不存在视为 (0,0) 而不是报错。"""
    p = subprocess.run(["rclone", "size", spec, "--json", "--retries", "3"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        err = p.stderr or ""
        if allow_missing and ("not found" in err.lower()):
            return 0, 0
        raise RuntimeError("size 失败(%s): %s" % (spec, err[-150:]))
    d = json.loads(p.stdout)
    return int(d.get("count", 0)), int(d.get("bytes", 0))


def copy(src, dst, max_duration):
    cmd = ["rclone", "copy", src, dst,
           "--transfers", "8", "--checkers", "8",
           "--drive-chunk-size", "128M", "--drive-multi-thread-streams", "4",
           "--drive-acknowledge-abuse",
           "--onedrive-chunk-size", "100M",
           "--max-duration", max_duration,
           "--stats", "60s", "--stats-one-line",
           "--retries", "3", "--low-level-retries", "10"]
    log("开始备份(%s -> %s, 限时 %s)" % ("SRC", "DST", max_duration))
    p = subprocess.run(cmd, text=True)
    return p.returncode


def tg(text):
    tok = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    if not tok or not chat:
        return False
    import urllib.parse
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3900],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok,
                                 data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            return bool(json.loads(x.read().decode("utf-8", "replace")).get("ok"))
    except Exception as e:
        log("!! 发 TG 失败: %s" % str(e)[:100])
        return False


def relay():
    """还有没传完的 -> 再派发一轮自己"""
    tok = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not tok or not repo:
        log("!! 缺 GH_TOKEN/GITHUB_REPOSITORY, 无法自接力")
        return False
    body = json.dumps({"ref": "main", "inputs": {}}).encode()
    req = urllib.request.Request("%s/repos/%s/actions/workflows/%s/dispatches" % (API, repo, WF),
                                 data=body, method="POST",
                                 headers={"Authorization": "token " + tok, "User-Agent": "bk-relay",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            log("已派发下一轮 (HTTP %s)" % x.status)
            return True
    except urllib.error.HTTPError as e:
        log("!! 派发失败 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:150]))
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-duration", default="300m")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-relay", action="store_true")
    a = ap.parse_args()

    src, dst = cfg()
    if not src or not dst:
        log("!! 缺 BACKUP_JSON(src/dst)")
        return 1
    log("任务已载入(src/dst 来自 Secret, 不回显)")

    sc, sb = size(src)
    log("源: 文件 %d, 合计 %.2f GB" % (sc, sb / 1073741824.0))

    if a.status:
        dc, db = size(dst, allow_missing=True)
        log("目标: 文件 %d, 合计 %.2f GB" % (dc, db / 1073741824.0))
        log("进度: %.2f%%  (缺 %d 个文件)" % (
            (db * 100.0 / sb) if sb else 100.0, max(0, sc - dc)))
        return 0

    rc = copy(src, dst, a.max_duration)
    dc, db = size(dst, allow_missing=True)
    pct = (db * 100.0 / sb) if sb else 100.0
    done = (dc >= sc) and (db >= sb)
    log("--- 本轮结束 ---")
    log("源 %d 个 / %.2f GB ; 目标 %d 个 / %.2f GB ; 进度 %.2f%%  rc=%d" % (
        sc, sb / 1073741824.0, dc, db / 1073741824.0, pct, rc))

    if done:
        log("备份完成 ✅")
        tg("☁️ 备份完成\n文件 %d 个 / %.2f GB\n进度 100%%" % (dc, db / 1073741824.0))
        return 0

    log("尚未完成, 准备自接力")
    if not a.no_relay:
        relay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
