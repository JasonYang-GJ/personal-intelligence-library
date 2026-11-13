"""Pure catch-up decisions. This module never launches tasks or writes checkpoints.

The host persists phase=starting BEFORE dispatch, running after confirmation, and
failed with failed_at after an observed failure. starting/running never expire
here: only the host's observed outcome may resolve an uncertain launch.
"""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
CATEGORIES = (
    "ai-models", "ai-products", "platform-income", "user-pains",
    "solo-business", "cross-border",
)


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(BEIJING)


def _parse(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an aware ISO string")
    return _aware(datetime.fromisoformat(value))


def decide(now, checkpoints, runtime, *, app_ready, native_busy, verification_target=None):
    """Return action/reason/due_at/missing_categories; inputs remain unchanged.

    checkpoints: {category: ISO string} or ledger tasks {category: {checked_through: ISO}}.
    runtime: {phase: idle|starting|running|failed, failed_at?: aware ISO string}.
    Any retained failed_at enforces a 15-minute cooldown, even if phase is idle.
    verification_target: explicit one-off observed-time target; bypasses only
    the daily 09:00 grace period, never readiness, busy, launch or retry gates.
    Missing/corrupt/future checkpoints wait for inspection, never imply coverage.
    """
    local = _aware(now)
    today_due = local.replace(hour=9, minute=0, second=0, microsecond=0)
    daily_due = today_due if local >= today_due else today_due - timedelta(days=1)
    due = daily_due
    if verification_target is not None:
        try:
            due = _parse(verification_target)
        except (ValueError, TypeError):
            return {"action": "wait", "reason": "invalid_verification_target", "due_at": None,
                    "missing_categories": list(CATEGORIES)}

    missing, invalid = [], []
    for category in CATEGORIES:
        value = checkpoints.get(category) if isinstance(checkpoints, dict) else None
        if isinstance(value, dict):
            value = value.get("checked_through")
        try:
            checkpoint = _parse(value)
            if checkpoint > local:
                raise ValueError("Checkpoint is in the future")
            if checkpoint < due:
                missing.append(category)
        except (ValueError, TypeError):
            missing.append(category)
            invalid.append(category)

    def result(action, reason, **extra):
        return {"action": action, "reason": reason, "due_at": due.isoformat(),
                "missing_categories": missing, **extra}

    if invalid:
        return result("wait", "invalid_checkpoints", invalid_categories=invalid)
    if due > local:
        return result("wait", "verification_target_in_future")
    if not missing:
        return result("covered", "checkpoints_cover_target")
    if not isinstance(runtime, dict):
        return result("wait", "invalid_runtime")
    phase = runtime.get("phase", "idle")
    if not isinstance(phase, str) or phase not in {"idle", "starting", "running", "failed"}:
        return result("wait", "invalid_runtime")
    if phase == "starting":
        return result("wait", "launch_awaiting_confirmation")
    if phase == "running":
        return result("wait", "confirmed_run_in_progress")
    if not app_ready:
        return result("wait", "app_not_ready")
    if native_busy:
        return result("wait", "native_task_busy")

    retry_at = None
    if phase == "failed" or runtime.get("failed_at") is not None:
        try:
            failed_at = _parse(runtime.get("failed_at"))
            if failed_at > local:
                raise ValueError("Failure time is in the future")
            retry_at = failed_at + timedelta(minutes=15)
        except (ValueError, TypeError):
            return result("wait", "invalid_failure_time")
    grace_until = due + timedelta(minutes=5) if verification_target is None else None
    waits = [(time, reason) for time, reason in ((retry_at, "failure_cooldown"), (grace_until, "native_grace_period"))
             if time is not None and local < time]
    if waits:
        next_time, reason = max(waits, key=lambda item: item[0])
        return result("wait", reason, retry_at=next_time.isoformat())
    return result("trigger", "verification_gap" if verification_target is not None else "daily_checkpoint_gap")
