"""Hidden intake scheduling; only the runner may write library data/checkpoints.

The byte lock spans the decision and the complete synchronous intake call.
Acquiring it proves no prior scheduler still owns the run, irrespective of age.
"""
from contextlib import contextmanager
from datetime import timedelta
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from catchup_policy import CATEGORIES, decide
from catchup_watch import native_activity, now_local, parse_time, read_json, stamp, write_json

ROOT = Path(__file__).resolve().parents[1]


class RunnerBusy(Exception):
    pass


def directory(root):
    root = Path(root).resolve()
    path = root / ".intake/background"
    if not path.resolve().is_relative_to(root):
        raise ValueError("Background state directory escaped project")
    return path


@contextmanager
def schedule_lock(root):
    """OS lock, not a PID file or timeout: crashes release ownership."""
    import msvcrt
    folder = directory(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "schedule.lock"
    if path.is_symlink():
        raise ValueError("Refusing a symlink lock")
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RunnerBusy from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def load_runtime(root):
    path = directory(root) / "runtime.json"
    if not path.exists():
        return {"schema_version": 1, "phase": "idle", "run_count": 0}
    state = read_json(path)
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or state.get("phase") not in {"idle", "running", "failed"}
            or type(state.get("run_count")) is not int or state["run_count"] < 0):
        raise ValueError("Invalid background runtime; not resetting")
    return state


def enabled_config(root):
    path = directory(root) / "config.json"
    if not path.exists():
        return False
    config = read_json(path)
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("Invalid background config")
    return config.get("enabled") is True


def manual_only(root):
    mode = read_json(Path(root) / "data/catchup-config.json").get("mode", "automatic")
    if mode not in {"automatic", "manual_only"}:
        raise ValueError("Unknown intake mode; refusing to schedule")
    return mode == "manual_only"


def verification_request(root, runtime, now):
    path = directory(root) / "verification.json"
    if not path.exists():
        return None
    request = read_json(path)
    if (not isinstance(request, dict)
            or request.get("purpose") != "user_authorized_integration_verification"
            or not isinstance(request.get("id"), str)
            or not 0 < len(request["id"]) <= 128):
        raise ValueError("Invalid background verification request")
    if request["id"] == runtime.get("verification_completed_id"):
        return None
    target, expires = parse_time(request["target"]), parse_time(request["expires_at"])
    if expires <= now:
        return None
    if (target > now or now - target > timedelta(hours=2)
            or expires <= target or expires - target > timedelta(hours=2)):
        raise ValueError("Invalid background verification interval")
    return request


def ledger_tasks(root):
    ledger = read_json(Path(root) / "data/intake-state.json")
    if not isinstance(ledger, dict) or ledger.get("schema_version") != 1 or not isinstance(ledger.get("tasks"), dict):
        raise ValueError("Invalid intake ledger")
    return ledger["tasks"]


def evaluate(now, tasks, runtime, source, activity, request):
    if not activity.get("available"):
        return {"action": "wait", "reason": activity.get("reason", "native_task_unavailable")}
    # Daily is called by the native task itself; its own busy event is expected.
    if source in {"catchup", "manual"} and activity.get("busy"):
        return {"action": "wait", "reason": "native_task_busy"}
    target = request["target"] if request else None
    if source == "daily" and target is None:
        daily = now.replace(hour=9, minute=0, second=0, microsecond=0)
        target = stamp(daily if now >= daily else daily - timedelta(days=1))
    result = decide(now, tasks, runtime, app_ready=True, native_busy=False, verification_target=target)
    if source == "daily" and request is None and result.get("reason") == "verification_gap":
        result["reason"] = "daily_checkpoint_gap"
    return result


def invoke_intake(root, run_id):
    from background_intake import run_intake
    return run_intake(root, run_id)


def recover_intake(root):
    # Lazy import avoids coupling module initialization to the coordinator.
    # Recovery can complete a durable commit and acknowledge its saved notices;
    # it never launches a browser or starts another intake.
    from background_intake import recover_transactions
    return recover_transactions(Path(root))


def record(root, runtime, result, now, *, save):
    result = {"checked_at": stamp(now), "phase": runtime["phase"],
              "run_count": runtime["run_count"], **result}
    if save:
        runtime["last_check_at"] = stamp(now)
        runtime["last_result"] = result
        write_json(directory(root) / "runtime.json", runtime)
    return result


