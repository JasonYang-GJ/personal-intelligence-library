"""Fixed-source background intake; model summaries never own library writes.

The scheduler holds its OS lock for this entire entry point. A second ledger
lock protects the final comparison and transaction. Prepared multi-file commits
are roll-forward only, and refuse unexpected user edits instead of restoring
older backups. Source tasks, browser focus, mouse and clipboard are untouched.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
from types import SimpleNamespace

import build
import intake_state
from intake_alias import plan_aliases, source_key, SOURCES

ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))
FLOOR = datetime(2026, 10, 16, tzinfo=BEIJING)
CAPTURE_TIMEOUT = 900
MAX_NEW_PER_RUN = 4


def now():
    return datetime.now(BEIJING)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=build.unique_keys)


def safe(root, relative):
    root = Path(root).resolve()
    candidate = root / relative
    if not candidate.absolute().is_relative_to(root) or ".." in Path(relative).parts:
        raise ValueError("Path outside project")
    for path in (candidate, *candidate.parents):
        if path == root.parent:
            break
        if path.exists() and (path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Refusing a linked project path")
    return candidate


def write(path, value):
    build.atomic_write(path, encode(value))


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp has no timezone")
    return result.astimezone(BEIJING)


def snapshot(root):
    catalog = build.load_library(root)
    paths = ["data/catalog.json", "data/intake-state.json", build.OUTPUT, "evidence/build-manifest.json",
             "ui/template.html", "ui/styles.css", "ui/app.js", "PROJECT.md", "AGENTS.md", "使用说明.txt", "scripts/build.py"]
    paths += [r["original_file"] for r in catalog["records"]]
    paths += [p for p in ("ui/management.js", "ui/manual-intake.js", "scripts/management_state.py", "data/management/project.json") if safe(root, p).exists()]
    paths += [r["original_file"] for r in catalog.get("weekly_reviews", [])]
    return {p: sha(safe(root, p).read_bytes()) for p in paths}


def verify_snapshot(root, expected):
    for name, digest in expected.items():
        if sha(safe(root, name).read_bytes()) != digest:
            raise ValueError("Library changed during preparation: " + name)


def request_capture(root, run_id):
    """Publish only a fixed reader plan, then wait for actual extension evidence."""
    from extension_native_host import locked as host_locked
    from background_schedule import manual_only
    only_manual = manual_only(root)
    path = safe(root, ".intake/extension-probe/config.json")
    previous = read_json(path)
    expected = {(c, task, cid) for c, (task, cid) in SOURCES.items()}
    actual = {(s["category_id"], s["task_name"], s["conversation_id"]) for s in previous["sources"]}
    if actual != expected or previous.get("extension_id") != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":
        raise ValueError("Fixed extension/source configuration mismatch")
    previous_id = "extension-probe-" + sha(previous["request_id"].encode())[:24]
    previous_path = safe(root, f"evidence/extension-probe/{previous_id}/run.json")
    prior = read_json(previous_path) if previous_path.exists() else None
    # Resume an interrupted background capture; never displace a live reader.
    if prior and prior["status"] == "pending" and not only_manual:
        if not previous["request_id"].startswith("bg-"):
            raise ValueError("Another browser verification is still active")
        capture_id, request_id = previous_id, previous["request_id"]
    else:
        ledger = intake_state.read_state(root)
        start = max(FLOOR, min(instant(v["checked_through"]) for v in ledger["tasks"].values()) - timedelta(hours=48))
        plan = {"extension_id": previous["extension_id"], "enabled": True, "mode": "probe",
                "request_id": run_id, "scan_start": start.isoformat(), "scan_end": None,
                "sources": previous["sources"]}
        with host_locked(root):
            if read_json(path) != previous:
                raise ValueError("Reader configuration changed concurrently")
            write(path, plan)
        request_id = run_id
        capture_id = "extension-probe-" + sha(run_id.encode())[:24]
    directory = safe(root, "evidence/extension-probe/" + capture_id)
    deadline = time.monotonic() + CAPTURE_TIMEOUT
    try:
        while time.monotonic() < deadline:
            if read_json(path).get("request_id") != request_id:
                raise ValueError("Reader plan was replaced; stop without advancing coverage")
            state_path = directory / "run.json"
            if state_path.exists():
                state = read_json(state_path)
                if state["status"] in {"complete", "partial"}:
                    return directory
            time.sleep(2)
        raise TimeoutError("后台浏览器未在15分钟内完成；请检查Chrome登录和扩展状态。")
    finally:
        if only_manual:
            with host_locked(root):
                current = read_json(path)
                if current.get("request_id") == request_id:
                    # No leftover reader may reopen pages on the next browser start.
                    write(path, {**current, "enabled": False})


def next_id(catalog, date):
    prefix = "intel-" + date.replace("-", "") + "-"
    used = [int(r["id"][len(prefix):]) for r in catalog["records"] if r["id"].startswith(prefix)]
    return prefix + f"{max(used, default=0) + 1:03d}"


def new_source_known(state, capture):
    key = source_key(capture["conversation_id"], capture["turn_key"])
    entry = state.get("source_occurrences", {}).get(key)
    if entry is None:
        return False
    target = state["occurrences"].get(entry["target_occurrence_key"])
    canonical = sha(intake_state.normalize(capture["original"]).encode("utf-8"))
    if (not target or entry["category_id"] != capture["category_id"] or
            entry["conversation_id"] != capture["conversation_id"] or
            entry["turn_key"] != capture["turn_key"] or
            instant(entry["generated_at"]) != instant(capture["generated_at"]) or
            target["normalized_sha256"] != canonical or entry["normalized_sha256"] != canonical):
        raise ValueError("Previously imported source turn has changed; needs review")
    return True


def validated_summary(root, capture, category, output_dir, config):
    from background_summary import summarize, load_validated
    digest = sha(capture["raw_bytes"])
    cached = config.get("validated_summary_cache", {}).get(digest)
    card = (load_validated(capture["raw_bytes"], category, safe(root, cached), root=root) if cached else
            summarize(capture["raw_bytes"], category, output_dir, root=root))
    # Markdown destinations can contain escaped punctuation (for example \&).
    # Decode that syntax for clickable sources; the full original stays bytes.
    for source in card["sources"]:
        source["url"] = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~])", r"\1", source["url"])
    return card


def capture_fingerprint(batch):
    return (batch["run_sha256"], sorted((c["conversation_id"], c["turn_key"], c["generated_at"],
            sha(c["raw_bytes"]), c["metadata_sha256"]) for c in batch["captures"]))


def prepare_transaction(root, run_id, capture_batch, config):
    from background_capture import legacy_proofs, load_capture
    root = Path(root).resolve()
    directory = safe(root, ".intake/background/runs/" + run_id)
    directory.mkdir(parents=True, exist_ok=False)
    baseline = snapshot(root)
    catalog = read_json(safe(root, "data/catalog.json"))
    state = intake_state.read_state(root)
    captures = capture_batch["captures"]
    remaining = [c for c in captures if not new_source_known(state, c)]
    proofs = legacy_proofs(root, state)
    alias_plan = plan_aliases(state, remaining, proofs)
    if alias_plan["status"] != "ready":
        write(directory / "alias-errors.json", alias_plan["errors"])
        raise ValueError("旧报告身份对应存在歧义；未改动正式资料。")
    for alias in alias_plan["aliases"].values():
        if any(v["target_occurrence_key"] == alias["target_occurrence_key"] and
               v["turn_key"] != alias["turn_key"] for v in state.get("source_occurrences", {}).values()):
            raise ValueError("Legacy minute target already belongs to a different source turn")
    write(directory / "alias-plan.json", alias_plan)
    stage = directory / "staging"
    for path in [*baseline, "ui/template.html", "ui/styles.css", "ui/app.js", "PROJECT.md", "AGENTS.md", "使用说明.txt", "scripts/build.py"]:
        source = safe(root, path)
        dest = stage / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    staged_state = deepcopy(state)
    staged_state.setdefault("source_aliases", {}).update(alias_plan["aliases"])
    intake_state.save(stage, staged_state)
    known_keys = {v["source_key"] for v in alias_plan["known"]}
    complete = set(capture_batch["complete_categories"])
    failures = deepcopy(capture_batch["failed_categories"])
    imported, duplicates, decisions = [], [], []
    summary_attempts = 0
    category_map = {c["id"]: c for c in catalog["categories"]}
    for capture in captures:
        source_id = source_key(capture["conversation_id"], capture["turn_key"])
        if source_id in known_keys or new_source_known(state, capture):
            decisions.append({"source_key": source_id, "status": "known"})
            continue
        local = stage / capture["capture_file"]
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(capture["raw_bytes"])
        generated = instant(capture["generated_at"]).isoformat()
        args = SimpleNamespace(command="check", file=capture["capture_file"], category=capture["category_id"], generated_at=generated)
        check = intake_state.execute(args, stage)
        if check["status"] == "known":
            # An old exact-second occurrence may already exist, but cannot be
            # claimed by two distinct newly observed source turns.
            owners = [v for v in staged_state.get("source_occurrences", {}).values()
                      if v["target_occurrence_key"] == check["occurrence_key"]]
            if owners and any(v["turn_key"] != capture["turn_key"] for v in owners):
                raise ValueError("Two different source turns share one ledger timestamp identity")
            record_id = check["existing_id"]
            outcome = staged_state["occurrences"][check["occurrence_key"]]["outcome"]
        elif check["status"] == "duplicate":
            record_id, outcome = check["existing_id"], "duplicate"
            duplicates.append(record_id)
        else:
            if summary_attempts >= MAX_NEW_PER_RUN:
                complete.discard(capture["category_id"])
                failures[capture["category_id"]] = {"reason": "summary_batch_limit", "error": "其余新报告将在下一轮继续处理。"}
                continue
            summary_attempts += 1
            try:
                card = validated_summary(root, capture, category_map[capture["category_id"]], directory / "summaries" / source_id, config)
            except Exception as error:
                complete.discard(capture["category_id"])
                failures[capture["category_id"]] = {"reason": "summary_failed", "error": str(error)[:300]}
                decisions.append({"source_key": source_id, "status": "summary_failed"})
                continue
            collected = now().date().isoformat()
            record_id = next_id(catalog, collected)
            original_file = "data/originals/" + record_id + ".md"
            target = stage / original_file
            with target.open("xb") as handle:
                handle.write(capture["raw_bytes"])
            normalized = intake_state.normalize(capture["original"])
            prior_records = build.load_library(stage)["records"]
            related = [r["id"] for r in prior_records if r["category_id"] == capture["category_id"] and
                       SequenceMatcher(None, normalized, intake_state.normalize(r["original"]), autojunk=False).ratio() >= .65][:5]
            card.update(id=record_id, collected_at=collected, original_file=original_file,
                        original_sha256=sha(capture["raw_bytes"]), related_ids=related,
                        source_capture={"generated_at": generated, "source_task": capture["source_task"],
                                        "conversation_id": capture["conversation_id"], "turn_key": capture["turn_key"],
                                        "capture_file": capture["capture_file"], "metadata_file": capture["metadata_file"],
                                        "capture_run": capture_batch["run_dir"], "method": "background_extension"})
            catalog["records"].append(card)
            catalog["updated_at"] = collected
            write(stage / "data/catalog.json", catalog)
            # Validate complete raw bytes and card schema before any ledger mark.
            build.load_library(stage)
            imported.append(record_id)
            outcome = "imported"
        if check["status"] != "known":
            intake_state.mutate(SimpleNamespace(command="mark", file=capture["capture_file"], category=capture["category_id"],
                                generated_at=generated, run_id=run_id, record_id=record_id, outcome=outcome), stage)
        staged_state = intake_state.read_state(stage)
        staged_state.setdefault("source_occurrences", {})[source_id] = {
            "category_id": capture["category_id"], "conversation_id": capture["conversation_id"],
            "turn_key": capture["turn_key"], "generated_at": capture["generated_at"],
            "normalized_sha256": check["normalized_sha256"], "raw_sha256": sha(capture["raw_bytes"]),
            "target_occurrence_key": check["occurrence_key"], "capture_file": capture["capture_file"]}
        intake_state.save(stage, staged_state)
        decisions.append({"source_key": source_id, "status": check["status"], "record_id": record_id})
    for category in complete:
        intake_state.mutate(SimpleNamespace(command="checkpoint", category=category,
                            through=instant(capture_batch["scan_end"]).isoformat(), run_id=run_id), stage)
    pending = intake_state.pending_notifications(intake_state.read_state(stage))
    notifications = [{"occurrence_key": v["occurrence_key"], "existing_id": v["existing_id"],
                      "message": "新出现的报告与已有记录重复，已跳过新建卡片。"} for v in pending]
    # A failed read with zero progress must not look like a partial success.
    # Saved reports/duplicate decisions still count as durable progress even
    # when a later report prevents that category's checkpoint from advancing.
    status = "complete" if len(complete) == 6 else "partial" if complete or imported or duplicates else "failed"
    summary = f"新增 {len(imported)} 篇，跳过已处理 {sum(v['status']=='known' for v in decisions)} 篇，新重复 {len(duplicates)} 篇；完成 {len(complete)}/6 类检查。"
    result = {"run_id": run_id, "status": status, "summary": summary, "finished_at": now().isoformat(),
              "capture_run": capture_batch["run_dir"], "scan_start": capture_batch["scan_start"], "scan_end": capture_batch["scan_end"],
              "imported_ids": imported, "duplicate_ids": duplicates, "duplicate_notifications": notifications,
              "complete_categories": sorted(complete), "failures": failures, "decisions": decisions}
    catalog["last_intake"] = {k: result[k] for k in ("run_id", "status", "summary", "finished_at", "duplicate_notifications", "failures")}
    catalog["last_intake"]["result_file"] = f"evidence/daily/{run_id}/result.json"
    notices = {n["occurrence_key"]: n for n in catalog.get("intake_notifications", [])}
    notices.update({n["occurrence_key"]: {**n, "recorded_at": result["finished_at"]} for n in notifications})
    catalog["intake_notifications"] = list(notices.values())
    write(stage / "data/catalog.json", catalog)
    # User actions are independent immutable events. Render one observed
    # snapshot, but never pin or overwrite live events in this transaction.
    from management_state import load_management
    build.build(stage, management_snapshot=load_management(root))
    # All old cards and original bytes must remain unchanged.
    prior = read_json(root / "data/catalog.json")
    if catalog["records"][:len(prior["records"])] != prior["records"]:
        raise ValueError("An existing card was modified")
    for record in prior["records"]:
        name = record["original_file"]
        if sha((stage / name).read_bytes()) != baseline[name]:
            raise ValueError("An existing original was modified")
    writes = [r["original_file"] for r in catalog["records"] if r["id"] in imported]
    writes += ["data/catalog.json", build.OUTPUT, "evidence/build-manifest.json", "data/intake-state.json"]
    result_path = f"evidence/daily/{run_id}/result.json"
    write(stage / result_path, result)
    writes.append(result_path)
    operations = [{"path": p, "old_sha256": baseline.get(p), "new_sha256": sha((stage / p).read_bytes()),
                   "prepared_file": (stage / p).relative_to(root).as_posix()} for p in writes]
    journal = {"schema_version": 1, "run_id": run_id, "status": "prepared", "baseline": baseline,
               "operations": operations, "result_file": result_path}
    with intake_state.locked(root):
        verify_snapshot(root, baseline)
        if capture_fingerprint(load_capture(safe(root, capture_batch["run_dir"]), root=root)) != capture_fingerprint(capture_batch):
            raise ValueError("Captured source evidence changed during summary preparation")
        # Recompute legacy decisions under the final write lock.
        current = intake_state.read_state(root)
        repeated = plan_aliases(current, remaining, legacy_proofs(root, current))
        if repeated != alias_plan:
            raise ValueError("Alias plan changed before commit")
        write(directory / "journal.json", journal)
        apply_transaction(root, directory / "journal.json")
    return result


def apply_transaction(root, journal_path):
    """Caller holds the ledger lock. Refuse drift before writing any operation."""
    root = Path(root).resolve()
    journal = read_json(journal_path)
    if journal["status"] == "committed":
        return read_json(safe(root, journal["result_file"]))
    if journal.get("schema_version") != 1 or journal["status"] != "prepared":
        raise ValueError("Invalid transaction journal")
    for op in journal["operations"]:
        target, prepared = safe(root, op["path"]), safe(root, op["prepared_file"])
        if sha(prepared.read_bytes()) != op["new_sha256"]:
            raise ValueError("Prepared transaction data changed")
        actual = sha(target.read_bytes()) if target.exists() else None
        if actual not in {op["old_sha256"], op["new_sha256"]}:
            raise ValueError("Transaction target changed; refusing overwrite: " + op["path"])
    changed_paths = {op["path"] for op in journal["operations"]}
    verify_snapshot(root, {p: h for p, h in journal["baseline"].items() if p not in changed_paths})
    for op in journal["operations"]:
        target = safe(root, op["path"])
        if target.exists() and sha(target.read_bytes()) == op["new_sha256"]:
            continue
        build.atomic_write(target, safe(root, op["prepared_file"]).read_bytes())
    build.load_library(root)
    for op in journal["operations"]:
        if sha(safe(root, op["path"]).read_bytes()) != op["new_sha256"]:
            raise ValueError("Post-commit verification failed")
    journal.update(status="committed", committed_at=now().isoformat())
    write(journal_path, journal)
    return read_json(safe(root, journal["result_file"]))


def recover_transactions(root, *, acknowledge_intake=True):
    root = Path(root).resolve()
    directory = safe(root, ".intake/background/runs")
    if not directory.exists():
        return
    for journal_path in sorted(directory.glob("*/journal.json")):
        journal = read_json(journal_path)
        if journal["status"] == "prepared":
            with intake_state.locked(root):
                apply_transaction(root, journal_path)
    if not acknowledge_intake:
        return
    # A result saved before a crash is the only basis for acknowledging a notice.
    state = intake_state.read_state(root)
    for item in intake_state.pending_notifications(state):
        result = safe(root, f"evidence/daily/{item['first_marked_in_run_id']}/result.json")
        if result.exists():
            intake_state.execute(SimpleNamespace(command="ack-notifications", result_file=result.relative_to(root).as_posix(),
                                                 run_id=item["first_marked_in_run_id"]), root)


def run_intake(root, run_id):
    from background_capture import load_capture
    root = Path(root).resolve()
    if not re.fullmatch(r"bg-[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("Invalid background run ID")
    config = read_json(safe(root, ".intake/background/config.json"))
    if config.get("schema_version") != 1 or config.get("enabled") is not True:
        raise ValueError("Background intake is not enabled")
    recover_transactions(root)
    capture = load_capture(request_capture(root, run_id), root=root)
    result = prepare_transaction(root, run_id, capture, config)
    if result["duplicate_notifications"]:
        intake_state.execute(SimpleNamespace(command="ack-notifications", result_file=f"evidence/daily/{run_id}/result.json", run_id=run_id), root)
    write(safe(root, "evidence/background-latest.json"), result)
    return result
