"""Build the local library with Python's standard library. No network access."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
OUTPUT = "个人情报库.html"
CATEGORIES = {
    "ai-models": "AI模型新发布",
    "ai-products": "AI产品机会雷达",
    "platform-income": "平台变现机会雷达",
    "user-pains": "真实用户痛点雷达",
    "solo-business": "个人创业机会雷达",
    "cross-border": "跨境电商入场雷达",
}
RECOMMENDATIONS = {"值得尝试", "继续观察", "暂不考虑"}
STATUSES = {"待验证", "验证中", "准备做", "搁置"}
WEEKLY_REVIEW_TASK_ID = "example-weekly-review"
WEEKLY_REVIEW_THREAD_ID = "00000000-0000-4000-8000-000000000003"
WEEKLY_REVIEW_SLOTS = {"2026-10-25T10:00:00+08:00", "2026-11-01T10:00:00+08:00"}
WEEKLY_REVIEW_PACKET_FIELDS = {"schema_version", "scheduled_for", "coverage_start", "coverage_end", "input_sha256",
                               "title", "summary", "candidate_record_ids", "selected_record_id", "next_action", "unknowns"}


class ValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def text_field(obj, key, context, max_length=10000):
    value = obj.get(key)
    require(isinstance(value, str) and bool(value.strip()), f"{context}：{key} 必须是非空文本")
    require(len(value) <= max_length, f"{context}：{key} 过长，请保留完整原文并缩短卡片")
    return value


def date_field(value, context, nullable=False):
    if value is None and nullable:
        return
    require(isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)), f"{context}：日期格式应为 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{context}：无效日期") from exc


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"JSON 中出现重复字段：{key}")
        result[key] = value
    return result


def review_timestamp(value, context):
    require(isinstance(value, str), f"{context}：时间必须是带时区文本")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{context}：时间格式无效") from exc
    require(parsed.tzinfo is not None, f"{context}：时间必须含时区")
    return parsed.astimezone(timezone(timedelta(hours=8)))


def validate_review_fields(review, record_ids):
    """Shared review content checks; ingestion owns provenance and file writes."""
    require(isinstance(review, dict), "周复盘必须是对象")
    scheduled = review_timestamp(review.get("scheduled_for"), "周复盘计划时间")
    require(scheduled.isoformat() in WEEKLY_REVIEW_SLOTS, "周复盘不属于已授权的两次计划")
    start = review_timestamp(review.get("coverage_start"), "周复盘覆盖开始")
    end = review_timestamp(review.get("coverage_end"), "周复盘覆盖结束")
    require(start < end, "周复盘覆盖时间必须递增")
    for field, size in (("title", 80), ("summary", 220), ("next_action", 400)):
        text_field(review, field, "周复盘", size)
    candidates = review.get("candidate_record_ids")
    require(isinstance(candidates, list) and len(candidates) <= 3 and
            all(isinstance(value, str) and value in record_ids for value in candidates),
            "周复盘候选必须是最多三条已有情报")
    require(len(set(candidates)) == len(candidates), "周复盘候选不能重复")
    require("selected_record_id" in review, "周复盘须明确选中情报或 null")
    selected = review["selected_record_id"]
    require(selected is None or (isinstance(selected, str) and selected in candidates),
            "周复盘选中情报必须来自候选")
    unknowns = review.get("unknowns")
    require(isinstance(unknowns, list) and len(unknowns) <= 30 and
            all(isinstance(value, str) and value.strip() and len(value) <= 1000 for value in unknowns),
            "周复盘待确认事项必须是文本列表")
    require(isinstance(review.get("input_sha256"), str) and
            re.fullmatch(r"[0-9a-f]{64}", review["input_sha256"]) is not None,
            "周复盘缺少有效的输入依据指纹")
    return scheduled, start, end


def load_weekly_reviews(root, catalog, record_ids):
    reviews = catalog.get("weekly_reviews", [])
    require(isinstance(reviews, list), "weekly_reviews 必须是列表")
    seen = set()
    for review in reviews:
        scheduled, _start, end = validate_review_fields(review, record_ids)
        rid = "review-" + scheduled.strftime("%Y%m%d")
        require(review.get("id") == rid and rid not in seen, "周复盘标识无效或重复")
        seen.add(rid)
        require(review.get("source_task_id") == WEEKLY_REVIEW_TASK_ID and
                review.get("source_thread_id") == WEEKLY_REVIEW_THREAD_ID, "周复盘来源身份不符")
        generated = review_timestamp(review.get("generated_at"), f"{rid} 生成时间")
        require(end <= generated and scheduled <= generated, f"{rid}：复盘不能包含未来覆盖时间或提前执行")
        date_field(review.get("collected_at"), f"{rid} 收录日期")
        require(review["collected_at"] == generated.date().isoformat(), f"{rid}：收录日期与生成时间不符")
        for field in ("original_sha256", "packet_sha256"):
            require(isinstance(review.get(field), str) and re.fullmatch(r"[0-9a-f]{64}", review[field]) is not None,
                    f"{rid}：{field} 无效")
        packet = {key: review[key] for key in WEEKLY_REVIEW_PACKET_FIELDS if key != "schema_version"}
        packet["schema_version"] = 1
        packet_bytes = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        require(hashlib.sha256(packet_bytes).hexdigest() == review["packet_sha256"], f"{rid}：复盘结构与提交指纹不符")
        expected_name = f"data/reviews/{rid}.md"
        require(review.get("original_file") == expected_name, f"{rid}：复盘原文必须位于 data/reviews 固定路径")
        target = root / expected_name
        for path in (target, *target.parents):
            if path == root.parent:
                break
            require(not path.is_symlink() and not (path.exists() and getattr(path.stat(), "st_file_attributes", 0) & 0x400),
                    f"{rid}：拒绝链接复盘路径")
        raw = target.read_bytes()
        require(bool(raw.strip()), f"{rid}：复盘原文为空")
        require(hashlib.sha256(raw).hexdigest() == review["original_sha256"], f"{rid}：复盘原文校验不通过")
        review["original"] = raw.decode("utf-8")


def load_library(root=ROOT):
    from management_state import load_management
    root = Path(root).resolve()
    catalog = json.loads((root / "data/catalog.json").read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)
    require(isinstance(catalog, dict), "资料目录必须是对象")
    require(catalog.get("schema_version") == 1, "不支持的资料格式版本")
    text_field(catalog, "library_name", "资料目录", 100)
    date_field(catalog.get("updated_at"), "最后更新日期")
    categories = catalog.get("categories")
    require(isinstance(categories, list) and len(categories) == 6, "必须保留六个分类")
    require(all(isinstance(c, dict) for c in categories), "分类格式无效")
    require({c.get("id"): c.get("label") for c in categories} == CATEGORIES, "分类标识或名称与六类约定不一致")
    for category in categories:
        for field in ("short_label", "description", "focus"):
            text_field(category, field, category["label"], 300)
    records = catalog.get("records")
    require(isinstance(records, list), "records 必须是列表")
    ids = set()
    original_paths = set()
    for record in records:
        require(isinstance(record, dict), "记录格式无效")
        rid = text_field(record, "id", "记录", 80)
        require(re.fullmatch(r"intel-\d{8}-\d{3,}", rid) is not None, f"记录标识无效：{rid}")
        require(rid not in ids, f"记录标识重复：{rid}")
        ids.add(rid)
        require(record.get("category_id") in CATEGORIES, f"{rid}：未知分类")
        for field, size in (("title", 80), ("source_task", 100), ("summary", 220), ("audience", 300), ("relevance", 160), ("recommendation_reason", 160), ("next_action", 200)):
            text_field(record, field, rid, size)
        require(record.get("recommendation") in RECOMMENDATIONS, f"{rid}：未知建议")
        require(record.get("status") in STATUSES, f"{rid}：未知进度")
        date_field(record.get("collected_at"), f"{rid} 收录日期")
        require("report_date" in record, f"{rid}：须写 report_date；未知用 null")
        date_field(record["report_date"], f"{rid} 报告日期", nullable=True)
        for field in ("tags", "uncertainties", "related_ids"):
            value = record.get(field)
            require(isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value), f"{rid}：{field} 必须是文本列表")
        original_name = text_field(record, "original_file", rid)
        original_root = (root / "data/originals").resolve()
        original_path = (root / original_name).resolve()
        require(original_path.is_relative_to(original_root) and original_path.suffix in {".md", ".txt"}, f"{rid}：原文必须位于 data/originals 内")
        require(original_path not in original_paths, f"{rid}：不同记录应保留各自的原文文件")
        original_paths.add(original_path)
        original_bytes = original_path.read_bytes()
        require(bool(original_bytes.strip()), f"{rid}：原文为空")
        expected_hash = text_field(record, "original_sha256", rid, 64)
        require(hashlib.sha256(original_bytes).hexdigest() == expected_hash, f"{rid}：原文校验不通过。请先核对原文，不得直接更新校验值来绕过检查。")
        record["original"] = original_bytes.decode("utf-8")
        sources = record.get("sources")
        require(isinstance(sources, list), f"{rid}：sources 必须是列表")
        for source in sources:
            require(isinstance(source, dict), f"{rid}：来源格式无效")
            for field in ("label", "url", "kind", "verification"):
                text_field(source, field, rid)
            url = source["url"]
            require(not any(ch.isspace() or ord(ch) < 32 for ch in url), f"{rid}：来源链接含无效空白字符")
            parts = urlsplit(url)
            require(parts.scheme in {"https", "http"} and bool(parts.hostname) and not parts.username and not parts.password, f"{rid}：来源仅允许无账号凭证的 HTTP/HTTPS 链接")
            require(source["verification"] in {"未核验", "已核验"}, f"{rid}：未知核验状态")
            if source["verification"] == "已核验":
                date_field(source.get("checked_at"), f"{rid} 来源核验日期")
                text_field(source, "verification_note", f"{rid} 核验说明")
    for record in records:
        for related in record["related_ids"]:
            require(related in ids and related != record["id"], f"{record['id']}：关联记录不存在或指向自身")
    load_weekly_reviews(root, catalog, ids)
    catalog["management"] = load_management(root)
    return catalog


def embedded_json(data):
    # A report may contain HTML or a closing script tag. Keep it inert in JSON.
    result = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for char, escaped in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        result = result.replace(char, escaped)
    return result


def render(root, catalog):
    template = (root / "ui/template.html").read_text(encoding="utf-8")
    styles = (root / "ui/styles.css").read_text(encoding="utf-8")
    script = (root / "ui/app.js").read_text(encoding="utf-8")
    management_script = root / "ui/management.js"
    if management_script.exists():
        script = management_script.read_text(encoding="utf-8") + "\n" + script
    manual_script = root / "ui/manual-intake.js"
    if manual_script.exists():
        script = manual_script.read_text(encoding="utf-8") + "\n" + script
    values = {"{{STYLES}}": styles, "{{DATA}}": embedded_json(catalog), "{{SCRIPT}}": script}
    for token in values:
        require(template.count(token) == 1, f"页面模板缺少或重复占位符：{token}")
    # Substitute once: report text that resembles template syntax stays text.
    return re.sub(r"\{\{(?:STYLES|DATA|SCRIPT)\}\}", lambda match: values[match.group()], template)


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), f"拒绝写入符号链接：{path.name}")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".library-", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink()


def build(root=ROOT, *, management_snapshot=None):
    root = Path(root).resolve()
    catalog = load_library(root)
    if management_snapshot is not None:
        catalog["management"] = management_snapshot
    html = render(root, catalog).encode("utf-8")
    paths = ["PROJECT.md", "AGENTS.md", "使用说明.txt", "data/catalog.json", "ui/template.html", "ui/styles.css", "ui/app.js", "scripts/build.py"]
    paths += ["ui/management.js", "ui/manual-intake.js", "scripts/management_state.py", "data/management/project.json"]
    paths.extend(record["original_file"] for record in catalog["records"])
    paths.extend(review["original_file"] for review in catalog.get("weekly_reviews", []))
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths if (root / name).is_file()}
    hashes[OUTPUT] = hashlib.sha256(html).hexdigest()
    manifest = {"library_name": catalog["library_name"], "updated_at": catalog["updated_at"], "record_count": len(catalog["records"]), "weekly_review_count": len(catalog.get("weekly_reviews", [])), "category_count": len(catalog["categories"]), "sha256": hashes}
    manifest["management_snapshot"] = catalog["management"]
    atomic_write(root / OUTPUT, html)
    atomic_write(root / "evidence/build-manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return catalog, root / OUTPUT


def main():
    parser = argparse.ArgumentParser(description="校验资料并更新个人情报库首页；不联网、不修改原文。")
    parser.add_argument("--check", action="store_true", help="仅校验资料，不写入文件")
    args = parser.parse_args()
    try:
        if args.check:
            catalog = load_library()
            print(f"资料校验通过：{len(catalog['categories'])} 类，{len(catalog['records'])} 条报告。")
        else:
            catalog, output = build()
            print(f"首页已更新：{output}\n共 {len(catalog['categories'])} 类，{len(catalog['records'])} 条报告；原文校验通过。")
    except (ValidationError, OSError, ValueError, UnicodeError) as exc:
        print(f"未完成更新：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
