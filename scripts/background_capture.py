"""Validate local browser evidence and positive legacy minute-time proofs.

No browser operations, writes, scheduling, or ledger mutations occur here.
``load_capture`` returns only complete categories in ``captures``; any captured
text from incomplete categories is separately labelled ``incomplete_captures``.
Existing probe indexes do not store a metadata-file digest: metadata hashes are
returned for subsequent transaction pinning, with source/time/body cross-checks.
"""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from intake_alias import SOURCES, _time, UI_MINUTE
from intake_state import digest, normalize

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-f]{64}")
ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class CaptureError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise CaptureError(message)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(CaptureError("invalid_json_number")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CaptureError("invalid_utf8_json") from exc


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n").encode()


def _sha(value):
    require(isinstance(value, str) and SHA.fullmatch(value), "invalid_sha256")
    return value


def _safe(root, value):
    root = Path(root).absolute()
    require(isinstance(value, (str, Path)), "invalid_path")
    value = Path(value)
    require(".." not in value.parts, "path_traversal")
    target = value if value.is_absolute() else root / value
    require(target.is_relative_to(root), "path_outside_project")
    for path in [root, *reversed(target.parents), target]:
        if not path.is_relative_to(root):
            continue
        info = path.lstat()
        require(not path.is_symlink() and not (getattr(info, "st_file_attributes", 0) & 0x400), "linked_evidence_path")
    require(target.resolve().is_relative_to(root.resolve()), "resolved_path_outside_project")
    return target


def _read(root, path):
    resolved = _safe(root, path)
    require(resolved.is_file(), "expected_evidence_file")
    return resolved.read_bytes()


def _time_checked(value):
    try:
        return _time(value)
    except (ValueError, TypeError) as exc:
        raise CaptureError("invalid_timestamp") from exc


def _identity(identity):
    require(isinstance(identity, dict) and set(identity) == {"category_id", "conversation_id", "turn_key"}, "invalid_identity")
    category = identity["category_id"]
    require(category in SOURCES and identity["conversation_id"] == SOURCES[category][1], "fixed_source_mismatch")
    turn = identity["turn_key"]
    require(isinstance(turn, str) and 0 < len(turn) <= 256 and not any(ord(c) < 32 for c in turn), "invalid_turn_key")
    return digest(_json_bytes(identity))


def load_capture(run_dir, root=ROOT):
    """Return a validated finished run; invalid evidence raises CaptureError.

    Result: run, run_sha256, run_dir (relative), status, scan_start, scan_end,
    complete_categories, failed_categories ({category_id: coverage/reason}),
    captures, incomplete_captures. Each capture extends intake_alias input with
    raw_bytes, body_file/metadata_file (relative), meta, metadata_sha256 and identity_sha256.
    No checkpoint decision is made here; partial status always remains partial.
    """
    root = Path(root).absolute()
    folder = _safe(root, run_dir)
    require(folder.is_dir() and folder.parent == root / "evidence/extension-probe", "invalid_run_directory")
    raw_run = _read(root, folder / "run.json")
    run = _json(raw_run)
    require(isinstance(run, dict) and run.get("schema_version") == 1 and run.get("mode") == "probe", "invalid_run_schema")
    request = run.get("request_id")
    require(isinstance(request, str) and ID.fullmatch(request), "invalid_request_id")
    expected_run = "extension-probe-" + digest(request.encode())[:24]
    require(run.get("run_id") == folder.name == expected_run, "request_run_id_mismatch")
    _sha(run.get("config_sha256"))
    require("diagnostic" not in run, "visible_diagnostic_not_intake")
    require(run.get("status") in {"complete", "partial"}, "run_not_finished")
    start, end = _time_checked(run.get("scan_start")), _time_checked(run.get("scan_end"))
    created, finished = _time_checked(run.get("created_at")), _time_checked(run.get("finished_at"))
    updated = _time_checked(run.get("updated_at"))
    require(start <= end <= created <= finished <= updated, "invalid_run_time_order")
    if "activation" in run:
        activation = run["activation"]
        require(isinstance(activation, dict) and activation.get("required_event") == activation.get("trigger_event") == "startup"
                and _time_checked(activation.get("observed_at")) == created, "invalid_startup_activation")
    sources = run.get("sources")
    require(isinstance(sources, list) and len(sources) == len(SOURCES), "six_fixed_sources_required")
    seen = set()
    for source in sources:
        require(isinstance(source, dict), "invalid_source")
        category = source.get("category_id")
        require(category in SOURCES and category not in seen
                and (source.get("task_name"), source.get("conversation_id")) == SOURCES[category], "fixed_source_mismatch")
        seen.add(category)
    categories = run.get("categories")
    require(isinstance(categories, dict) and set(categories).issubset(SOURCES), "invalid_category_coverage")
    complete, failures = [], {}
    for category in SOURCES:
        coverage = categories.get(category)
        if coverage is None:
            failures[category] = {"complete": False, "reason": "missing_category_coverage"}
            continue
        require(isinstance(coverage, dict) and type(coverage.get("complete")) is bool
                and isinstance(coverage.get("reason"), str) and coverage["reason"], "invalid_category_coverage")
        dates = coverage.get("dates", [])
        require(isinstance(dates, list) and len(dates) <= 10000, "invalid_coverage_dates")
        parsed_dates = [_time_checked(value) for value in dates]
        # The native host accepts early failures before page counts exist.
        # Keep that absence as unknown; only completed coverage must prove a
        # count. Explicitly supplied invalid values remain corrupt evidence.
        if coverage["complete"] or "page_count" in coverage:
            require(type(coverage.get("page_count")) is int and 0 <= coverage["page_count"] <= 10000, "invalid_page_count")
        boundary = _time_checked(coverage["boundary_time"]) if coverage.get("boundary_time") is not None else None
        if coverage["complete"]:
            require(not coverage.get("error"), "completed_category_has_error")
            if coverage["reason"] == "boundary_before_start":
                require(boundary is not None and boundary < start and boundary in parsed_dates, "unproven_history_boundary")
            elif coverage["reason"] == "conversation_start":
                require(coverage.get("reached_start") is True, "unproven_conversation_start")
            else:
                raise CaptureError("invalid_complete_reason")
            complete.append(category)
        else:
            failures[category] = coverage
    require((run["status"] == "complete") == (len(complete) == len(SOURCES)), "run_status_coverage_mismatch")

    reports, skipped = run.get("reports"), run.get("skipped")
    require(isinstance(reports, dict) and isinstance(skipped, dict), "invalid_reports_index")
    expected_files, captures, incomplete = set(), [], []
    for key, index in reports.items():
        _sha(key)
        require(isinstance(index, dict) and index.get("category_id") in SOURCES, "invalid_report_index")
        require(index.get("body_file") == f"reports/{key}.md" and index.get("metadata_file") == f"reports/{key}.json", "report_path_mismatch")
        body_path, meta_path = folder / index["body_file"], folder / index["metadata_file"]
        expected_files.update((body_path.name, meta_path.name))
        raw, raw_meta = _read(root, body_path), _read(root, meta_path)
        meta = _json(raw_meta)
        require(isinstance(meta, dict) and meta.get("schema_version") == 1 and meta.get("is_report") is True, "invalid_report_metadata")
        require(_identity(meta.get("identity")) == meta.get("identity_sha256") == key, "report_identity_hash_mismatch")
        identity = meta["identity"]
        category = identity["category_id"]
        require(category == index["category_id"] and meta.get("title") == SOURCES[category][0]
                and meta.get("path") == "/c/" + SOURCES[category][1], "report_source_mismatch")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise CaptureError("original_not_utf8") from exc
        require(text.strip() and "\0" not in text and len(raw) < 2 * 1024 * 1024, "invalid_original")
        body_hash = digest(raw)
        require(body_hash == _sha(meta.get("original_sha256")) == _sha(index.get("original_sha256")), "original_sha256_mismatch")
        require(type(meta.get("original_bytes")) is int and meta["original_bytes"] == len(raw), "original_length_mismatch")
        metadata_hash = digest(raw_meta)
        if "metadata_sha256" in index:
            require(metadata_hash == _sha(index["metadata_sha256"]), "metadata_sha256_mismatch")
        generated = _time_checked(meta.get("report_time"))
        require(generated == _time_checked(index.get("report_time")) and start <= generated <= end, "report_outside_scan_or_time_mismatch")
        require(meta.get("report_date_beijing") == generated.date().isoformat(), "report_generation_date_mismatch")
        require(created <= _time_checked(meta.get("captured_at")) <= finished, "capture_outside_run")
        guard = meta.get("guard")
        require(isinstance(guard, dict) and guard.get("installed_early") is True, "copy_guard_not_installed_early")
        for field, expected in (("copy_intercepted", 1), ("real_clipboard_calls", 0), ("clipboard_read_calls", 0), ("legacy_copy_calls", 0)):
            require(type(guard.get(field)) is int and guard[field] == expected, "copy_guard_failed")
        require(type(meta.get("table_count")) is int and 0 <= meta["table_count"] <= 10000, "invalid_table_count")
        links = meta.get("links")
        require(isinstance(links, list) and len(links) <= 10000, "invalid_links")
        for link in links:
            url = link if isinstance(link, str) else link.get("href", link.get("url")) if isinstance(link, dict) else None
            require(isinstance(url, str) and len(url) <= 8192, "invalid_link")
            try:
                parsed = urlsplit(url)
                require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password, "unsafe_link")
            except ValueError as exc:
                raise CaptureError("unsafe_link") from exc
        history = meta.get("history_dates", [])
        require(isinstance(history, list) and len(history) <= 10000, "invalid_report_history")
        for value in history:
            _time_checked(value)
        capture = {**identity, "source_task": SOURCES[category][0], "generated_at": meta["report_time"],
                   "original": text, "capture_file": body_path.relative_to(root).as_posix(),
                   "raw_sha256": body_hash, "normalized_sha256": digest(normalize(text).encode()),
                   "raw_bytes": raw, "body_file": body_path.relative_to(root).as_posix(), "meta": meta,
                   "metadata_file": meta_path.relative_to(root).as_posix(),
                   "metadata_sha256": metadata_hash, "identity_sha256": key}
        (captures if category in complete else incomplete).append(capture)
    if reports or (folder / "reports").exists():
        report_folder = _safe(root, folder / "reports")
        require({p.name for p in report_folder.iterdir()} == expected_files, "unindexed_report_files")
    for key, item in skipped.items():
        require(isinstance(item, dict), "invalid_skipped_identity")
        identity = {field: item.get(field) for field in ("category_id", "conversation_id", "turn_key")}
        require(_identity(identity) == key and key not in reports and item.get("reason") == "not_report", "skipped_identity_conflict")
        require(created <= _time_checked(item.get("seen_at")) <= finished, "skipped_outside_run")
    return {"run": run, "run_sha256": digest(raw_run), "run_dir": folder.relative_to(root).as_posix(),
            "status": run["status"], "scan_start": run["scan_start"], "scan_end": run["scan_end"],
            "complete_categories": complete, "failed_categories": failures,
            "captures": captures, "incomplete_captures": incomplete}


