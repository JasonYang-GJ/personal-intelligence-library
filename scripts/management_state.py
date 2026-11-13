"""Read immutable user management events. Never changes originals or intake state."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
RECORD = re.compile(r"(?:intel-\d{8}-\d{3,}|review-\d{8})")
STATES = {"active", "archived", "trashed"}


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("管理记录包含重复字段")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)


def _plain(path):
    if path.exists() and (path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400):
        raise ValueError("管理资料路径不能是链接")


def validate_event(event, name, project_id):
    keys = {"schema_version", "project_id", "id", "record_id", "action", "state", "restore_to", "order", "created_at"}
    if not isinstance(event, dict) or set(event) != keys:
        raise ValueError("管理记录字段不完整或包含未知字段")
    if event["schema_version"] != 1 or event["project_id"] != project_id:
        raise ValueError("管理记录不属于当前项目")
    if not isinstance(event["id"], str) or not UUID.fullmatch(event["id"]) or name != event["id"] + ".json":
        raise ValueError("管理记录编号与文件名不一致")
    if not isinstance(event["record_id"], str) or not RECORD.fullmatch(event["record_id"]):
        raise ValueError("管理记录指向无效条目")
    if event["state"] not in STATES or event["restore_to"] not in {"active", "archived"}:
        raise ValueError("管理记录状态无效")
    pairs = {"archive": {"archived"}, "unarchive": {"active"}, "trash": {"trashed"}, "restore": {"active", "archived"}}
    if event["action"] not in pairs or event["state"] not in pairs[event["action"]]:
        raise ValueError("管理操作与目标状态不一致")
    if event["state"] != "trashed" and event["restore_to"] != event["state"]:
        raise ValueError("恢复位置与状态不一致")
    if type(event["order"]) is not int or not 0 <= event["order"] <= 9007199254740991:
        raise ValueError("管理记录顺序无效")
    try:
        when = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("管理记录时间无效") from error
    return event


def load_management(root):
    root = Path(root).resolve()
    folder = root / "data/management"
    for path in (root / "data", folder, folder / "events", folder / "project.json"):
        _plain(path)
    marker = folder / "project.json"
    empty = {"schema_version": 1, "project_id": None, "states": {}, "event_count": 0,
             "empty_files": [], "fingerprint": hashlib.sha256(b"").hexdigest(), "last_event_at": None}
    if not marker.exists():
        if (folder / "events").exists() and any((folder / "events").iterdir()):
            raise ValueError("管理目录缺少项目身份；不能忽略现有操作")
        return empty
    project = _json(marker.read_bytes())
    if (not isinstance(project, dict) or project.get("schema_version") != 1 or
            not isinstance(project.get("project_id"), str) or not UUID.fullmatch(project["project_id"])):
        raise ValueError("管理目录项目身份无效")
    result = {**empty, "project_id": project["project_id"]}
    events, fingerprints, unfinished = [], [], []
    for path in sorted((folder / "events").glob("*.json")):
        _plain(path)
        if not UUID.fullmatch(path.stem):
            raise ValueError("管理目录包含未知记录文件：" + path.name)
        if path.stat().st_size > 16384:
            raise ValueError("管理记录过大：" + path.name)
        raw = path.read_bytes()
        fingerprints.append(path.name + ":" + hashlib.sha256(raw).hexdigest())
        if not raw:
            unfinished.append(path.name)
            continue  # File handle exists, but the browser has not closed its stream.
        event = validate_event(_json(raw), path.name, project["project_id"])
        if (json.dumps(event, ensure_ascii=False, indent=2) + "\n").encode() != raw:
            raise ValueError("管理记录格式发生变化，请核对：" + path.name)
        events.append(event)
    for event in sorted(events, key=lambda value: (value["order"], value["id"])):
        result["states"][event["record_id"]] = {
            "state": event["state"], "restore_to": event["restore_to"],
            "event_id": event["id"], "order": event["order"]}
        result["last_event_at"] = event["created_at"]
    result.update(event_count=len(events), empty_files=unfinished,
                  fingerprint=hashlib.sha256("\n".join(fingerprints).encode()).hexdigest())
    return result
