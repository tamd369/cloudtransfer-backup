# -*- coding: utf-8 -*-
"""源目录 -> 目标远端 整份拷贝/备份(增量, 可反复重跑)

两种配置来源(优先 JOBS_JSON):
  JOBS_JSON = {"jobs": {"<任务名>": {"src":"gd6x:out5", "dst":"gd2:out5"}, ...}}
      用 --job <任务名> 选一个
  BACKUP_JSON = {"src":"...","dst":"..."}     (老格式, 不带 --job 时用)

特性:
  - rclone copy 天然增量: 已存在且一致的文件自动跳过, 中断后重跑接着传
  - 单轮限时(--max-duration), 跑不完自动"自接力"再起一轮(RELAY_WF 指定派发哪个 workflow)
  - 防死循环: rclone 报错且本轮零进展 -> 绝不自接力, 直接失败

用法:
  python3 backup.py                            # 老格式(BACKUP_JSON)
  python3 backup.py --job out5-to-gd2          # 按任务名(JOBS_JSON)
  python3 backup.py --job X --status           # 只统计两端
  python3 backup.py --job X --max-duration 120m --no-relay
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def log(m):
    print("%s %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def cfg(job):
    """返回 (src, dst, 说明)"""
    if job:
        raw = os.environ.get("JOBS_JSON", "")
        if not raw:
            raise SystemExit("!! 给了 --job 但缺 JOBS_JSON")
        data = json.loads(raw)
        j = (data.get("jobs") or {}).get(job)
        if not j:
            raise SystemExit("!! JOBS_JSON 里没有任务 %r (可用: %s)" % (
                job, ", ".join(sorted((data.get("jobs") or {}).keys()))))
        return j.get("src", ""), j.get("dst", ""), job
    raw = os.environ.get("BACKUP_JSON", "")
    if raw:
        try:
            d = json.loads(raw)
            if d.get("src") and d.get("dst"):
                return d["src"], d["dst"], "BACKUP_JSON"
        except Exception:
            pass
    return os.environ.get("BACKUP_SRC", ""), os.environ.get("BACKUP_DST", ""), "env"


def size(spec, allow_missing=False):
    """rclone size --json -> (文件数, 字节)。allow_missing 时目标不存在视为 (0,0)。"""
    p = subprocess.run(["rclone", "size", spec, "--json", "--retries", "3"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        err = p.stderr or ""
        if allow_missing and ("not found" in err.lower() or "directory not found" in err.lower()):
            return 0, 0
        raise RuntimeError("size 失败(%s): %s" % (spec, err[-150:]))
    d = json.loads(p.stdout)
    return int(d.get("count", 0)), int(d.get("bytes", 0))


def copy(src, dst, max_duration):
    cmd = ["rclone", "copy", src, dst,
           "--transfers", "8", "--checkers", "8",
           "--drive-chunk-size", "128M",
           "--multi-thread-streams", "4", "--multi-thread-cutoff", "100M",
           "--drive-acknowledge-abuse",
           "--onedrive-chunk-size", "100M",
           "--max-duration", max_duration,
           "--stats", "60s", "--stats-one-line",
           "--retries", "3", "--low-level-retries", "10"]
    log("开始拷贝(限时 %s)" % max_duration)
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
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            return bool(json.loads(x.read().decode("utf-8", "replace")).get("ok"))
    except Exception as e:
        log("!! 发 TG 失败: %s" % str(e)[:100])
        return False


def relay(wf, inputs):
    tok = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not tok or not repo:
        log("!! 缺 GH_TOKEN/GITHUB_REPOSITORY, 无法自接力")
        return False
    body = json.dumps({"ref": "main", "inputs": inputs or {}}).encode()
    req = urllib.request.Request("%s/repos/%s/actions/workflows/%s/dispatches" % (API, repo, wf),
                                 data=body, method="POST",
                                 headers={"Authorization": "token " + tok, "User-Agent": "bk-relay",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            log("已派发下一轮 %s (HTTP %s)" % (wf, x.status))
            return True
    except urllib.error.HTTPError as e:
        log("!! 派发失败 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:150]))
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", default="", help="JOBS_JSON 里的任务名")
    ap.add_argument("--max-duration", default="300m")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-relay", action="store_true")
    a = ap.parse_args()

    src, dst, tag = cfg(a.job)
    if not src or not dst:
        log("!! 缺 src/dst (检查 JOBS_JSON / BACKUP_JSON)")
        return 1
    log("任务 = %s" % tag)

    sc, sb = size(src)
    log("源: 文件 %d, 合计 %.2f GB" % (sc, sb / 1073741824.0))

    if a.status:
        dc, db = size(dst, allow_missing=True)
        log("目标: 文件 %d, 合计 %.2f GB" % (dc, db / 1073741824.0))
        log("进度: %.2f%%  (缺 %d 个文件)" % (
            (db * 100.0 / sb) if sb else 100.0, max(0, sc - dc)))
        return 0

    dc0, db0 = size(dst, allow_missing=True)
    rc = copy(src, dst, a.max_duration)
    dc, db = size(dst, allow_missing=True)
    pct = (db * 100.0 / sb) if sb else 100.0
    done = (dc >= sc) and (db >= sb)
    progressed = (db > db0) or (dc > dc0)
    log("--- 本轮结束 ---")
    log("源 %d 个 / %.2f GB ; 目标 %d 个 / %.2f GB ; 进度 %.2f%%  rc=%d" % (
        sc, sb / 1073741824.0, dc, db / 1073741824.0, pct, rc))
    log("本轮新增 %.2f GB" % ((db - db0) / 1073741824.0))

    if done:
        log("拷贝完成 ✅")
        tg("☁️ 拷贝完成 [%s]\n文件 %d 个 / %.2f GB" % (tag, dc, db / 1073741824.0))
        return 0

    # 防死循环: rclone 报错且零进展 -> 不自接力
    if rc != 0 and not progressed:
        log("!! 本轮零进展且 rclone 返回 %d —— 不自接力, 直接失败以免死循环" % rc)
        tg("❌ 拷贝失败 [%s]: rclone rc=%d 且零进展, 已停止自接力" % (tag, rc))
        return 1

    log("尚未完成, 准备自接力")
    if not a.no_relay:
        wf = os.environ.get("RELAY_WF", "backup.yml")
        inputs = {"job": a.job} if a.job else {}
        relay(wf, inputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