def _ui_generated(value, captured_day):
    require(isinstance(value, str), "missing_minute_ui_timestamp")
    match = UI_MINUTE.search(value)
    require(match is not None, "minute_only_ui_timestamp_required")
    hour, minute = int(match[1]), int(match[2])
    prefix = value[:match.start()].strip()
    if prefix in {"今天", "昨天", "前天"}:
        day = captured_day - timedelta(days={"今天": 0, "昨天": 1, "前天": 2}[prefix])
    elif prefix in {"星期" + char for char in "一二三四五六日天"}:
        weekday = "一二三四五六日".index(prefix[2:].replace("天", "日"))
        day = captured_day - timedelta(days=(captured_day.weekday() - weekday) % 7)
    else:
        parsed = re.fullmatch(r"(\d{4})(?:年|-)(\d{1,2})(?:月|-)(\d{1,2})日?", prefix)
        require(parsed is not None, "unsupported_ui_date_label")
        day = date(*(int(part) for part in parsed.groups()))
    return _time_checked(f"{day.isoformat()}T{hour:02d}:{minute:02d}:00+08:00")


def legacy_proofs(root, state):
    """Read and bind actual minute-only UI evidence; never infer it from :00.

    Returns the occurrence-keyed proof map consumed by plan_aliases. Entries
    without any positive legacy UI evidence are omitted. Contradictory or corrupt
    evidence raises CaptureError. Each supporting file is pinned by SHA-256.
    """
    root = Path(root).absolute()
    catalog_raw = _read(root, "data/catalog.json")
    catalog = _json(catalog_raw)
    records = {r["id"]: r for r in catalog["records"]}
    require(len(records) == len(catalog["records"]), "duplicate_catalog_ids")
    proofs = {}
    for key, entry in state["occurrences"].items():
        when = _time_checked(entry["generated_at"])
        if when.second or when.microsecond or "." in entry["generated_at"] or entry.get("generated_at_precision", "minute") != "minute":
            continue
        record = records.get(entry.get("record_id"))
        require(record is not None, "legacy_record_missing")
        source_capture = record.get("source_capture") or {}
        sidecar = root / (entry["capture_file"] + ".json")
        evidence_name = source_capture.get("metadata_file") or source_capture.get("evidence_file")
        if sidecar.exists():
            evidence_name = sidecar.relative_to(root).as_posix()
        if not evidence_name:
            continue
        evidence_raw = _read(root, evidence_name)
        evidence = _json(evidence_raw)
        ui = evidence.get("ui_timestamp") if isinstance(evidence, dict) else source_capture.get("ui_timestamp")
        if not ui:
            continue
        category = entry["category_id"]
        require(category in SOURCES and record.get("category_id") == category and record.get("source_task") == SOURCES[category][0], "legacy_source_mismatch")
        capture_raw = _read(root, entry["capture_file"])
        require(digest(capture_raw) == _sha(entry["raw_sha256"]), "legacy_capture_sha256_mismatch")
        require(digest(normalize(capture_raw.decode("utf-8")).encode()) == _sha(entry["normalized_sha256"]), "legacy_canonical_hash_mismatch")
        original_raw = _read(root, record["original_file"])
        require(digest(original_raw) == _sha(record["original_sha256"]), "catalog_original_sha256_mismatch")
        require(digest(normalize(original_raw.decode("utf-8")).encode()) == entry["normalized_sha256"], "legacy_record_body_mismatch")
        supporting = {entry["capture_file"]: digest(capture_raw), record["original_file"]: digest(original_raw)}
        if isinstance(evidence, list):
            # First import used a separate UI inventory. Bind its minute label
            # to the copied body's header through a matching SHA-pinned sidecar.
            require(source_capture.get("timezone") == "Asia/Shanghai"
                    and _time_checked(source_capture.get("generated_at")) == when, "legacy_source_time_mismatch")
            parent = _safe(root, Path(evidence_name).parent)
            candidates = []
            for candidate in parent.glob("*.md.json"):
                candidate_raw = _read(root, candidate)
                item = _json(candidate_raw)
                if isinstance(item, dict) and item.get("sha256") == entry["raw_sha256"]:
                    require(item.get("category_id") == category and item.get("task_name") == SOURCES[category][0], "legacy_metadata_source_mismatch")
                    matching = [row for row in evidence if row.get("ui_timestamp") == ui and row.get("header") == item.get("header")]
                    if len(matching) == 1:
                        candidates.append((candidate, item, candidate_raw))
            require(len(candidates) == 1, "legacy_inventory_body_binding_ambiguous")
            candidate, metadata, candidate_raw = candidates[0]
            old_body = _safe(root, candidate.parent / metadata["file"])
            require(digest(_read(root, old_body)) == entry["raw_sha256"], "legacy_metadata_body_mismatch")
            supporting[candidate.relative_to(root).as_posix()] = digest(candidate_raw)
            captured_day = _time_checked(source_capture["captured_at"]).date()
        else:
            require(isinstance(evidence, dict) and evidence.get("category_id") == category
                    and evidence.get("task_name") == SOURCES[category][0], "legacy_metadata_source_mismatch")
            require(evidence.get("sha256") == entry["raw_sha256"], "legacy_metadata_sha256_mismatch")
            body = _safe(root, Path(evidence_name).parent / evidence["file"])
            require(digest(_read(root, body)) == entry["raw_sha256"], "legacy_metadata_body_mismatch")
            capture_date = evidence.get("capture_date")
            require(isinstance(capture_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", capture_date), "invalid_legacy_capture_date")
            try:
                captured_day = date.fromisoformat(capture_date)
            except ValueError as exc:
                raise CaptureError("invalid_legacy_capture_date") from exc
            if source_capture:
                require(source_capture.get("timezone") == "Asia/Shanghai"
                        and _time_checked(source_capture.get("generated_at")) == when
                        and source_capture.get("ui_timestamp") == ui, "legacy_source_time_mismatch")
        require(_ui_generated(ui, captured_day) == when, "legacy_ui_date_mismatch")
        proofs[key] = {"category_id": category, "source_task": SOURCES[category][0], "conversation_id": SOURCES[category][1],
                       "generated_at": entry["generated_at"], "precision": "minute", "ui_timestamp": ui,
                       "evidence_file": Path(evidence_name).as_posix(), "evidence_sha256": digest(evidence_raw),
                       "supporting_evidence": supporting}
    return proofs
