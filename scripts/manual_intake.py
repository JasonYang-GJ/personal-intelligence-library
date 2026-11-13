"""Consume fixed-action requests from the authorized library folder.

The page writes requests only. This worker shares the daily runner's OS lock,
recovery, native-task gates, cooldown, six-source intake and real checkpoints.
Request text is never interpreted as a command, a filename or a source list.
"""
from datetime import timedelta
import json
from pathlib import Path
import re
import stat
import uuid

import background_schedule as schedule
from catchup_watch import parse_time, read_json, stamp, write_json

REQUEST_FIELDS = {"schema_version", "project_id", "id", "action", "requested_at"}
TERMINAL = {"complete", "partial", "failed"}
STATUSES = TERMINAL | {"waiting", "running"}
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def safe(root, relative):
    """All paths are constants plus a validated UUID; reject redirected folders."""
    root = Path(root).resolve()
    path = root
    for part in Path(relative).parts:
        if part in {"..", "."} or Path(part).is_absolute():
            raise ValueError("Invalid manual intake path")
        path = path / part
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("Refusing a redirected manual intake path")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Manual intake path escaped project")
    return path


def project_id(root):
    project = read_json(safe(root, "data/management/project.json"))
    value = project.get("project_id") if isinstance(project, dict) else None
    if (not isinstance(project, dict) or type(project.get("schema_version")) is not int
            or project["schema_version"] != 1 or not isinstance(value, str)
            or str(uuid.UUID(value)) != value):
        raise ValueError("Invalid library project identity")
    return value


def validate_request(value, identity, request_id, now):
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        raise ValueError("请求格式不正确，请刷新首页后重新提交。")
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["project_id"] != identity or value["id"] != request_id
            or value["action"] != "intake"):
        raise ValueError("请求身份或操作不匹配，未执行收录。")
    if not isinstance(value["requested_at"], str):
        raise ValueError("请求时间不正确，请重新提交。")
    try:
        requested = parse_time(value["requested_at"])
    except (ValueError, TypeError):
        raise ValueError("请求时间必须包含时区，请重新提交。") from None
    if now - requested > timedelta(hours=24):
        raise ValueError("这次请求已超过 24 小时，请重新点击手动收录。")
    if requested - now > timedelta(minutes=5):
        raise ValueError("请求时间超前超过 5 分钟，请检查电脑时间后重新提交。")
    return {"id": request_id, "target": value["requested_at"], "purpose": "manual_intake"}


def result_path(root, request_id):
    if not isinstance(request_id, str) or not UUID4.fullmatch(request_id):
        raise ValueError("Invalid manual request ID")
    return safe(root, f".intake/manual/results/{request_id}.json")


def previous_result(root, identity, request_id):
    path = result_path(root, request_id)
    if not path.exists():
        return None
    result = read_json(path)
    if (not isinstance(result, dict) or type(result.get("schema_version")) is not int
            or result["schema_version"] != 1 or result.get("project_id") != identity
            or result.get("id") != request_id or result.get("status") not in STATUSES
            or not isinstance(result.get("message"), str)):
        raise ValueError("Invalid saved manual result; refusing to reset")
    parse_time(result["updated_at"])
    return result


def save_result(root, identity, request_id, status, message, **extra):
    value = {"schema_version": 1, "project_id": identity, "id": request_id,
             "status": status, "message": message, "updated_at": stamp(schedule.now_local()), **extra}
    path = result_path(root, request_id)
    write_json(path, value)
    if read_json(path) != value:
        raise OSError("Manual intake result readback failed")
    return value


def record_item_error(root, identity, request_id):
    """Keep a corrupt response untouched and isolate it from automatic intake."""
    path = safe(root, f".intake/manual/errors/{request_id}.json")
    error = {"schema_version": 1, "project_id": identity, "id": request_id,
             "reason": "saved_result_invalid", "message": "手动收录回执损坏，已跳过该请求；自动补收仍可继续。"}
    if path.exists():
        try:
            existing = read_json(path)
            if isinstance(existing, dict) and all(existing.get(key) == value for key, value in error.items()):
                return
        except (ValueError, OSError, UnicodeError):
            # Do not replace an independently corrupted diagnostic either.
            return
    write_json(path, {**error, "updated_at": stamp(schedule.now_local())})


def failure_message(result):
    """Translate only known failure codes; never expose raw exception text."""
    messages = {
        "collector_interrupted": "读取窗口意外中断。",
        "collector_window_unavailable": "读取窗口已不可用，无法继续读取。",
        "collector_window_user_interaction": "读取窗口状态发生变化，已停止读取以免干扰使用。",
        "collector_creation_took_focus": "读取窗口意外切到前台，已停止读取以免干扰使用。",
        "collector_url_changed": "读取页面发生变化，已停止读取。",
        "login_required": "ChatGPT 需要登录，请先在 Chrome 中完成登录。",
        "page_loading": "ChatGPT 页面加载未完成，请确认网络正常后重试。",
        "undated_report": "有报告未显示可核对的生成时间，该栏目尚未完成检查。",
        "history_loading_timeout_without_boundary": "历史报告页面加载超时，请确认网络正常后重试。",
        "intake_timeout": "本轮收录等待超时，请确认 Chrome 已打开、ChatGPT 已登录且网络正常。",
        "summary_failed": "报告整理失败，相关检查断点未推进。",
        "summary_batch_limit": "新报告较多，已达到本轮处理上限。",
    }
    failures = result.get("failures")
    code = result.get("error_code")
    details = [messages[code]] if isinstance(code, str) and code in messages else []
    if isinstance(failures, dict):
        for value in failures.values():
            if not isinstance(value, dict):
                continue
            error, reason = value.get("error"), value.get("reason")
            message = ((messages.get(error) if isinstance(error, str) else None) or
                       (messages.get(reason) if isinstance(reason, str) else None))
            if message and message not in details:
                details.append(message)
    return "".join(details) or "未能确认具体原因，请查看本机收录记录。"


