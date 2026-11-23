"""Assign the retained records from the previous single-user installation."""

import argparse

from .auth import username_identity
from .config import ROOT, Settings
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="An existing account that owns the entire old workspace")
    args = parser.parse_args()
    try:
        _, key = username_identity(args.username)
    except ValueError as err:
        parser.error(str(err))
    directory = Settings.load().data_dir
    database = directory / "assistant.sqlite3"
    if not database.is_file():
        parser.error("请先启动项目并注册接收旧记录的账号。")
    store = Store(database, ROOT / "data")
    try:
        user = store.user_by_name(key)
        if not user:
            parser.error("该账号尚未注册。")
        conversations, uploads = store.assign_legacy(user["id"])
        print(f"已为 {user['username']} 关联 {conversations} 个旧会话和 {uploads} 个旧文件。")
    finally:
        store.close()


if __name__ == "__main__":
    main()
