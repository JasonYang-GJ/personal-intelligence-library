"""Local intake ledger; timestamps are observed source times, never inferred dates."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import CATEGORIES, atomic_write, load_library

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "2026-10-18T00:00:00+08:00"


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,7})?)?\+08:00", value):
        raise ValueError("时间必须明确为 ISO 北京时间，例如 2026-10-18T00:00:00+08:00")
    return datetime.fromisoformat(value).isoformat(timespec="seconds")


def normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    def clean_url(match):
        p = urlsplit(match.group(0))
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), p.fragment))
    text = re.sub(r"https?://[^\s<>\)\]]+", clean_url, text)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"^#{1,6}[ \t]+", "", line).strip()
        if not line or re.fullmatch(r"\|(?:\s*:?-+:?\s*\|)+", line):
            continue
        if line.startswith("|") and line.endswith("|"):
            line = "|" + "|".join(cell.strip() for cell in line[1:-1].split("|")) + "|"
        lines.append(line)
    return "\n".join(lines)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def occurrence(root, filename, category, generated_at):
    if category not in CATEGORIES:
        raise ValueError("未知分类")
    path = (root / filename).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or path.suffix not in {".md", ".txt"}:
        raise ValueError("捕获原文必须是项目内的 .md/.txt 文件")
    raw = path.read_bytes()
    if not raw.strip():
        raise ValueError("捕获原文为空")
    when = timestamp(generated_at)
    canonical = digest(normalize(raw.decode("utf-8")).encode("utf-8"))
    key = digest(f"{category}\n{when}\n{canonical}".encode("utf-8"))
    return key, {"category_id": category, "generated_at": when, "normalized_sha256": canonical,
                 "raw_sha256": digest(raw), "capture_file": path.relative_to(root.resolve()).as_posix()}


def read_state(root):
    path = root / "data/intake-state.json"
    if path.is_symlink():
        raise ValueError("拒绝读取符号链接台账")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1 or state.get("normalization_version") != 1:
        raise ValueError("台账版本不匹配；禁止自动重置")
    if set(state["tasks"]) != set(CATEGORIES) or not isinstance(state["occurrences"], dict):
        raise ValueError("台账结构不完整；禁止自动重置")
    for task in state["tasks"].values():
        timestamp(task["checked_through"])
    return state


@contextmanager
def locked(root):
    """Shared OS-owned lock; a committed-format marker persists after exit.

    An old O_EXCL/PID lock is not evidence its owner has exited. Unknown,
    legacy, or interrupted marker initialization is therefore never adopted.
    """
    path = root / "data/intake-state.lock"
    marker = b"personal-intel-ledger-byte-lock-v1\n"
    if path.is_symlink() or (path.exists() and getattr(path.stat(), "st_file_attributes", 0) & 0x400):
        raise ValueError("拒绝链接台账锁文件")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, os.O_RDWR)
        created = False
    with os.fdopen(fd, "r+b") as handle:
        if created:
            # Before the marker is complete, other callers fail closed. Old
            # writers also fail O_EXCL because this same path already exists.
            handle.write(marker)
            handle.flush()
            os.fsync(handle.fileno())
        acquired = False
        try:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                raise BlockingIOError("另一个进程正在维护台账，稍后重试") from error
            handle.seek(0)
            if handle.read(len(marker) + 1) != marker:
                raise FileExistsError("发现旧版或未知台账锁；需确认旧进程状态，禁止自动接管")
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            # Never unlink: another process may already hold this same inode.


def save(root, state):
    atomic_write(root / "data/intake-state.json", (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def matching(records, canonical):
    return next((r for r in records if digest(normalize(r["original"]).encode("utf-8")) == canonical), None)


def notification_pending(entry):
    # Legacy reported_in_run_id, including the baseline, remains acknowledged.
    return bool(entry and entry.get("outcome") == "duplicate" and not entry.get("reported_in_run_id"))


def pending_notifications(state):
    return [{"occurrence_key": key, "existing_id": entry["record_id"], "category_id": entry["category_id"],
             "generated_at": entry["generated_at"], "first_marked_in_run_id": entry["run_id"], "capture_file": entry["capture_file"]}
            for key, entry in state["occurrences"].items() if notification_pending(entry)]


def acknowledge_result(args, root, state):
    path = (root / args.result_file).resolve(strict=True)
    if not path.is_relative_to(root / "evidence/daily") or path.suffix != ".json":
        raise ValueError("确认依据必须是 evidence/daily 内已保存的结果 JSON")
    raw = path.read_bytes()
    result = json.loads(raw.decode("utf-8-sig"))
    if (not isinstance(result, dict) or result.get("run_id") != args.run_id or result.get("status") not in {"complete", "partial", "failed"}
            or not isinstance(result.get("summary"), str) or not result["summary"].strip()):
        raise ValueError("结果须包含匹配的 run_id、最终 status 和非空 summary；草稿不能确认")
    items = result.get("duplicate_notifications")
    if not isinstance(items, list) or not items:
        raise ValueError("结果未包含实际重复提醒")
    keys = set()
    for item in items:
        key = item.get("occurrence_key") if isinstance(item, dict) else None
        entry = state["occurrences"].get(key)
        if (key in keys or not entry or entry.get("outcome") != "duplicate" or item.get("existing_id") != entry["record_id"]
                or not isinstance(item.get("message"), str) or not item["message"].strip()):
            raise ValueError("结果中的重复身份、已有记录或提醒正文无效；不确认任何条目")
        keys.add(key)
    acknowledged = 0
    for key in keys:
        entry = state["occurrences"][key]
        if notification_pending(entry):
            entry.update(notification_status="recorded_in_result", reported_in_run_id=args.run_id,
                         notification_result={"file": path.relative_to(root).as_posix(), "sha256": digest(raw)})
            acknowledged += 1
    if acknowledged:
        save(root, state)
    return {"status": "recorded_in_result", "acknowledged": acknowledged,
            "pending_count": len(pending_notifications(state)), "delivery_verified": False}


def execute(args, root=ROOT):
    root = Path(root).resolve()
    if args.command in {"init", "mark", "checkpoint", "ack-notifications"}:
        with locked(root):
            return mutate(args, root)
    state = read_state(root)
    if args.command == "status":
        return {"tasks": state["tasks"], "known_occurrences": len(state["occurrences"]), "pending_notifications": pending_notifications(state)}
    key, entry = occurrence(root, args.file, args.category, args.generated_at)
    records = load_library(root)["records"]
    known = state["occurrences"].get(key)
    match = matching(records, entry["normalized_sha256"])
    return {"status": "known" if known else "duplicate" if match else "new", "occurrence_key": key,
            "existing_id": known["record_id"] if known else match["id"] if match else None,
            "notification_pending": notification_pending(known), **entry}


def mutate(args, root):
    if args.command == "init":
        if (root / "data/intake-state.json").exists():
            raise ValueError("台账已存在，init 不会覆盖或重置")
        records = load_library(root)["records"]
        state = {"schema_version": 1, "normalization_version": 1,
                 "tasks": {c: {"checked_through": BASELINE, "run_id": "baseline-20261018"} for c in CATEGORIES}, "occurrences": {}}
        for record in records:
            when = record.get("source_capture", {}).get("generated_at")
            if when is None:
                continue  # Missing provenance cannot become an invented occurrence.
            key, entry = occurrence(root, record["original_file"], record["category_id"], when)
            state["occurrences"][key] = {**entry, "outcome": "imported", "record_id": record["id"], "run_id": "initial-import"}
        save(root, state)
        return {"status": "initialized", "known_occurrences": len(state["occurrences"])}
    state = read_state(root)
    if not args.run_id.strip():
        raise ValueError("run-id 不能为空")
    if args.command == "ack-notifications":
        return acknowledge_result(args, root, state)
    if args.command == "checkpoint":
        through = timestamp(args.through)
        if through < state["tasks"][args.category]["checked_through"]:
            raise ValueError("检查断点不能倒退")
        state["tasks"][args.category] = {"checked_through": through, "run_id": args.run_id}
        save(root, state)
        return {"status": "checkpointed", "category_id": args.category, "checked_through": through}
    key, entry = occurrence(root, args.file, args.category, args.generated_at)
    record = next((r for r in load_library(root)["records"] if r["id"] == args.record_id), None)
    if not record or digest(normalize(record["original"]).encode("utf-8")) != entry["normalized_sha256"]:
        raise ValueError("指定记录不存在或正文不匹配；不能标记完成")
    if args.outcome == "imported" and (record["original_sha256"] != entry["raw_sha256"] or record["category_id"] != args.category):
        raise ValueError("入库原文字节或分类不一致；不能标记完成")
    known = state["occurrences"].get(key)
    if known:
        if known["record_id"] != args.record_id or known["outcome"] != args.outcome:
            raise ValueError("出现身份已有不同处理结果；拒绝覆盖")
        return {"status": "known", "existing_id": args.record_id, "notification_pending": notification_pending(known)}
    state["occurrences"][key] = {**entry, "outcome": args.outcome, "record_id": args.record_id, "run_id": args.run_id}
    if args.outcome == "duplicate":
        state["occurrences"][key]["notification_status"] = "pending"
    save(root, state)
    return {"status": "marked", "outcome": args.outcome, "existing_id": args.record_id,
            "occurrence_key": key, "notification_pending": args.outcome == "duplicate"}


def parser():
    cli = argparse.ArgumentParser(description="本地收录台账；checkpoint 仅在明确完整扫描后调用，不自动判定成功")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "check", "mark", "checkpoint", "ack-notifications"):
        command = commands.add_parser(name)
        if name in {"check", "mark", "checkpoint"}:
            command.add_argument("--category", choices=CATEGORIES, required=True)
        if name in {"check", "mark"}:
            command.add_argument("--file", required=True)
            command.add_argument("--generated-at", required=True)
        if name in {"mark", "checkpoint", "ack-notifications"}:
            command.add_argument("--run-id", required=True)
        if name == "ack-notifications":
            command.add_argument("--result-file", required=True)
        if name == "mark":
            command.add_argument("--outcome", choices=("imported", "duplicate"), required=True)
            command.add_argument("--record-id", required=True)
        if name == "checkpoint":
            command.add_argument("--through", required=True)
    return cli


if __name__ == "__main__":
    try:
        print(json.dumps(execute(parser().parse_args()), ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
