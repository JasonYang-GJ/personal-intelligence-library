"""Read-only checks for a completed background intake and optional fresh reread.

This module never starts a browser, calls a model, modifies the library, or
acknowledges notifications. A pass describes the supplied evidence window, not
reports that arrive after that window. Legacy minute aliases are checked using
their original UI evidence; formatting-normalized equality is labelled as such.
"""
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

import build
from background_capture import load_capture, legacy_proofs
from intake_alias import SOURCES, plan_aliases, source_key
from intake_state import normalize


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间没有时区")
    return parsed


def validate_intake(root, result_file, recheck_run_dir=None):
    """Return {status: pass|issues, issues: [{code,message}], evidence: ...}.

    ``root`` is the library directory. Both supplied names must be project
    relative. The caller owns scheduling and chooses the target time; this
    function checks six checkpoints against the result's actual scan_end.
    """
    root = Path(root).resolve()
    issues, observed, evidence = [], {}, {}

    def issue(code, message):
        item = {"code": code, "message": str(message)}
        if item not in issues:
            issues.append(item)

    def path(name):
        if not isinstance(name, str) or not name or "\\" in name:
            raise ValueError("路径必须是项目内相对路径")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or ":" in name:
            raise ValueError("拒绝项目外路径")
        target = root / relative
        for candidate in (root, *list(target.parents)[::-1], target):
            if candidate.is_relative_to(root) and candidate.exists():
                if candidate.is_symlink() or getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("拒绝链接路径")
        return target

    def raw(name):
        value = path(name).read_bytes()
        digest = _hash(value)
        if name in observed and observed[name] != digest:
            raise ValueError("检查期间文件发生变化：" + name)
        observed[name] = digest
        return value

    def read(name):
        return json.loads(raw(name).decode("utf-8-sig"), object_pairs_hook=build.unique_keys)

    def check(code, function):
        try:
            return function()
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            issue(code, str(error))
            return None

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def result_check():
        require(path(result_file).is_relative_to(root / "evidence/daily"), "收录结果必须来自 evidence/daily")
        value = read(result_file)
        require(value.get("status") == "complete", "收录结果不是 complete")
        require(set(value["complete_categories"]) == set(SOURCES) and len(value["complete_categories"]) == 6,
                "收录结果没有完整六类")
        require(not value.get("failures"), "收录结果仍有失败分类")
        require(_time(value["scan_start"]) <= _time(value["scan_end"]) <= _time(value["finished_at"]), "结果时间顺序错误")
        require(isinstance(value.get("capture_run"), str), "旧版或手动结果缺少完整后台读取证据，不能判定通过")
        return value

    result = check("result_invalid", result_check)
    catalog_raw = check("catalog_invalid", lambda: read("data/catalog.json"))

    def library_check():
        require(catalog_raw is not None, "资料目录不可读取")
        for record in catalog_raw["records"]:
            raw(record["original_file"])
        return build.load_library(root)

    library = check("original_or_catalog_invalid", library_check)
    state = check("ledger_invalid", lambda: read("data/intake-state.json"))
    records = {r["id"]: r for r in library["records"]} if library else {}
    if library:
        evidence["record_count"] = len(records)
        evidence["originals_sha256"] = {r["id"]: r["original_sha256"] for r in records.values()}

        def unique_cards():
            bodies, identities = defaultdict(list), defaultdict(list)
            for record in records.values():
                bodies[(record["category_id"], _hash(normalize(record["original"]).encode()))].append(record["id"])
                source = record.get("source_capture") or {}
                if source.get("conversation_id") or source.get("turn_key"):
                    identities[source_key(source["conversation_id"], source["turn_key"])].append(record["id"])
            for group in bodies.values():
                if len(group) > 1:
                    issue("duplicate_cards_need_review", "同类正文重复，现有结构无明确重复收录授权凭据：" + ", ".join(group))
            for group in identities.values():
                if len(group) > 1:
                    issue("duplicate_source_cards", "同一来源消息产生多张卡片：" + ", ".join(group))
        check("card_identity_invalid", unique_cards)

    def capture_check(directory):
        raw(directory + "/run.json")
        batch = load_capture(path(directory), root=root)
        require(batch["status"] == "complete" and set(batch["complete_categories"]) == set(SOURCES)
                and not batch["failed_categories"], "后台读取没有证明六类完整历史边界")
        for capture in batch["captures"]:
            raw(capture["capture_file"])
            raw(capture["metadata_file"])
        return batch

    batch = check("capture_invalid", lambda: capture_check(result["capture_run"])) if result else None
    if batch:
        def capture_result_binding():
            require(all(_time(batch[key]) == _time(result[key]) for key in ("scan_start", "scan_end")), "结果与读取证据的时间窗不同")
            require(_time(result["finished_at"]) >= _time(batch["run"]["finished_at"]), "收录结果早于实际读取完成")
            keys = [source_key(c["conversation_id"], c["turn_key"]) for c in batch["captures"]]
            decisions = result["decisions"]
            require(len(keys) == len(set(keys)), "读取证据包含重复来源身份")
            require(Counter(keys) == Counter(d["source_key"] for d in decisions), "结果决策未逐条覆盖所有读取身份")
            require(all(d["status"] in {"known", "new", "duplicate"} for d in decisions), "结果含有未处理报告")
            evidence.update(capture_run=batch["run_dir"], capture_run_sha256=batch["run_sha256"],
                            scan_start=batch["scan_start"], scan_end=batch["scan_end"],
                            captured_reports=len(keys), complete_categories=sorted(batch["complete_categories"]))
        check("capture_result_mismatch", capture_result_binding)

    bindings = {}
    def ledger_check():
        require(library is not None and state is not None, "原文或台账不可读取")
        require(state.get("schema_version") == state.get("normalization_version") == 1, "台账版本错误")
        require(set(state["tasks"]) == set(SOURCES), "台账缺少六类断点")
        for key, entry in state["occurrences"].items():
            record = records[entry["record_id"]]
            require(entry["category_id"] in SOURCES and entry["outcome"] in {"imported", "duplicate"}, "台账分类或结果无效")
            require(key == _hash(f"{entry['category_id']}\n{entry['generated_at']}\n{entry['normalized_sha256']}".encode()), "出现身份散列不一致")
            _time(entry["generated_at"])
            captured = raw(entry["capture_file"])
            require(_hash(captured) == entry["raw_sha256"], "台账捕获原文 SHA 不一致：" + key)
            canonical = _hash(normalize(captured.decode("utf-8")).encode())
            require(canonical == entry["normalized_sha256"] == _hash(normalize(record["original"]).encode()), "台账正文与完整原文不同：" + key)
            if entry["outcome"] == "imported":
                require(record["category_id"] == entry["category_id"] and record["original_sha256"] == entry["raw_sha256"], "导入结果的分类或原文字节不一致")
        targets = {}
        for kind in ("source_aliases", "source_occurrences"):
            for key, binding in state.get(kind, {}).items():
                require(key not in bindings, "来源身份同时映射到旧别名和新台账：" + key)
                require(key == source_key(binding["conversation_id"], binding["turn_key"]), "来源身份散列不一致")
                category = binding["category_id"]
                require(category in SOURCES and binding["conversation_id"] == SOURCES[category][1], "来源与分类不符")
                target = binding["target_occurrence_key"]
                require(target not in targets, "多个来源身份指向同一出现台账")
                targets[target] = key
                entry = state["occurrences"][target]
                require(entry["category_id"] == category and entry["normalized_sha256"] == binding["normalized_sha256"], "来源与目标台账不符")
                captured = raw(binding["capture_file"])
                require(_hash(captured) == binding["raw_sha256"] and _hash(normalize(captured.decode()).encode()) == binding["normalized_sha256"], "来源绑定的完整捕获原文不一致")
                if kind == "source_occurrences":
                    require(_time(binding["generated_at"]).replace(microsecond=0) == _time(entry["generated_at"]), "精确来源时间与台账不符")
                bindings[key] = (kind, binding, entry)
        for record in records.values():
            source = record.get("source_capture") or {}
            if source.get("conversation_id") or source.get("turn_key"):
                key = source_key(source["conversation_id"], source["turn_key"])
                require(key in bindings, "卡片来源身份没有台账映射：" + record["id"])
                _, binding, entry = bindings[key]
                require(entry["outcome"] == "imported" and entry["record_id"] == record["id"]
                        and binding["category_id"] == record["category_id"]
                        and _time(binding["generated_at"]) == _time(source["generated_at"]), "卡片来源与台账映射不同")
        proofs = legacy_proofs(root, state)
        for proof in proofs.values():
            raw(proof["evidence_file"])
            for name in proof["supporting_evidence"]:
                raw(name)
        alias_captures = [c for c in batch["captures"] if source_key(c["conversation_id"], c["turn_key"]) in state.get("source_aliases", {})] if batch else []
        aliases = plan_aliases(state, alias_captures, proofs)
        require(aliases["status"] == "ready", "旧报告身份时间证据不符：" + json.dumps(aliases["errors"], ensure_ascii=False))
        for alias in state.get("source_aliases", {}).values():
            require(alias["precision_evidence"] == proofs[alias["target_occurrence_key"]], "旧时间证据与保存的证明不同")
        evidence["source_bindings"] = {kind: len(state.get(kind, {})) for kind in ("source_aliases", "source_occurrences")}
        if result:
            require(all(_time(v["checked_through"]) >= _time(result["scan_end"]) for v in state["tasks"].values()), "至少一类断点未覆盖本轮结束时间")
            evidence["checked_through"] = {c: v["checked_through"] for c, v in state["tasks"].items()}
    check("ledger_or_source_binding_invalid", ledger_check)

    if batch and state and library:
        def check_each_capture():
            decisions = {d["source_key"]: d for d in result["decisions"]}
            captured_keys = {source_key(c["conversation_id"], c["turn_key"]) for c in batch["captures"]}
            expected_keys = {key for key, (_, binding, _) in bindings.items()
                             if _time(batch["scan_start"]) <= _time(binding["generated_at"]) <= _time(batch["scan_end"])}
            require(expected_keys.issubset(captured_keys), "读取漏掉台账中已知的窗内来源消息：" + ", ".join(sorted(expected_keys - captured_keys)))
            imported, duplicated = [], []
            exact, normalized_legacy = 0, 0
            for capture in batch["captures"]:
                key = source_key(capture["conversation_id"], capture["turn_key"])
                require(key in bindings, "报告漏收，来源消息未记入台账：" + key)
                kind, binding, entry = bindings[key]
                require(_time(binding["generated_at"]) == _time(capture["generated_at"]), "同一来源消息时间改变：" + key)
                require(binding["normalized_sha256"] == capture["normalized_sha256"], "同一来源消息正文改变：" + key)
                require(binding["raw_sha256"] == capture["raw_sha256"], "同一来源消息完整字节改变：" + key)
                record = records[entry["record_id"]]
                if kind == "source_occurrences" and entry["outcome"] == "imported":
                    require(record["original_sha256"] == capture["raw_sha256"], "新导入原文未完整保存")
                    exact += 1
                elif kind == "source_aliases":
                    normalized_legacy += 1
                decision = decisions[key]
                if decision["status"] in {"new", "duplicate"}:
                    require(decision["record_id"] == entry["record_id"] and entry["run_id"] == result["run_id"], "新增/重复决策与本轮台账不符")
                    expected = "imported" if decision["status"] == "new" else "duplicate"
                    require(entry["outcome"] == expected, "新增/重复结果语义不符")
                    (imported if expected == "imported" else duplicated).append(entry["record_id"])
            require(Counter(imported) == Counter(result["imported_ids"]) and Counter(duplicated) == Counter(result["duplicate_ids"]), "新增/重复汇总与逐条结果不同")
            for notice in result["duplicate_notifications"]:
                entry = state["occurrences"][notice["occurrence_key"]]
                require(entry["outcome"] == "duplicate" and entry["record_id"] == notice["existing_id"]
                        and isinstance(notice.get("message"), str) and bool(notice["message"].strip()), "重复提醒与实际台账不符")
            evidence.update(exact_new_originals=exact, legacy_normalized_originals=normalized_legacy)
        check("report_missing_or_mismatched", check_each_capture)

    if result:
        def journal_check():
            run_id = result["run_id"]
            require(isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id), "运行标识无效")
            name = f".intake/background/runs/{run_id}/journal.json"
            if not path(name).exists():
                require(not run_id.startswith("bg-"), "后台收录缺少提交日志")
                evidence["journal"] = "legacy_result_without_transaction_journal"
                return
            journal = read(name)
            require(journal.get("schema_version") == 1 and journal.get("status") == "committed", "收录事务尚未完整提交")
            require(journal["run_id"] == run_id and journal["result_file"] == result_file, "提交日志对应不同结果")
            operations = journal["operations"]
            require(len({op["path"] for op in operations}) == len(operations), "提交路径重复")
            result_ops = [op for op in operations if op["path"] == result_file]
            require(len(result_ops) == 1 and result_ops[0]["new_sha256"] == _hash(raw(result_file)), "提交的结果散列不一致")
            evidence["journal"] = {"file": name, "status": "committed", "sha256": observed[name]}
        check("transaction_not_committed", journal_check)

    if library:
        def homepage_check():
            html = raw(build.OUTPUT)
            for name in ("ui/template.html", "ui/styles.css", "ui/app.js", "scripts/build.py"):
                raw(name)
            manifest = read("evidence/build-manifest.json")
            # A later browser archive/trash action is not a failed intake.
            # Reproduce the actual built snapshot, not today's live event log.
            display_library = dict(library)
            if "management_snapshot" in manifest:
                display_library["management"] = manifest["management_snapshot"]
            require(html == build.render(root, display_library).encode("utf-8"), "首页与当前资料/页面源代码不一致")
            blocks = re.findall(r'<script\s+type="application/json"\s+id="library-data">(.*?)</script>', html.decode(), re.S)
            require(len(blocks) == 1 and json.loads(blocks[0], object_pairs_hook=build.unique_keys) == display_library, "首页嵌入资料与完整原文不一致")
            require(manifest["record_count"] == len(records) and manifest["category_count"] == 6
                    and manifest["updated_at"] == library["updated_at"], "首页构建清单数量或日期不一致")
            names = ["data/catalog.json", build.OUTPUT, "ui/template.html", "ui/styles.css", "ui/app.js", "scripts/build.py"]
            names += [r["original_file"] for r in records.values()]
            names += [r["original_file"] for r in library.get("weekly_reviews", [])]
            names += [name for name in ("ui/management.js", "scripts/management_state.py", "data/management/project.json") if (root / name).exists()]
            require(all(manifest["sha256"].get(name) == _hash(raw(name)) for name in names), "构建清单的资料或首页散列不一致")
            if result:
                latest = library.get("last_intake", {})
                require(latest.get("run_id") == result["run_id"] and latest.get("result_file") == result_file
                        and latest.get("status") == "complete", "首页未显示本轮完成结果")
            evidence["homepage_sha256"] = _hash(html)
            evidence["manifest_sha256"] = observed["evidence/build-manifest.json"]
        check("homepage_not_updated", homepage_check)

    if recheck_run_dir is not None:
        reread = check("independent_recheck_invalid", lambda: capture_check(recheck_run_dir))
        if reread and batch:
            def compare_reread():
                require(reread["run_dir"] != batch["run_dir"], "复核必须使用另一次独立读取")
                require(_time(reread["run"]["created_at"]) >= _time(batch["run"]["finished_at"]), "复核读取没有发生在原读取完成后")
                require(all(_time(reread[k]) == _time(batch[k]) for k in ("scan_start", "scan_end")), "独立复核必须使用完全相同的时间窗")
                def rows(captures):
                    return {source_key(c["conversation_id"], c["turn_key"]):
                            (c["category_id"], _time(c["generated_at"]), c["raw_sha256"], c["normalized_sha256"])
                            for c in captures}
                before, after = rows(batch["captures"]), rows(reread["captures"])
                require(len(before) == len(batch["captures"]) and len(after) == len(reread["captures"]), "复核来源身份重复")
                missing, unexpected = sorted(before.keys() - after.keys()), sorted(after.keys() - before.keys())
                changed = sorted(key for key in before.keys() & after.keys() if before[key] != after[key])
                require(not missing and not unexpected and not changed,
                        f"两次读取不一致：复核缺失={missing}，原收录遗漏={unexpected}，正文或时间改变={changed}")
                evidence["independent_recheck"] = {"run_dir": reread["run_dir"], "run_sha256": reread["run_sha256"],
                                                   "identities_and_full_bodies_equal": len(before), "same_window": True}
            check("independent_recheck_mismatch", compare_reread)

    for name, expected in list(observed.items()):
        check("files_changed_during_audit", lambda name=name, expected=expected:
              require(_hash(path(name).read_bytes()) == expected, "验收期间文件发生变化：" + name))
    evidence["files_checked"] = len(observed)
    evidence["result_file"] = result_file
    return {"status": "issues" if issues else "pass", "issues": issues, "evidence": evidence}
