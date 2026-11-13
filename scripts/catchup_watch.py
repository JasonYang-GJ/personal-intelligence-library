"""Hidden, local catch-up host. Launches the existing native task; never archives data."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib

from catchup_policy import BEIJING, CATEGORIES, decide

ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_ID = "example-personal-library-intake"
THREAD_ID = "00000000-0000-4000-8000-000000000002"
EXPECTED_RULE = "RRULE:FREQ=WEEKLY;BYHOUR=9;BYMINUTE=0;BYDAY=SU,MO,TU,WE,TH,FR,SA"


def now_local():
    return datetime.now(BEIJING)


def stamp(value):
    return value.astimezone(BEIJING).isoformat(timespec="seconds")


def parse_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp has no timezone")
    return parsed.astimezone(BEIJING)


def read_json(path):
    if path.is_symlink():
        raise ValueError("Refusing a symlink state file")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing a symlink state file")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as f:
        temp = Path(f.name)
        f.write((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def host_lock(root):
    """OS-owned byte lock is released on process exit, including crashes."""
    import msvcrt
    path = root / "data/catchup-watch.lock"
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def load_runtime(root):
    path = root / "data/catchup-runtime.json"
    if not path.exists():
        return {"schema_version": 1, "phase": "idle", "trigger_count": 0}
    value = read_json(path)
    if value.get("schema_version") != 1 or value.get("phase") not in {"idle", "starting", "running", "failed"}:
        raise ValueError("Invalid catch-up state; not resetting")
    return value


def native_activity(config):
    """Read only this native task's metadata and most recent lifecycle event."""
    automation = tomllib.loads(Path(config["automation_file"]).read_text(encoding="utf-8"))
    if automation.get("id") != AUTOMATION_ID or automation.get("target_thread_id") != THREAD_ID:
        raise ValueError("Native task identity mismatch")
    if automation.get("rrule") != EXPECTED_RULE or automation.get("status") != "ACTIVE":
        return {"available": False, "reason": "native_task_paused_or_schedule_changed", "busy": False}
    db = Path(config["state_database"])
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=3) as connection:
        row = connection.execute("SELECT rollout_path, archived FROM threads WHERE id=?", (THREAD_ID,)).fetchone()
    if not row or row[1]:
        return {"available": False, "reason": "native_thread_missing_or_archived", "busy": False}
    activity = last_activity(Path(row[0]))
    return {"available": True, "rollout_path": row[0], **activity}


def last_activity(path):
    # Large transcripts grow daily; no scan of other chats or internal reasoning.
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        offset = max(0, size - 8 * 1024 * 1024)
        stream.seek(offset)
        if offset:
            stream.readline()
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        payload = event.get("payload") or {}
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind in {"task_started", "task_complete", "task_aborted"}:
            return {"busy": kind == "task_started", "event": kind, "at": event["timestamp"],
                    "turn_id": payload.get("root_turn_id") or payload.get("turn_id")}
    raise ValueError("Native lifecycle unknown; refusing to trigger")


def verification_request(root, runtime, now):
    path = root / "data/catchup-verification-request.json"
    if not path.exists():
        return None
    request = read_json(path)
    if request.get("schema_version") != 1 or request.get("purpose") != "user_authorized_integration_verification":
        raise ValueError("Invalid one-off verification request")
    if request.get("id") == runtime.get("verification_completed_id"):
        return None
    target = parse_time(request["target"])
    expires = parse_time(request["expires_at"])
    if expires <= now:
        return None
    if target > now or expires - target > timedelta(hours=2) or not request.get("id"):
        raise ValueError("Invalid verification interval")
    return request


def reconcile(runtime, activity, now):
    """Resolve uncertain launches only using native lifecycle evidence."""
    result = dict(runtime)
    if result["phase"] not in {"starting", "running"}:
        return result
    requested = parse_time(result["requested_at"])
    if activity["busy"]:
        result.update(phase="running", observed_turn_id=activity.get("turn_id"), observed_start=activity["at"])
    elif parse_time(activity["at"]) >= requested:
        # Coverage is evaluated next; an ended run without coverage stays retryable.
        result.update(phase="failed", failed_at=stamp(now), last_failure="native_run_ended_awaiting_coverage_check")
    elif result["phase"] == "running":
        # Do not guess that an unknown or older lifecycle means the run has ended.
        raise ValueError("Native lifecycle went backwards; refusing another launch")
    return result


