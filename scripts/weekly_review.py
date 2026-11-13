"""Archive the two authorized local weekly reviews, independently of six-source intake.

The model writes drafts only. This module validates a frozen evidence fingerprint,
stages the full library, and uses the existing forward-only file transaction.
Management events remain a separately observed user snapshot, never a write target.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid

import background_intake as intake
from background_schedule import RunnerBusy, schedule_lock
import build
import intake_state

ROOT = Path(__file__).resolve().parents[1]
PACKET_FIELDS = build.WEEKLY_REVIEW_PACKET_FIELDS
MAX_DRAFT_BYTES = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def draft_path(root, name, suffix):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("草稿须使用项目内 .intake/weekly-review/ 相对路径")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != suffix:
        raise ValueError("草稿路径或扩展名无效")
    path = intake.safe(root, name)
    if not path.is_relative_to(Path(root).resolve() / ".intake/weekly-review"):
        raise ValueError("草稿必须位于 .intake/weekly-review/ 内")
    return path


def read_draft(path):
    raw = path.read_bytes()
    if not raw.strip() or len(raw) > MAX_DRAFT_BYTES:
        raise ValueError("草稿为空或超过 2 MiB")
    raw.decode("utf-8")
    return raw


def times(scheduled_for, coverage_start, coverage_end, current):
    scheduled = build.review_timestamp(scheduled_for, "计划时间")
    if scheduled.isoformat() not in build.WEEKLY_REVIEW_SLOTS:
        raise ValueError("只允许已授权的 10 月 25 日和 11 月 1 日周复盘")
    start = build.review_timestamp(coverage_start, "覆盖开始")
    end = build.review_timestamp(coverage_end, "覆盖结束")
    if not start < end <= current:
        raise ValueError("覆盖时间必须递增且不能超过当前北京时间")
    return scheduled, start, end


def evidence_context(root, scheduled_for, coverage_start, coverage_end):
    from management_state import load_management

    library = build.load_library(root)
    management = load_management(root)
    if not isinstance(management.get("fingerprint"), str):
        raise ValueError("管理状态缺少依据指纹")
    records = sorted(library["records"], key=lambda item: item["id"])
    reviews = sorted(library.get("weekly_reviews", []), key=lambda item: item["id"])
    # load_library has already compared every SHA with the exact original bytes.
    # Runtime intake timestamps are intentionally absent: a no-change daily scan
    # must not invalidate a decision, while edits to any source content must.
    evidence = {"schema_version": 1, "scheduled_for": scheduled_for,
                "coverage_start": coverage_start, "coverage_end": coverage_end,
                "records": [{key: value for key, value in item.items() if key != "original"} for item in records],
                "weekly_reviews": [{key: value for key, value in item.items() if key != "original"} for item in reviews],
                "management_fingerprint": management["fingerprint"]}
    states = management.get("states", {})
    visible = lambda item: states.get(item["id"], {}).get("state") != "trashed"
    return {"schema_version": 1, "scheduled_for": scheduled_for,
            "coverage_start": coverage_start, "coverage_end": coverage_end,
            "input_sha256": digest(canonical(evidence)),
            "source_task_id": build.WEEKLY_REVIEW_TASK_ID,
            "source_thread_id": build.WEEKLY_REVIEW_THREAD_ID,
            "records": [item for item in records if visible(item)],
            "weekly_reviews": [item for item in reviews if visible(item)],
            "management": management,
            "last_intake": library.get("last_intake"),
            "evidence_note": "覆盖时间描述本次复盘所用资料窗口；完整性另以每日收录断点为准。未记录的行动或付费反馈保持未知。"}


def prepare_input(root, *, scheduled_for, coverage_start, coverage_end, output):
    root = Path(root).resolve()
    target = draft_path(root, output, ".json")
    with schedule_lock(root):
        intake.recover_transactions(root, acknowledge_intake=False)
        scheduled, start, end = times(scheduled_for, coverage_start, coverage_end, intake.now())
        context = evidence_context(root, scheduled.isoformat(), start.isoformat(), end.isoformat())
        content = intake.encode(context)
        if target.exists():
            if target.read_bytes() != content:
                raise ValueError("输入草稿已存在且内容不同；请使用新的运行目录")
        else:
            build.atomic_write(target, content)
    return {"status": "input_prepared", "input_file": target.relative_to(root).as_posix(),
            "input_sha256": context["input_sha256"], "record_count": len(context["records"]),
            "weekly_review_count": len(context["weekly_reviews"])}


def validate_packet(packet, library, current):
    if not isinstance(packet, dict) or set(packet) != PACKET_FIELDS or type(packet.get("schema_version")) is not int or packet["schema_version"] != 1:
        raise ValueError("周复盘 packet 字段不完整或包含未允许字段")
    scheduled, start, end = times(packet["scheduled_for"], packet["coverage_start"], packet["coverage_end"], current)
    packet = deepcopy(packet)
    packet.update(scheduled_for=scheduled.isoformat(), coverage_start=start.isoformat(), coverage_end=end.isoformat())
    build.validate_review_fields(packet, {record["id"] for record in library["records"]})
    if scheduled > current:
        raise ValueError("不能在计划时间之前写入正式周复盘")
    return packet, scheduled


def _commit_locked(root, packet_name, markdown_name):
    # Recovery precedes duplicate checks: an existing catalog entry does not
    # prove its homepage/result finished writing before a previous interruption.
    intake.recover_transactions(root, acknowledge_intake=False)
    packet_path = draft_path(root, packet_name, ".json")
    markdown_path = draft_path(root, markdown_name, ".md")
    packet_raw, original = read_draft(packet_path), read_draft(markdown_path)
    packet = json.loads(packet_raw.decode("utf-8-sig"), object_pairs_hook=build.unique_keys)
    library = build.load_library(root)
    generated = intake.now()
    packet, scheduled = validate_packet(packet, library, generated)
    packet_hash, original_hash = digest(canonical(packet)), digest(original)
    rid = "review-" + scheduled.strftime("%Y%m%d")
    existing = next((review for review in library.get("weekly_reviews", []) if review["id"] == rid), None)
    if existing is not None:
        if existing["packet_sha256"] != packet_hash or existing["original_sha256"] != original_hash:
            raise ValueError("同一周复盘计划已有不同内容；拒绝覆盖正式复盘")
        return {"status": "already_committed", "review_id": rid, "scheduled_for": packet["scheduled_for"],
                "original_file": existing["original_file"], "original_sha256": original_hash}
    context = evidence_context(root, packet["scheduled_for"], packet["coverage_start"], packet["coverage_end"])
    visible_ids = {item["id"] for item in context["records"]}
    if any(rid not in visible_ids for rid in packet["candidate_record_ids"]):
        raise ValueError("周复盘不能推荐已移入回收站的情报")
    if context["input_sha256"] != packet["input_sha256"]:
        raise ValueError("复盘输入依据已变化；请重新生成输入与结论")
    baseline = intake.snapshot(root)
    catalog = intake.read_json(intake.safe(root, "data/catalog.json"))
    run_id = f"review-{scheduled:%Y%m%d}-{generated:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:12]}"
    directory = intake.safe(root, ".intake/background/runs/" + run_id)
    directory.mkdir(parents=True, exist_ok=False)
    stage = directory / "staging"
    for name in baseline:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(intake.safe(root, name), target)
    original_name = f"data/reviews/{rid}.md"
    original_target = stage / original_name
    original_target.parent.mkdir(parents=True, exist_ok=True)
    with original_target.open("xb") as handle:
        handle.write(original)
    review = {key: value for key, value in packet.items() if key != "schema_version"}
    review.update(id=rid, source_task_id=build.WEEKLY_REVIEW_TASK_ID,
                  source_thread_id=build.WEEKLY_REVIEW_THREAD_ID, generated_at=generated.isoformat(),
                  collected_at=generated.date().isoformat(), original_file=original_name,
                  original_sha256=original_hash, packet_sha256=packet_hash)
    catalog.setdefault("weekly_reviews", []).append(review)
    catalog["updated_at"] = generated.date().isoformat()
    intake.write(stage / "data/catalog.json", catalog)
    build.build(stage, management_snapshot=context["management"])
    result_name = f"evidence/weekly-review/{run_id}/result.json"
    result = {"status": "committed", "kind": "weekly_review", "run_id": run_id, "review_id": rid,
              "scheduled_for": packet["scheduled_for"], "generated_at": generated.isoformat(),
              "coverage_start": packet["coverage_start"], "coverage_end": packet["coverage_end"],
              "input_sha256": packet["input_sha256"], "packet_sha256": packet_hash,
              "original_file": original_name, "original_sha256": original_hash, "result_file": result_name}
    intake.write(stage / result_name, result)
    writes = [original_name, "data/catalog.json", build.OUTPUT, "evidence/build-manifest.json", result_name]
    operations = [{"path": name, "old_sha256": baseline.get(name), "new_sha256": digest((stage / name).read_bytes()),
                   "prepared_file": (stage / name).relative_to(root).as_posix()} for name in writes]
    journal = {"schema_version": 1, "kind": "weekly_review", "run_id": run_id, "status": "prepared",
               "baseline": baseline, "operations": operations, "result_file": result_name}
    with intake_state.locked(root):
        intake.verify_snapshot(root, baseline)
        current_context = evidence_context(root, packet["scheduled_for"], packet["coverage_start"], packet["coverage_end"])
        if current_context["input_sha256"] != packet["input_sha256"]:
            raise ValueError("复盘依据在准备期间变化；未提交正式资料")
        if packet_path.read_bytes() != packet_raw or markdown_path.read_bytes() != original:
            raise ValueError("复盘草稿在准备期间变化；未提交正式资料")
        # A browser can append a management event immediately after this
        # observation. Such later user actions do not rewrite this saved review.
        intake.write(directory / "journal.json", journal)
        return intake.apply_transaction(root, directory / "journal.json")


def commit(root=ROOT, *, packet, markdown):
    root = Path(root).resolve()
    # Validate locations before lock creation or recovery has any side effects.
    draft_path(root, packet, ".json")
    draft_path(root, markdown, ".md")
    with schedule_lock(root):
        return _commit_locked(root, packet, markdown)


def main(argv=None):
    # Windows pipes otherwise use the legacy console code page and corrupt the
    # Chinese JSON results consumed by the scheduled task.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="将已授权周复盘草稿安全写入个人情报库；不运行每日采集。")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("input", help="生成含原文、管理状态和依据指纹的本地输入")
    for name in ("scheduled-for", "coverage-start", "coverage-end", "output"):
        prepare.add_argument("--" + name, required=True)
    submit = commands.add_parser("commit", help="校验草稿并事务入库")
    submit.add_argument("--packet", required=True)
    submit.add_argument("--markdown", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "input":
            result = prepare_input(ROOT, scheduled_for=args.scheduled_for, coverage_start=args.coverage_start,
                                   coverage_end=args.coverage_end, output=args.output)
        else:
            result = commit(ROOT, packet=args.packet, markdown=args.markdown)
    except RunnerBusy:
        print(json.dumps({"status": "busy", "error": "已有收录或复盘在运行；保留草稿，稍后重试。"}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, UnicodeError, KeyError, TypeError) as error:
        print(json.dumps({"status": "conflict", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
