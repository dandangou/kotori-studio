#!/usr/bin/env python3
"""Copy an available portable Python runtime, without its third-party packages."""
import pathlib
import shutil
import sys

source = pathlib.Path(sys.argv[1]).resolve().parents[1]
target = pathlib.Path(sys.argv[2]).resolve()
if target.exists():
    raise SystemExit("目标 Python 目录已存在；为避免覆盖现有运行环境，停止复制。")
shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"))
for link in target.rglob("*"):
    if link.is_symlink() and link.readlink().is_absolute():
        sibling = link.parent / link.readlink().name
        if sibling.exists():
            link.unlink()
            link.symlink_to(sibling.name)
for file in (target / "bin").iterdir():
    if not file.name.startswith("python"):
        if file.is_dir():
            shutil.rmtree(file)
        else:
            file.unlink()
print(f"本地 Python 运行时：{target}")
