"""Verifies the local toolchain and Python libraries for the RAG project."""
import importlib
import shutil
import subprocess
import sys

OK, BAD = "\033[92m✔\033[0m", "\033[91m✘\033[0m"
failed = False


def report(ok: bool, label: str, detail: str = "") -> None:
    global failed
    failed |= not ok
    print(f"{OK if ok else BAD} {label:<24} {detail}")


def cmd(args: list[str]) -> str | None:
    if not shutil.which(args[0]):
        return None
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return None


# Python
v = sys.version_info
report((v.major, v.minor) == (3, 12), "Python 3.12", f"found {v.major}.{v.minor}.{v.micro}")
report(sys.prefix != sys.base_prefix, "Virtualenv active", sys.prefix)

# CLI tools
for name, args in [
    ("Git", ["git", "--version"]),
    ("Docker", ["docker", "--version"]),
    ("Docker Compose", ["docker", "compose", "version"]),
]:
    out = cmd(args)
    report(out is not None, name, out or "not found")

daemon = subprocess.run(["docker", "info"], capture_output=True, text=True).returncode == 0
report(daemon, "Docker daemon running", "" if daemon else "start Docker Desktop")

# Python libraries (import name, pip name)
libs = [
    ("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"), ("sqlalchemy", "sqlalchemy"),
    ("alembic", "alembic"), ("psycopg", "psycopg"), ("pgvector", "pgvector"),
    ("jwt", "pyjwt"), ("argon2", "argon2-cffi"),
    ("sentence_transformers", "sentence-transformers"), ("anthropic", "anthropic"),
    ("pytest", "pytest"), ("pytest_asyncio", "pytest-asyncio"),
    ("testcontainers", "testcontainers"), ("httpx", "httpx"),
]
for mod, pip_name in libs:
    try:
        m = importlib.import_module(mod)
        report(True, pip_name, getattr(m, "__version__", ""))
    except Exception as e:
        report(False, pip_name, f"import failed: {e.__class__.__name__}")

print("\nAll good. Ready for Step 3." if not failed else "\nFix the ✘ items above, then rerun.")
sys.exit(1 if failed else 0)