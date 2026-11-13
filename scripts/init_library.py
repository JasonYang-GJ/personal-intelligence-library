"""Initialize an empty local library. Never imports data or enables collection."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = "个人情报库.html"
BEIJING = timezone(timedelta(hours=8))


def _plain(path: Path) -> None:
    if path.is_symlink() or (path.exists() and getattr(path.stat(), "st_file_attributes", 0) & 0x400):
        raise ValueError(f"拒绝初始化链接路径：{path.name}")


def _new_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def initialize(root: Path = ROOT) -> Path:
    """Create local-only files in a new checkout; existing state fails closed."""
    root = Path(root).absolute()
    for path in (root, root / "data", root / "evidence", root / ".intake"):
        _plain(path)
    protected = [root / OUTPUT, root / "evidence/build-manifest.json",
                 root / "data/catalog.json", root / "data/intake-state.json",
                 root / "data/catchup-config.json", root / "data/management/project.json"]
    for path in protected:
        _plain(path)
        if path.exists():
            raise ValueError(f"已存在 {path.relative_to(root)}；为保护资料，初始化不会覆盖或重置。")
    for name in ("data", ".intake"):
        path = root / name
        if path.exists() and any(path.iterdir()):
            raise ValueError(f"{name} 目录已有内容；请使用新的代码副本初始化。")
    required = ("examples/catalog.example.json", "scripts/build.py", "scripts/management_state.py",
                "ui/template.html", "ui/styles.css", "ui/app.js", "ui/management.js", "ui/manual-intake.js")
    if any(not (root / name).is_file() for name in required):
        raise ValueError("源码文件不完整，请先完整下载并解压项目。")
    template = json.loads((root / "examples/catalog.example.json").read_text(encoding="utf-8"))
    if template.get("records") != [] or template.get("weekly_reviews") != []:
        raise ValueError("初始化模板必须为空，不能包含报告或复盘。")
    template["updated_at"] = datetime.now(BEIJING).date().isoformat()
    import build
    if {c["id"]: c["label"] for c in template["categories"]} != build.CATEGORIES:
        raise ValueError("初始化模板与代码分类不一致。")
    for name in ("data/originals", "data/reviews", "data/management/events"):
        (root / name).mkdir(parents=True, exist_ok=True)
    _new_json(root / "data/catalog.json", template)
    _new_json(root / "data/management/project.json", {
        "schema_version": 1, "project_id": str(uuid.uuid4()), "library_name": template["library_name"]})
    _new_json(root / "data/intake-state.json", {
        "schema_version": 1, "normalization_version": 1,
        "tasks": {category: {"checked_through": "1970-01-01T00:00:00+08:00", "run_id": "empty-library"}
                  for category in build.CATEGORIES}, "occurrences": {}})
    _new_json(root / "data/catchup-config.json", {
        "schema_version": 1, "enabled": False, "backend": "background", "mode": "manual_only"})
    _, page = build.build(root)
    return page


def main() -> int:
    if sys.version_info < (3, 11):
        print("需要 Python 3.11 或更新版本。", file=sys.stderr)
        return 1
    try:
        page = initialize()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"未完成初始化：{error}", file=sys.stderr)
        return 1
    print(f"空资料库已创建：{page}\n报告数为 0；后台收录保持停用。")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
