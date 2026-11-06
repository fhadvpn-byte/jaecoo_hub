"""بعد انتهاء جلسة Cursor: احفظ تعديلات المنصة وارفعها إلى GitHub دون أسرار."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOW = {
    "app.py",
    "telegram_scraper.py",
    "database_and_ai.py",
    "requirements.txt",
    ".streamlit/config.toml",
    "jaecoo_knowledge.db",
    ".cursor/hooks.json",
    ".cursor/hooks/publish_cloud.py",
}


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True)


def changed_paths() -> list[str]:
    status = run(["git", "status", "--porcelain", "-u"])
    paths = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"').replace("\\", "/")
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path in ALLOW:
            paths.append(path)
    return paths


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass

    paths = changed_paths()
    if not paths:
        print("{}")
        return 0

    if "jaecoo_knowledge.db" in paths:
        db_path = ROOT / "jaecoo_knowledge.db"
        if db_path.exists():
            connection = sqlite3.connect(db_path)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()

    add = run(["git", "add", "-f", "--", *paths])
    if add.returncode != 0:
        print(json.dumps({"followup_message": f"تعذر تجهيز الملفات للرفع: {add.stderr.strip()}"}))
        return 0

    staged = run(["git", "diff", "--cached", "--name-only"])
    if not staged.stdout.strip():
        print("{}")
        return 0

    commit = run(
        [
            "git",
            "commit",
            "-m",
            "Publish the latest hub updates for Streamlit Cloud.",
        ]
    )
    if commit.returncode != 0:
        print(json.dumps({"followup_message": f"تعذر حفظ التعديلات: {commit.stderr.strip()}"}))
        return 0

    push = run(["git", "push", "origin", "HEAD"])
    if push.returncode != 0:
        run(["git", "pull", "--rebase", "origin", "main"])
        push = run(["git", "push", "origin", "HEAD"])
    if push.returncode != 0:
        detail = (push.stderr or push.stdout).strip()
        print(json.dumps({"followup_message": f"حُفظت التعديلات محلياً وتعذر رفعها إلى GitHub: {detail}"}))
        return 0

    names = "، ".join(paths)
    print(
        json.dumps(
            {
                "followup_message": (
                    f"تم رفع هذه الملفات إلى GitHub تلقائياً: {names}. "
                    "أخبر المستخدم بالعربي أن Streamlit Cloud سيتحدث من هذا الدفع، "
                    "ولا تعدل أي ملفات."
                )
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
