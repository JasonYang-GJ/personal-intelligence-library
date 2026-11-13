"""Read-only, fail-closed plans linking proven minute-resolution legacy reports.

``plan_aliases(state, captures, legacy_proofs)`` never reads or writes files.
The caller must supply a complete capture batch, verify each proof file/hash,
then re-run the plan under the intake ledger lock before committing aliases.
Proofs are keyed by the original occurrence key and explicitly bind the fixed
source, minute-only UI timestamp, generated time and evidence file/hash. Seconds
equal to zero alone are not proof. No ledger entry or checkpoint is rewritten.

Capture dictionaries require category_id, source_task, conversation_id,
turn_key, generated_at (timezone required), original (complete copied text),
and capture_file. Optional raw_sha256/normalized_sha256 are verified if supplied.
An additive ``state['source_aliases']`` map can hold returned aliases. Notification
decisions always read the original target entry, so later acknowledgements apply.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

from intake_state import digest, normalize, notification_pending


SOURCES = {
    "ai-models": ("AI模型新发布", "00000000-0000-4000-8000-000000000010"),
    "ai-products": ("AI产品机会雷达", "00000000-0000-4000-8000-000000000008"),
    "platform-income": ("平台变现机会雷达", "00000000-0000-4000-8000-000000000009"),
    "user-pains": ("真实用户痛点雷达", "00000000-0000-4000-8000-000000000007"),
    "solo-business": ("个人创业机会雷达", "00000000-0000-4000-8000-000000000011"),
    "cross-border": ("跨境电商入场雷达", "00000000-0000-4000-8000-000000000012"),
}
BEIJING = timezone(timedelta(hours=8))
SHA = re.compile(r"[0-9a-f]{64}")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+08:00)")
UI_MINUTE = re.compile(r"(?:^|\s)(\d{1,2}):(\d{2})$")


def _time(value):
    if not isinstance(value, str) or not TIME.fullmatch(value):
        raise ValueError("timestamp_requires_seconds_and_Z_or_Beijing_offset")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BEIJING)


def _source(item):
    category = item.get("category_id")
    if category not in SOURCES or (item.get("source_task"), item.get("conversation_id")) != SOURCES[category]:
        raise ValueError("source_not_allowlisted_for_category")


def _hash(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("invalid_sha256")
    return value


def _relative_path(value):
    if (not isinstance(value, str) or not value.strip() or "\\" in value or ":" in value
            or value.startswith("/") or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ValueError("evidence_path_must_be_project_relative")
    return value


def _bucket(item):
    return (item["category_id"], item["conversation_id"], item["normalized_sha256"],
            _time(item["generated_at"]).strftime("%Y-%m-%dT%H:%M"))


def source_key(conversation_id, turn_key):
    """Stable source identity; report time is intentionally not rounded or keyed."""
    if not all(isinstance(v, str) and v.strip() and "\n" not in v for v in (conversation_id, turn_key)):
        raise ValueError("invalid_source_identity")
    return digest(f"{conversation_id}\n{turn_key}".encode("utf-8"))


def _capture(item):
    _source(item)
    when = _time(item["generated_at"])
    original = item["original"]
    if not isinstance(original, str) or not original.strip():
        raise ValueError("empty_original")
    raw = digest(original.encode("utf-8"))
    canonical = digest(normalize(original).encode("utf-8"))
    for field, computed in (("raw_sha256", raw), ("normalized_sha256", canonical)):
        if field in item and item[field] != computed:
            raise ValueError(f"{field}_mismatch")
    return {"source_key": source_key(item["conversation_id"], item["turn_key"]),
            **{k: item[k] for k in ("category_id", "source_task", "conversation_id", "turn_key", "generated_at")},
            "generated_at_beijing_exact": when.isoformat(), "raw_sha256": raw,
            "normalized_sha256": canonical, "capture_file": _relative_path(item["capture_file"])}


def _proof(key, proof, occurrences):
    _source(proof)
    entry = occurrences.get(key)
    if not entry:
        raise ValueError("proof_target_missing")
    when = _time(entry["generated_at"])
    if (proof.get("precision") != "minute" or entry.get("generated_at_precision", "minute") != "minute"
            or when.second != 0 or when.microsecond != 0 or "." in entry["generated_at"]):
        raise ValueError("legacy_time_not_proven_minute_precision")
    if entry["category_id"] != proof["category_id"] or _time(proof["generated_at"]) != when:
        raise ValueError("proof_target_mismatch")
    for field in ("conversation_id", "source_task"):
        if field in entry and entry[field] != proof[field]:
            raise ValueError("proof_source_mismatch")
    ui = proof.get("ui_timestamp", "")
    match = UI_MINUTE.search(ui) if isinstance(ui, str) else None
    if not match or (int(match[1]), int(match[2])) != (when.hour, when.minute):
        raise ValueError("minute_only_ui_time_required")
    _relative_path(proof["evidence_file"])
    _hash(proof["evidence_sha256"])
    return {**entry, "conversation_id": proof["conversation_id"]}


def _resolution(alias, entry):
    return {"source_key": alias["source_key"], "occurrence_key": alias["target_occurrence_key"],
            "record_id": entry["record_id"], "identity_resolution": "legacy_minute_alias",
            "notification_pending": notification_pending(entry),
            "target_entry": deepcopy(entry)}


def plan_aliases(state, captures, legacy_proofs):
    """Return aliases/known/unmatched or errors, without mutating any input.

    Any error suppresses all actionable output. Repeated identical capture input
    is harmless; different content/time for one source turn, multiple old targets,
    or two distinct turns in one eligible source/hash/minute bucket are errors.
    Missing precision evidence gives an unmatched report, never an assumed alias.
    """
    result = {"status": "ready", "aliases": {}, "known": [], "unmatched": [], "errors": []}

    def error(code, **context):
        result["errors"].append({"code": code, **context})

    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or state.get("normalization_version") != 1 or not isinstance(state.get("occurrences"), dict)
            or not isinstance(state.get("source_aliases", {}), dict)
            or not isinstance(captures, list) or not isinstance(legacy_proofs, dict)):
        return {**result, "status": "error", "errors": [{"code": "invalid_input_schema"}]}
    occurrences = state["occurrences"]
    all_old = defaultdict(list)
    eligible = defaultdict(list)
    incoming = {}
    incoming_buckets = defaultdict(set)
    existing = state.get("source_aliases", {})
    existing_buckets = defaultdict(set)

    for key, entry in occurrences.items():
        try:
            category = entry["category_id"]
            if category not in SOURCES:
                raise ValueError("unknown_legacy_category")
            _hash(entry["normalized_sha256"])
            _time(entry["generated_at"])
            if entry.get("outcome") not in {"imported", "duplicate"} or not entry.get("record_id"):
                raise ValueError("invalid_legacy_outcome")
            all_old[_bucket({**entry, "conversation_id": SOURCES[category][1]})].append(key)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            error(str(exc), occurrence_key=key)

    for key, proof in legacy_proofs.items():
        try:
            eligible[_bucket(_proof(key, proof, occurrences))].append(key)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            error(str(exc), occurrence_key=key)

    for index, item in enumerate(captures):
        try:
            capture = _capture(item)
            key = capture["source_key"]
            previous = incoming.get(key)
            if previous:
                # Equivalent UTC/local notation denotes one instant. Preserve
                # the first exact observed timestamp and capture path.
                if (_bucket(previous) != _bucket(capture) or _time(previous["generated_at"]) != _time(capture["generated_at"])
                        or previous["raw_sha256"] != capture["raw_sha256"]):
                    raise ValueError("conflicting_repeated_source_turn")
            else:
                incoming[key] = capture
                incoming_buckets[_bucket(capture)].add(key)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            error(str(exc), capture_index=index)

    for key, alias in existing.items():
        try:
            _source(alias)
            if alias["source_key"] != key or source_key(alias["conversation_id"], alias["turn_key"]) != key:
                raise ValueError("stored_alias_identity_mismatch")
            target = alias["target_occurrence_key"]
            occurrences[target]
            if target not in legacy_proofs:
                raise ValueError("stored_alias_proof_missing")
            if (alias.get("normalization_version") != 1 or alias.get("alias_version") != 1
                    or _bucket(alias) != _bucket(_proof(target, legacy_proofs[target], occurrences))):
                raise ValueError("stored_alias_target_mismatch")
            _hash(alias["raw_sha256"])
            existing_buckets[_bucket(alias)].add(key)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            error(str(exc), source_key=key)

    for bucket, targets in eligible.items():
        identities = incoming_buckets[bucket] | existing_buckets[bucket]
        if identities and len(all_old[bucket]) != 1:
            error("ambiguous_legacy_targets", occurrence_keys=sorted(all_old[bucket]))
        if len(identities) > 1:
            error("ambiguous_source_turns", source_keys=sorted(identities))

    for key, capture in incoming.items():
        alias = existing.get(key)
        if alias:
            try:
                if (_bucket(alias) != _bucket(capture) or _time(alias["generated_at"]) != _time(capture["generated_at"])):
                    raise ValueError("source_turn_changed_after_alias")
                result["known"].append(_resolution(alias, occurrences[alias["target_occurrence_key"]]))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                error(str(exc), source_key=key)
            continue
        candidates = eligible[_bucket(capture)]
        if not candidates:
            result["unmatched"].append(deepcopy(capture))
            continue
        if len(candidates) != 1:
            error("ambiguous_eligible_targets", source_key=key)
            continue
        target = candidates[0]
        alias = {**capture, "alias_version": 1, "normalization_version": 1,
                 "target_occurrence_key": target, "precision_evidence": deepcopy(legacy_proofs[target])}
        result["aliases"][key] = alias
        result["known"].append(_resolution(alias, occurrences[target]))

    if result["errors"]:
        result.update(status="error", aliases={}, known=[], unmatched=[])
    return result