def helper(root, config, probe):
    command = [config["pwsh"], "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden", "-File",
               str(root / "scripts/trigger_intake_task.ps1"), "-Probe" if probe else "-RunNow"]
    completed = subprocess.run(command, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, encoding="utf-8-sig", errors="replace", timeout=45,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        result = json.loads(completed.stdout.strip())
    except ValueError as error:
        raise ValueError(f"UI helper returned no valid JSON (exit {completed.returncode})") from error
    if not isinstance(result, dict) or "status" not in result or "triggered" not in result:
        raise ValueError("UI helper result schema mismatch")
    return result


def report(root, runtime, result, *, save=True):
    result = {"checked_at": stamp(now_local()), "phase": runtime["phase"],
              "trigger_count": runtime.get("trigger_count", 0), **result}
    if save:
        signature = json.dumps({k: result.get(k) for k in ("action", "reason", "phase", "due_at", "trigger_count")}, sort_keys=True)
        if runtime.get("last_event_signature") != signature:
            directory = root / "evidence/catchup"
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            runtime["last_event_signature"] = signature
        write_json(root / "data/catchup-runtime.json", runtime)
        write_json(root / "evidence/catchup/latest.json", result)
    return result


def run_once(root=ROOT, *, dry_run=False):
    root = Path(root)
    config = read_json(root / "data/catchup-config.json")
    if config.get("schema_version") != 1 or not config.get("enabled"):
        return {"action": "wait", "reason": "catchup_disabled"}
    runtime = load_runtime(root)
    now = now_local()
    activity = native_activity(config)
    if not activity["available"]:
        return report(root, runtime, {"action": "wait", "reason": activity["reason"]}, save=not dry_run)
    runtime = reconcile(runtime, activity, now)
    ledger = read_json(root / "data/intake-state.json")
    if ledger.get("schema_version") != 1:
        raise ValueError("Invalid intake ledger")
    checkpoints = ledger["tasks"]
    request = verification_request(root, runtime, now)
    target = request["target"] if request else None
    decision = decide(now, checkpoints, runtime, app_ready=True, native_busy=activity["busy"], verification_target=target)
    if decision["action"] == "covered":
        if not activity["busy"]:
            runtime.update(phase="idle", last_coverage_verified_at=stamp(now), last_covered_target=decision["due_at"])
            runtime.pop("failed_at", None)
            runtime.pop("last_failure", None)
            if request:
                runtime.update(verification_completed_id=request["id"], verification_completed_at=stamp(now))
        return report(root, runtime, {**decision, "native_activity": activity}, save=not dry_run)
    if decision["action"] != "trigger":
        return report(root, runtime, {**decision, "native_activity": activity}, save=not dry_run)
    probe = helper(root, config, True)
    ready = bool(probe.get("ok")) and not probe.get("triggered")
    decision = decide(now_local(), checkpoints, runtime, app_ready=ready, native_busy=activity["busy"], verification_target=target)
    if decision["action"] != "trigger" or dry_run:
        return report(root, runtime, {**decision, "dry_run": dry_run, "probe": probe}, save=not dry_run)
    # Re-read native activity immediately before reserving and triggering.
    activity = native_activity(config)
    if not activity["available"] or activity["busy"]:
        return report(root, runtime, {"action": "wait", "reason": "native_task_busy_or_changed", "native_activity": activity})
    # Probe/navigation preparation can overlap the completion of the daily run.
    # Its successful checkpoints, not the earlier snapshot, decide whether to launch.
    runtime = reconcile(runtime, activity, now_local())
    ledger = read_json(root / "data/intake-state.json")
    if ledger.get("schema_version") != 1:
        raise ValueError("Invalid intake ledger")
    request = verification_request(root, runtime, now_local())
    target = request["target"] if request else None
    decision = decide(now_local(), ledger["tasks"], runtime, app_ready=ready,
                      native_busy=activity["busy"], verification_target=target)
    if decision["action"] != "trigger":
        return report(root, runtime, {**decision, "native_activity": activity})
    runtime.update(phase="starting", requested_at=stamp(now_local()), target=decision["due_at"],
                   trigger_count=runtime.get("trigger_count", 0) + 1,
                   verification_request_id=request["id"] if request else None)
    runtime.pop("failed_at", None)
    write_json(root / "data/catchup-runtime.json", runtime)
    try:
        triggered = helper(root, config, False)
        runtime["last_ui_result"] = triggered
        for _ in range(10):
            activity = native_activity(config)
            if activity.get("busy"):
                runtime = reconcile(runtime, activity, now_local())
                break
            if not triggered.get("triggered"):
                break
            time.sleep(2)
        if runtime["phase"] != "running" and not triggered.get("triggered"):
            runtime.update(phase="failed", failed_at=stamp(now_local()), last_failure=triggered.get("reason", triggered["status"]))
        result = {**decision, "reason": "native_start_confirmed" if runtime["phase"] == "running" else "launch_not_yet_confirmed",
                  "ui_result": triggered, "native_activity": activity}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Keep starting, because a timed-out helper may already have clicked.
        runtime["last_failure"] = str(error)
        result = {"action": "wait", "reason": "trigger_result_uncertain", "detail": str(error)}
    return report(root, runtime, result)


def main():
    parser = argparse.ArgumentParser(description="个人情报库隐藏补收检查器；不修改报告或原生9点计划")
    parser.add_argument("--dry-run", action="store_true", help="只读判断，不触发、不写运行状态")
    args = parser.parse_args()
    try:
        if read_json(ROOT / "data/catchup-config.json").get("backend") == "background":
            if not args.dry_run:
                from manual_intake import process_pending
                manual_result = process_pending(ROOT)
                if manual_result is not None:
                    if sys.stdout:
                        print(json.dumps(manual_result, ensure_ascii=False))
                    return 1 if manual_result.get("action") == "failed" else 0
            from background_schedule import manual_only
            if manual_only(ROOT):
                if sys.stdout:
                    print(json.dumps({"action": "wait", "reason": "manual_only", "source": "request_checker"}))
                return 0
            from background_schedule import main as background_main
            return background_main(["--source", "catchup"] + (["--dry-run"] if args.dry_run else []), root=ROOT)
        if args.dry_run:
            result = run_once(dry_run=True)
        else:
            with host_lock(ROOT):
                result = run_once()
        if sys.stdout:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        result = {"checked_at": stamp(now_local()), "action": "wait", "reason": "host_check_failed", "detail": str(error)}
        if not args.dry_run:
            # No resets, cleanup, or archival writes on failure.
            write_json(ROOT / "evidence/catchup/last-error.json", result)
        if sys.stderr:
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
