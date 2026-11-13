"""One requested post-intake audit; never edits cards, originals or checkpoints."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from background_intake import safe, sha, read_json, write, instant, SOURCES
from background_schedule import schedule_lock, RunnerBusy
from catchup_watch import native_activity
import intake_state

ROOT = Path(__file__).resolve().parents[1]
ZONE = timezone(timedelta(hours=8))
REQUEST_ID = "post-intake-20261019"
TASK_NAME = "个人情报库-20261019收录复核"
REQUEST_FILE = ".intake/post-intake-audit/request.json"
STATE_FILE = ".intake/post-intake-audit/state.json"
RESULT_FILE = "evidence/post-intake-audit/result.json"
PS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def now():
    return datetime.now(ZONE)


def boot_epoch():
    import ctypes
    tick = ctypes.windll.kernel32.GetTickCount64
    tick.restype = ctypes.c_ulonglong
    return time.time() - tick() / 1000


class AuditWait(Exception):
    pass


@contextmanager
def audit_lock(root):
    import msvcrt
    path = safe(root, ".intake/post-intake-audit/audit.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise AuditWait("audit_already_running") from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def request_config(root):
    config = read_json(safe(root, REQUEST_FILE))
    if (config.get("schema_version") != 1 or config.get("id") != REQUEST_ID
            or config.get("target") != "2026-10-19T09:00:00+08:00"
            or config.get("not_before") != "2026-10-19T09:05:00+08:00"
            or config.get("problem_after") != "2026-10-19T09:45:00+08:00"):
        raise ValueError("Unexpected one-time audit request")
    return config


def evidence(root, value):
    write(safe(root, RESULT_FILE), {"request_id": REQUEST_ID, "checked_at": now().isoformat(), **value})


def choose_result(root, config, stamp):
    state_path = safe(root, STATE_FILE)
    saved = read_json(state_path) if state_path.exists() else {}
    if saved.get("source_result"):
        return saved["source_result"]
    activity = native_activity(read_json(safe(root, "data/catchup-config.json")))
    if activity.get("busy"):
        if stamp >= instant(config["problem_after"]):
            raise ValueError("原收录任务仍在运行，超过本次等待时限仍未能开始独立复核；不会打断原任务。")
        raise AuditWait("daily_task_still_running")
    if not activity.get("available"):
        if stamp < instant(config["problem_after"]):
            raise AuditWait("daily_task_unavailable")
        raise ValueError("原收录任务已暂停、计划改变或不可用，未能确认明早目标完整收录。")
    path = safe(root, "evidence/background-latest.json")
    result = read_json(path) if path.exists() else None
    if result and instant(result["scan_end"]) >= instant(config["target"]):
        if result.get("status") != "complete":
            raise ValueError("本轮收录未完成六类；失败类别的断点仍保留。")
        relative = f"evidence/daily/{result['run_id']}/result.json"
        if read_json(safe(root, relative)) != result:
            raise ValueError("最新结果与持久收录结果不一致。")
        return relative
    runtime_path = safe(root, ".intake/background/runtime.json")
    runtime = read_json(runtime_path) if runtime_path.exists() else {}
    if (runtime.get("phase") == "failed" and runtime.get("requested_at")
            and instant(runtime["requested_at"]) >= instant(config["target"])):
        raise ValueError("明早收录执行失败，请查看 .intake/background/runtime.json。")
    if stamp >= instant(config["problem_after"]):
        raise ValueError("尚无覆盖北京时间 2026-10-19 09:00 的六类完整收录结果。")
    raise AuditWait("waiting_for_complete_intake")


def capture_again(root, result, *, problem_after="2026-10-19T09:45:00+08:00"):
    """Fixed same-window read, under the shared collector lock; no archive writes."""
    from extension_native_host import locked as host_locked
    # Deliberately not 'bg-': the intake runner must never resume this
    # read-only audit as if it were a new production intake.
    request_id = "audit-recheck-20261019-" + sha(result["run_id"].encode())[:12]
    run_id = "extension-probe-" + sha(request_id.encode())[:24]
    run_dir = safe(root, "evidence/extension-probe/" + run_id)
    path = safe(root, ".intake/extension-probe/config.json")
    previous = read_json(path)
    expected = {(c, task, cid) for c, (task, cid) in SOURCES.items()}
    actual = {(s["category_id"], s["task_name"], s["conversation_id"]) for s in previous["sources"]}
    if actual != expected or previous.get("extension_id") != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":
        raise ValueError("固定后台扩展或六类来源配置改变，不能复核。")
    if previous.get("enabled") is not True:
        raise ValueError("后台扩展读取已停用，不能完成独立复核。")
    if previous["request_id"] != request_id:
        previous_id = "extension-probe-" + sha(previous["request_id"].encode())[:24]
        prior_path = safe(root, "evidence/extension-probe/" + previous_id + "/run.json")
        if not prior_path.exists() or read_json(prior_path).get("status") not in {"complete", "partial"}:
            if now() >= instant(problem_after):
                raise ValueError("另一个后台读取仍未结束，独立复核未能开始；未改动收录资料。")
            raise AuditWait("another_reader_pending")
        with host_locked(root):
            if read_json(path) != previous:
                raise AuditWait("reader_configuration_changed")
            write(path, {"extension_id": previous["extension_id"], "enabled": True, "mode": "probe",
                  "request_id": request_id, "scan_start": result["scan_start"],
                  "scan_end": result["scan_end"], "sources": previous["sources"]})
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        if read_json(path)["request_id"] != request_id:
            raise ValueError("独立复核读取计划被替换，不能确认完整性。")
        report = run_dir / "run.json"
        if report.exists() and read_json(report)["status"] in {"complete", "partial"}:
            return run_dir.relative_to(Path(root).resolve()).as_posix()
        time.sleep(2)
    raise ValueError("后台重读等待 15 分钟仍未完成，请检查普通 Chrome 登录和扩展。")


def notify_problem(root):
    process = subprocess.run([str(PS), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-File", str(safe(root, "scripts/notify_intake_audit.ps1")),
                              "-ResultFile", str(safe(root, RESULT_FILE))],
                             capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if process.returncode:
        return {"submitted": False, "error": process.stderr.decode("utf-8", errors="replace")[-500:]}
    try:
        return json.loads(process.stdout.decode("utf-8-sig"))
    except ValueError:
        return {"submitted": False, "error": "notification_response_unreadable"}


def disable_own_task():
    # Literal task name only; never affects the daily intake or its catchup task.
    process = subprocess.run([str(PS), "-NoProfile", "-NonInteractive", "-Command",
        "Disable-ScheduledTask -TaskPath '\\' -TaskName '个人情报库-20261019收录复核' -ErrorAction Stop | Out-Null"],
        capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    return process.returncode == 0


def finish_notification(root, state):
    if state.get("task_disabled") is True:
        return state
    if state["status"] == "pass":
        state["notification"] = {"submitted": False, "reason": "success_stays_quiet"}
    elif not state.get("notification", {}).get("submitted"):
        if state.get("notification_retry_at") and now() < instant(state["notification_retry_at"]):
            return state
        try:
            state["notification"] = notify_problem(root)
        except Exception as error:
            state["notification"] = {"submitted": False, "error": str(error)[:300]}
        state["notification_retry_at"] = (now() + timedelta(minutes=15)).isoformat()
    write(safe(root, STATE_FILE), state)
    # A completed successful audit never produces a toast. A failed delivery
    # retains its evidence and retries the same tag, never a new popup.
    if state["status"] == "pass" or state.get("notification", {}).get("submitted"):
        state["task_disabled"] = disable_own_task()
        write(safe(root, STATE_FILE), state)
    return state


def run_once(root=ROOT, *, dry_run=False):
    root = Path(root).resolve()
    config = request_config(root)
    stamp = now()
    if stamp < instant(config["not_before"]):
        return {"status": "waiting", "reason": "not_due", "not_before": config["not_before"]}
    if dry_run:
        return {"status": "waiting", "reason": "dry_run_no_read_or_notification", "target": config["target"]}
    try:
        with audit_lock(root):
            path = safe(root, STATE_FILE)
            state = read_json(path) if path.exists() else {"request_id": REQUEST_ID, "status": "pending"}
            if state.get("request_id") != REQUEST_ID:
                raise ValueError("Unexpected audit state; refusing reset")
            if state["status"] in {"pass", "issues", "failed"}:
                return finish_notification(root, state)
            boot = boot_epoch()
            if not state.get("first_due_check_at") or abs(state.get("boot_epoch", boot) - boot) > 5:
                state.update(first_due_check_at=stamp.isoformat(), boot_epoch=boot)
            # A late login/reboot must have time to finish missed intake; do not
            # call an ordinary 10:00 boot a 09:45 timeout before it can catch up.
            config = {**config, "problem_after": max(instant(config["problem_after"]),
                      instant(state["first_due_check_at"]) + timedelta(minutes=40)).isoformat()}
            state["effective_problem_after"] = config["problem_after"]
            write(path, state)
            try:
                with schedule_lock(root):
                    relative = choose_result(root, config, stamp)
                    state.update(status="checking", source_result=relative, started_at=state.get("started_at", stamp.isoformat()))
                    write(path, state)
                    from intake_audit_checks import validate_intake
                    with intake_state.locked(root):
                        initial = validate_intake(root, relative)
                    if initial["status"] == "pass":
                        recheck = capture_again(root, read_json(safe(root, relative)), problem_after=config["problem_after"])
                        with intake_state.locked(root):
                            checked = validate_intake(root, relative, recheck_run_dir=recheck)
                        checked["recheck_run_dir"] = recheck
                    else:
                        checked = initial
                    evidence(root, {**checked, "source_result": relative, "target": config["target"]})
                    state.update(status=checked["status"], result_file=RESULT_FILE, finished_at=now().isoformat())
                    write(path, state)
            except (RunnerBusy, AuditWait, BlockingIOError) as error:
                state.update(last_wait_at=now().isoformat(), wait_reason=str(error) or "collector_or_ledger_busy")
                if now() >= instant(config["problem_after"]):
                    evidence(root, {"status": "issues", "target": config["target"], "issues": [
                        {"code": "audit_blocked", "message": "超过本次等待时限，收录或读取仍被占用，未能完成独立复核；没有抢占或中止原任务。"}]})
                    state.update(status="issues", result_file=RESULT_FILE, finished_at=now().isoformat())
                    write(path, state)
                    return finish_notification(root, state)
                write(path, state)
                return {"status": "waiting", "reason": state["wait_reason"]}
            except Exception as error:
                evidence(root, {"status": "issues", "target": config["target"],
                         "issues": [{"code": "audit_incomplete", "message": str(error)[:800]}]})
                state.update(status="issues", result_file=RESULT_FILE, finished_at=now().isoformat())
                write(path, state)
            return finish_notification(root, state)
    except AuditWait as error:
        return {"status": "waiting", "reason": str(error)}


def main():
    parser = argparse.ArgumentParser(description="2026-10-19 收录后的单次后台复核")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run_once(dry_run=args.dry_run)
    if sys.stdout:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"waiting", "pass"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
