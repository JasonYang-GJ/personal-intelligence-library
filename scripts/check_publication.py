"""Reject private/generated paths and common secret formats before publication.

Run after `git add`, before committing. Only file names and rule names are logged.
Pattern checks supplement review; they do not guarantee absence of all secrets.
"""
from pathlib import Path
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT = {"README.md", "PROJECT.md", "AGENTS.md", "使用说明.txt", ".gitignore", ".gitattributes"}
ALLOWED_DIRS = {"scripts", "ui", "browser-extension", "tests", "examples", ".github"}
ALLOWED_SUFFIXES = {".py", ".ps1", ".js", ".mjs", ".css", ".html", ".json", ".md", ".yml", ".txt"}
PATTERNS = {
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "github-fine-grained-token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    "api-key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private-key": re.compile(r"^-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.M),
}


def check():
    result = subprocess.run(["git", "ls-files", "--cached", "-z"], cwd=ROOT,
                            capture_output=True, check=True)
    paths = [p for p in result.stdout.decode("utf-8").split("\0") if p]
    if not paths:
        raise ValueError("No staged/tracked source files to audit")
    findings = []
    for name in paths:
        path = Path(name)
        if name not in ALLOWED_ROOT and (len(path.parts) < 2 or path.parts[0] not in ALLOWED_DIRS or path.suffix not in ALLOWED_SUFFIXES):
            findings.append({"path": name, "rule": "non-source-path"})
        # Audit exact index content, including a staged version differing from disk.
        raw = subprocess.run(["git", "show", ":" + name], cwd=ROOT,
                             capture_output=True, check=True).stdout
        text = raw.decode("utf-8-sig")
        for rule, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append({"path": name, "rule": rule})
        if name == "examples/catalog.example.json":
            catalog = json.loads(text)
            if catalog.get("records") != [] or catalog.get("weekly_reviews") != []:
                findings.append({"path": name, "rule": "example-must-be-empty"})
    print(json.dumps({"files_checked": len(paths), "findings": findings}, ensure_ascii=False))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(check())
