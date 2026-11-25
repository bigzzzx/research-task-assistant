"""Parse portable Agent Skills and validate files stored in an account's bundle."""

from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass
from pathlib import PureWindowsPath
import json
import re

import yaml

SKILL_FILE = "SKILL.md"
MAX_DOCUMENT = 12_288
MAX_FILES = 64
MAX_BUNDLE = 2 * 1024 * 1024
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass(frozen=True)
class SkillDocument:
    name: str
    description: str
    source: str


def parse_skill(source: str) -> SkillDocument:
    if len(source.encode("utf-8")) > MAX_DOCUMENT:
        raise ValueError("SKILL.md 最大为 12 KiB。")
    lines = source.lstrip("\ufeff").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 开头需要 YAML 元数据。")
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError("SKILL.md 的 YAML 元数据缺少结束分隔线。")
    try:
        frontmatter = yaml.safe_load("".join(lines[1:end]))
    except (yaml.YAMLError, RecursionError):
        raise ValueError("SKILL.md 的 YAML 元数据格式无效。") from None
    if not isinstance(frontmatter, dict):
        raise ValueError("SKILL.md 的 YAML 元数据需要提供 name 和 description。")
    name = frontmatter.get("name")
    if not isinstance(name, str) or len(name) > 64 or not SKILL_NAME.fullmatch(name):
        raise ValueError("name 需为 1–64 位小写英文字母、数字或单个连字符组成的技能标识。")
    description = frontmatter.get("description")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise ValueError("description 需填写 1–1024 个字符，说明用途和适用场景。")
    return SkillDocument(name, description.strip(), source)


def render_skill(name: str, description: str, body: str) -> str:
    if len(name) > 64 or not SKILL_NAME.fullmatch(name):
        raise ValueError("name 需为 1–64 位小写英文字母、数字或单个连字符组成的技能标识。")
    return f"---\nname: '{name}'\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n{body}\n"


def bundle_files(files: dict[str, str]) -> dict[str, str]:
    seen = {SKILL_FILE.casefold()}
    total = 0
    for path, encoded in files.items():
        parts = path.split("/")
        if len(path) > 200 or any(
            not part or part.endswith((" ", ".")) or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
            or PureWindowsPath(part).is_reserved() for part in parts
        ):
            raise ValueError("技能目录包含不支持的文件路径。")
        key = path.casefold()
        if key in seen:
            raise ValueError("技能目录包含重复文件。")
        seen.add(key)
        try:
            total += len(b64decode(encoded, validate=True))
        except (Base64Error, ValueError):
            raise ValueError("无法读取技能目录中的文件。") from None
        if total > MAX_BUNDLE:
            raise ValueError("技能附件总大小不能超过 2 MiB。")
    if any("/".join(path.split("/")[:index]).casefold() in seen
           for path in files for index in range(1, len(path.split("/")))):
        raise ValueError("技能目录包含同名文件和文件夹。")
    return files
