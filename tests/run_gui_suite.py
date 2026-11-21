#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_gui_suite.py —— GUI 探针并行跑批器（多开 VM）。

背景：GUI 探针以前只能串行单发，理由有两个——① 所有探针共用仓库根的
os-image.bin / disk.vhd，Windows 上第二个 QEMU 直接因文件锁起不来；
② 端口写死互相踩。这两点仓库里已有解法（tests/ezos_qemu.py 的 EZOS_SLOT
机制：槽位独占端口段 + temp/slot<N>/ 私有镜像副本 + 串口日志改名），
本脚本就是把它用起来：**每个探针进程注入不同 EZOS_SLOT，并行跑**。

注意：slot 0 会被 ezos_qemu.kill_stale_qemu 用来 taskkill 全体 QEMU，
所以并行跑批**绝不使用 slot 0**（默认从 10 起）。

用法：
    python tests/run_gui_suite.py                 # 跑全部登记的 GUI 探针
    python tests/run_gui_suite.py --only clickloss,apps
    python tests/run_gui_suite.py --base 20        # 换一批槽位
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 登记要并行跑的 GUI 探针：名字 -> 脚本
SUITE = [
    ("clickloss", "probe_clickloss.py"),
    ("apps", "probe_apps.py"),
    ("fuzz", "probe_fuzz.py"),
    ("combo2", "probe_combo2.py"),
]

LOG_DIR = os.path.join(ROOT, "temp", "suite")
TIMEOUT = 1800          # 单个探针上限（秒）


def run_one(name, script, slot):
    env = dict(os.environ)
    env["EZOS_SLOT"] = str(slot)
    log_path = os.path.join(LOG_DIR, "%s.log" % name)
    t0 = time.time()
    with open(log_path, "wb") as fh:
        try:
            p = subprocess.Popen([sys.executable, os.path.join(HERE, script)],
                                 cwd=ROOT, env=env, stdout=fh,
                                 stderr=subprocess.STDOUT)
            rc = p.wait(TIMEOUT)
            if rc is None:
                p.kill()
                rc = -9
        except Exception as e:                       # noqa: BLE001
            fh.write(("spawn failed: %r\n" % e).encode())
            rc = -1
    return rc, time.time() - t0, log_path


def summarize(log_path):
    """从探针日志里抓 PASS/FAIL 计数（探针结尾都会打这行）"""
    ok = bad = 0
    tail = ""
    try:
        with open(log_path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("PASS"):
                    ok += 1
                elif line.startswith("FAIL"):
                    bad += 1
                if line.startswith("====="):
                    tail = line
    except OSError:
        pass
    return ok, bad, tail


def main():
    base = 10
    only = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--base":
            base = int(args[i + 1])
            i += 2
        elif args[i] == "--only":
            only = set(args[i + 1].split(","))
            i += 2
        else:
            i += 1

    items = [(n, s) for n, s in SUITE if not only or n in only]
    if not items:
        print("no probe selected")
        return 2

    os.makedirs(LOG_DIR, exist_ok=True)
    print("=== 并行跑批：%d 个探针，slot %d..%d ===" %
          (len(items), base, base + len(items) - 1), flush=True)

    procs = []
    for idx, (name, script) in enumerate(items):
        slot = base + idx
        env = dict(os.environ)
        env["EZOS_SLOT"] = str(slot)
        log_path = os.path.join(LOG_DIR, "%s.log" % name)
        fh = open(log_path, "wb")
        p = subprocess.Popen([sys.executable, os.path.join(HERE, script)],
                             cwd=ROOT, env=env, stdout=fh,
                             stderr=subprocess.STDOUT)
        procs.append((name, slot, script, p, fh, log_path, time.time()))
        print("  启动 %-10s slot=%-3d log=%s" % (name, slot, log_path),
              flush=True)

    print("\n=== 等待完成 ===", flush=True)
    results = []
    for name, slot, script, p, fh, log_path, t0 in procs:
        try:
            rc = p.wait(TIMEOUT)
            if rc is None:
                p.kill()
                rc = -9
        except Exception:
            p.kill()
            rc = -1
        finally:
            fh.close()
        ok, bad, tail = summarize(log_path)
        results.append((name, slot, rc, time.time() - t0, ok, bad, tail))

    print("\n===== GUI 套件汇总 =====")
    bad = 0
    for name, slot, rc, dt, ok, fbad, tail in results:
        status = "PASS" if rc == 0 else "FAIL"
        if rc != 0:
            bad += 1
        print("%-5s %-10s slot=%-3d %6.1fs  pass=%-3d fail=%-3d rc=%s %s" %
              (status, name, slot, dt, ok, fbad, rc, tail))
    print("===== %d/%d 通过 =====" % (len(results) - bad, len(results)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