def consume_coverage(runtime, decision, request, now):
    runtime.update(phase="idle", last_coverage_verified_at=stamp(now), last_covered_target=decision["due_at"])
    runtime.pop("failed_at", None)
    runtime.pop("last_failure", None)
    if request and request.get("purpose") == "manual_intake":
        runtime.update(manual_completed_id=request["id"], manual_completed_at=stamp(now),
                       manual_completed_target=request["target"])
    elif request:
        runtime.update(verification_completed_id=request["id"], verification_completed_at=stamp(now),
                       verification_completed_target=request["target"])


def _run_locked(root, source, *, dry_run=False, manual_request=None, on_start=None):
    if source == "manual" and (not manual_request or manual_request.get("purpose") != "manual_intake"):
        raise ValueError("Manual scheduling requires a validated request")
    def current_request(runtime, now):
        return manual_request if source == "manual" else verification_request(root, runtime, now)

    only_manual = manual_only(root)
    if only_manual and source != "manual":
        return {"action": "wait", "reason": "manual_only", "source": source}

    def current_activity(config):
        # The shared OS lock still protects every writer. Manual-only intake
        # has no dependency on a desktop window, native chat, or daily schedule.
        return {"available": True, "busy": False} if only_manual else native_activity(config)

    if not enabled_config(root):
        return {"action": "wait", "reason": "background_disabled"}
    config = read_json(Path(root) / "data/catchup-config.json")
    if config.get("schema_version") != 1 or config.get("enabled") is not True:
        return {"action": "wait", "reason": "catchup_disabled"}
    runtime = load_runtime(root)
    now = now_local()
    activity = current_activity(config)
    if not activity.get("available") or (source in {"catchup", "manual"} and activity.get("busy")):
        return record(root, runtime, {"action": "wait", "source": source,
                      "reason": activity.get("reason", "native_task_unavailable")
                      if not activity.get("available") else "native_task_busy"}, now, save=not dry_run)
    if not dry_run:
        # An already-written ledger can be ahead of the result/journal after
        # a crash. Never call decide() until pending commits have rolled forward.
        # A recovery conflict must also outrank apparently covered checkpoints.
        if runtime.get("last_failure") == "transaction_recovery_failed":
            failed_at = parse_time(runtime["failed_at"])
            if failed_at > now:
                raise ValueError("Recovery failure time is in the future")
            retry_at = failed_at + timedelta(minutes=15)
            if now < retry_at:
                return record(root, runtime, {"action": "wait", "reason": "failure_cooldown",
                              "source": source, "retry_at": stamp(retry_at)}, now, save=True)
        try:
            recover_intake(root)
        except Exception as error:
            failed = now_local()
            runtime.update(phase="failed", failed_at=stamp(failed), last_failure="transaction_recovery_failed")
            return record(root, runtime, {"action": "failed", "status": "failed",
                          "reason": "transaction_recovery_failed", "source": source,
                          "detail": str(error)[:1000], "retry_at": stamp(failed + timedelta(minutes=15))}, failed, save=True)
        now = now_local()
    # A dry run has no lock and therefore cannot infer a prior runner is dead.
    if runtime["phase"] == "running":
        if dry_run:
            return {"action": "wait", "reason": "running_state_unchecked_without_lock", "dry_run": True}
        runtime.update(phase="failed", failed_at=stamp(now), last_failure="previous_runner_interrupted")
        return record(root, runtime, {"action": "wait", "reason": "interrupted_runner_cooldown",
                                      "retry_at": stamp(now + timedelta(minutes=15))}, now, save=True)
    request = current_request(runtime, now)
    decision = evaluate(now, ledger_tasks(root), runtime, source, activity, request)
    if decision["action"] == "covered":
        consume_coverage(runtime, decision, request, now)
    if decision["action"] != "trigger" or dry_run:
        return record(root, runtime, {**decision, "source": source, "dry_run": dry_run}, now, save=not dry_run)
    # Recheck the task and real ledger just before committing to a run.
    now = now_local()
    activity = current_activity(config)
    request = current_request(runtime, now)
    decision = evaluate(now, ledger_tasks(root), runtime, source, activity, request)
    if decision["action"] != "trigger":
        if decision["action"] == "covered":
            consume_coverage(runtime, decision, request, now)
        return record(root, runtime, {**decision, "source": source}, now, save=True)
    run_id = "bg-" + now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    runtime.update(phase="running", run_id=run_id, source=source, requested_at=stamp(now),
                   target=decision["due_at"], run_count=runtime["run_count"] + 1,
                   verification_request_id=request["id"] if request and source != "manual" else None,
                   manual_request_id=request["id"] if source == "manual" else None)
    runtime.pop("failed_at", None)
    runtime.pop("last_failure", None)
    write_json(directory(root) / "runtime.json", runtime)
    try:
        if on_start:
            on_start(run_id)
        result = invoke_intake(Path(root), run_id)
        if not isinstance(result, dict) or result.get("status") not in {"complete", "partial", "failed"}:
            raise ValueError("Invalid intake result")
        # Keep structured evidence for the manual receipt. Do not infer counts
        # or causes from the human-readable summary, or expose the whole result.
        result_details = {key: result[key] for key in
                          ("complete_categories", "imported_ids", "duplicate_ids", "failures") if key in result}
        # Checkpoints alone establish coverage; a runner's status is insufficient.
        finished = now_local()
        coverage = decide(finished, ledger_tasks(root), {"phase": "idle"}, app_ready=True,
                          native_busy=False, verification_target=decision["due_at"])
        if result["status"] == "complete" and coverage["action"] == "covered":
            consume_coverage(runtime, coverage, request, finished)
            runtime["finished_at"] = stamp(finished)
            return record(root, runtime, {"action": "completed", "reason": "intake_and_coverage_complete",
                          "status": "complete", "run_id": run_id, "source": source,
                          "due_at": decision["due_at"], "summary": result.get("summary")}, finished, save=True)
        runtime.update(phase="failed", failed_at=stamp(finished), finished_at=stamp(finished),
                       last_failure="intake_" + result["status"] if result["status"] != "complete" else "coverage_not_complete")
        return record(root, runtime, {"action": "failed", "reason": runtime["last_failure"],
                      "status": result["status"] if result["status"] != "complete" else "failed",
                      "run_id": run_id, "source": source, "due_at": decision["due_at"],
                      "coverage": coverage, "summary": result.get("summary"),
                      "retry_at": stamp(finished + timedelta(minutes=15)), **result_details}, finished, save=True)
    except Exception as error:
        finished = now_local()
        runtime.update(phase="failed", failed_at=stamp(finished), finished_at=stamp(finished),
                       last_failure="intake_exception")
        return record(root, runtime, {"action": "failed", "reason": "intake_exception", "status": "failed",
                      "run_id": run_id, "source": source, "detail": str(error)[:1000],
                      **({"error_code": "intake_timeout"} if isinstance(error, TimeoutError) else {}),
                      "retry_at": stamp(finished + timedelta(minutes=15))}, finished, save=True)


def run_once(root=ROOT, *, source="catchup", dry_run=False):
    if source not in {"catchup", "daily"}:
        raise ValueError("Unknown background schedule source")
    root = Path(root)
    if manual_only(root):
        return {"action": "wait", "reason": "manual_only", "source": source, "dry_run": dry_run}
    if not enabled_config(root):
        return {"action": "wait", "reason": "background_disabled"}
    config = read_json(root / "data/catchup-config.json")
    if config.get("schema_version") != 1 or config.get("enabled") is not True:
        return {"action": "wait", "reason": "catchup_disabled"}
    if dry_run:
        return _run_locked(root, source, dry_run=True)
    try:
        with schedule_lock(root):
            return _run_locked(root, source)
    except RunnerBusy:
        return {"action": "wait", "reason": "runner_busy"}


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description="个人情报库后台收录调度器")
    parser.add_argument("--source", choices=("daily", "catchup"), default="catchup")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_once(root, source=args.source, dry_run=args.dry_run)
        code = 1 if result.get("action") == "failed" else 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        result = {"action": "wait", "reason": "background_check_failed", "detail": str(error)[:1000]}
        code = 1
    if sys.stdout:
        print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
