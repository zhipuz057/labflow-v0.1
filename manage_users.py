"""Owner-only local user maintenance; never prints access keys to the terminal."""
import argparse
import getpass
import json
import os
import sqlite3
from pathlib import Path

import database


def main():
    parser = argparse.ArgumentParser(description='LabFlow owner 用户管理（无物理删除）')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list-users', help='列出账号、角色和启用状态')
    create = sub.add_parser('create-user', help='新增用户并输出独立钥匙')
    create.add_argument('--name', required=True)
    create.add_argument('--role', choices=['purchaser', 'manager', 'account_viewer'], default='purchaser')
    create.add_argument('--output', required=True, type=Path)
    rotate = sub.add_parser('rotate-key', help='轮换指定账号钥匙；旧钥匙和会话失效')
    rotate.add_argument('--user-id', required=True, type=int)
    rotate.add_argument('--output', required=True, type=Path)
    for command in ('deactivate-user', 'activate-user'):
        command_parser = sub.add_parser(command)
        command_parser.add_argument('--user-id', required=True, type=int)
    change = sub.add_parser('change-role', help='调整角色或更名，保留稳定用户 ID')
    change.add_argument('--user-id', required=True, type=int)
    change.add_argument('--role', choices=list(database.ROLE_PERMISSIONS))
    change.add_argument('--name')
    args = parser.parse_args()
    database.init_db()
    principal = database.authenticate(getpass.getpass('请输入 owner 的 LabFlow 访问钥匙：'))
    database.require_owner(principal)
    if args.command == 'list-users':
        print(json.dumps(database.list_users_for_cli(principal), ensure_ascii=False, indent=2))
    elif args.command in ('deactivate-user', 'activate-user'):
        database.update_user(principal, args.user_id, active=args.command == 'activate-user')
        print('用户启用状态已更新，历史记录完整保留。')
    elif args.command == 'change-role':
        if args.role is None and args.name is None:
            raise ValueError('请提供 --role 或 --name。')
        database.update_user(principal, args.user_id, role=args.role, name=args.name)
        print('用户身份已更新，历史记录完整保留。')
    else:
        # Match the repository's ignore rules even for custom handoff filenames.
        if args.output.suffix != '.json' or 'key' not in args.output.name:
            raise ValueError('钥匙文件请使用包含 key 的 .json 文件名，例如 new-access-keys.json。')
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            credential = (database.create_user(principal, args.name, args.role)
                          if args.command == 'create-user' else database.rotate_access_key(principal, args.user_id))
            output.write(json.dumps([credential], ensure_ascii=False, indent=2) + '\n')
            output.flush()
            os.fsync(output.fileno())
        print(f'钥匙已保存至 {args.output}（文件权限 0600），未输出到终端。')


if __name__ == '__main__':
    try:
        main()
    except (PermissionError, ValueError, sqlite3.Error, OSError) as error:
        raise SystemExit(str(error))