def display_result(result):
    reason = result.get("reason")
    if result.get("action") in {"completed", "covered"}:
        return "complete", "已完成六类报告检查；新报告已收录，已收录内容会自动跳过。"
    if result.get("status") in {"partial", "failed"} or result.get("action") == "failed":
        # Absence of progress fields is unknown, not evidence of zero progress.
        no_progress = all(result.get(key) == [] for key in
                          ("complete_categories", "imported_ids", "duplicate_ids"))
        status = "partial" if result.get("status") == "partial" and not no_progress else "failed"
        if no_progress:
            message = "本次收录失败：六类检查均未完成，没有新增报告。"
        elif result.get("imported_ids"):
            message = "本次收录未全部完成，已保存的新报告会保留。"
        elif status == "partial":
            message = "本次只完成了部分检查。"
        else:
            message = "本次收录未完成。"
        message += failure_message(result) + "未完成的检查断点已保留。"
        retry_at = result.get("retry_at")
        if retry_at:
            message += "请在 " + retry_at.replace("T", " ") + " 后重新点击“手动收录”。"
        else:
            message += "请排除原因后重新点击“手动收录”。"
        return status, message
    messages = {
        "failure_cooldown": "上次收录失败后需要等待 15 分钟；本次请求已保留，到时会继续。",
        "interrupted_runner_cooldown": "上次收录意外中断，正在等待 15 分钟保护间隔；本次请求会继续。",
        "native_task_busy": "已有收录任务正在运行，本次请求已排队。",
        "runner_busy": "已有收录正在运行，本次请求已排队。",
        "background_disabled": "后台收录尚未启用，本次请求正在等待。",
        "catchup_disabled": "后台检查已停用，本次请求正在等待。",
        "native_task_paused_or_schedule_changed": "每日收录任务已暂停或设置有变，本次请求正在等待；不会自动恢复任务。",
        "native_thread_missing_or_archived": "每日收录聊天不可用，本次请求正在等待。",
        "verification_target_in_future": "正在等待请求时间到达，请确认电脑时间准确。",
        "invalid_checkpoints": "收录检查记录不完整或时间异常，需要检查后才能继续。",
    }
    return "waiting", messages.get(reason, "收录条件尚未满足，本次请求已保留，后台会继续检查。")


def _process_locked(root, identity, paths):
    for path in paths:
        request_id = path.stem
        try:
            previous = previous_result(root, identity, request_id)
        except (ValueError, OSError, TypeError, KeyError, UnicodeError):
            record_item_error(root, identity, request_id)
            continue
        if previous and previous["status"] in TERMINAL:
            continue
        # showDirectoryPicker writes may expose the new empty placeholder until
        # its writable stream is closed. Never consume that placeholder.
        safe(root, path.relative_to(root))
        size = path.stat().st_size
        if size == 0:
            continue
        try:
            if size > 4096:
                raise ValueError("请求内容过大，未执行收录。")
            try:
                value = read_json(path)
            except (json.JSONDecodeError, UnicodeError):
                raise ValueError("请求文件无法读取，请刷新首页后重新提交。") from None
            request = validate_request(value, identity, request_id, schedule.now_local())
        except ValueError as error:
            response = save_result(root, identity, request_id, "failed", str(error))
            return {"action": "failed", "reason": "invalid_manual_request", "manual_result": response}

        def started(run_id):
            save_result(root, identity, request_id, "running", "正在读取六类报告并检查新内容，请稍候。", run_id=run_id)

        try:
            result = schedule._run_locked(root, "manual", manual_request=request, on_start=started)
        except Exception:
            # Never expose system paths/secrets from an exception to the page.
            # If the scheduler persisted running before a crash, the next
            # request still goes through recovery and the interruption gate.
            response = save_result(root, identity, request_id, "failed",
                                   "后台检查遇到错误，未确认收录完成。请检查后台运行记录后重试。")
            return {"action": "failed", "reason": "manual_check_failed", "manual_result": response}
        status, message = display_result(result)
        if schedule.manual_only(root) and status == "waiting":
            # An unmet condition is not permission for an unattended retry.
            # Keep the real reason/time, end this request, and require a new click.
            status = "failed"
            message = ("本次尚未开始收录，已停止等待，不会自动重试。"
                       "请在条件满足后重新点击“手动收录”。")
            if result.get("retry_at"):
                message = ("上次收录后仍处于保护等待期，本次未启动。"
                           "请在 " + result["retry_at"].replace("T", " ") +
                           " 后重新点击“手动收录”；不会自动重试。")
        extra = {key: result[key] for key in ("run_id", "summary", "retry_at") if result.get(key) is not None}
        response = save_result(root, identity, request_id, status, message, **extra)
        return {**result, "manual_result": response}
    return None


def process_pending(root):
    """Handle at most one queued request, or return None for normal scheduling.

    Caller must not call this during --dry-run. Lock contention never rewrites
    another process's running/final response; the page keeps showing queued.
    A crashed worker's running response is retried through real recovery.
    """
    root = Path(root).resolve()
    folder = safe(root, ".intake/manual/requests")
    if not folder.exists():
        return None
    paths = sorted((p for p in folder.glob("*.json") if UUID4.fullmatch(p.stem)),
                   key=lambda p: p.name)
    if not paths:
        return None
    identity = project_id(root)
    try:
        with schedule.schedule_lock(root):
            return _process_locked(root, identity, paths)
    except schedule.RunnerBusy:
        return {"action": "wait", "reason": "runner_busy", "source": "manual"}
