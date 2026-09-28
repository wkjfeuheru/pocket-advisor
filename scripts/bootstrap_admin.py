"""引导管理员账号：创建或提升一个管理员，并把其余用户降为普通用户。

角色落在 ``finance.users.is_admin``（``migrations/001_schema.sql``）。

用法::

    python -m scripts.bootstrap_admin --username admin --password 'YourPassw0rd'
    python -m scripts.bootstrap_admin --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys

from app.crud.user_crud import get_user_store


def _print_roles(store) -> int:
    rows = store.list_users()
    if not rows:
        print("库内没有用户。")
        return 0
    admins = 0
    for row in rows:
        mark = "管理员" if row.get("is_admin") else "普通用户"
        if row.get("is_admin"):
            admins += 1
        print(f"  {row.get('customer_id'):<14} {row.get('username'):<20} {mark}")
    print(f"共 {len(rows)} 个用户，其中管理员 {admins} 个。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="引导管理员账号并同步库内角色")
    parser.add_argument("--username", default="admin", help="管理员用户名（默认 admin）")
    parser.add_argument(
        "--password",
        default=os.environ.get("FINANCE_ADMIN_PASSWORD", ""),
        help="管理员密码；也可用环境变量 FINANCE_ADMIN_PASSWORD",
    )
    parser.add_argument("--display-name", default="", help="显示名（默认与用户名相同）")
    parser.add_argument(
        "--keep-others",
        action="store_true",
        help="保留其他用户的管理员角色（默认把其他人都降为普通用户）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印当前角色，不写入任何数据")
    args = parser.parse_args(argv)

    store = get_user_store()
    if args.dry_run:
        print("当前用户角色：")
        return _print_roles(store)

    if not args.password:
        parser.error("必须提供 --password 或环境变量 FINANCE_ADMIN_PASSWORD")
    from app.core.config import IS_PRODUCTION
    from app.crud.user_crud import _validate_password

    try:
        _validate_password(args.password)
    except ValueError as exc:
        parser.error(str(exc))
    if IS_PRODUCTION and len(args.password) < 12:
        parser.error("生产环境管理员密码至少 12 个字符")

    existing = store.get_user_by_username(args.username)
    if existing:
        customer_id = existing["customer_id"]
        store.set_password(customer_id, args.password)
        store.set_admin(customer_id, True)
    else:
        created = store.register(
            username=args.username,
            password=args.password,
            display_name=args.display_name,
            is_admin=True,
        )
        customer_id = created["customer_id"]

    if not args.keep_others:
        for row in store.list_users():
            other = row.get("customer_id")
            if other and other != customer_id and row.get("is_admin"):
                store.set_admin(other, False)

    print("当前用户角色：")
    return _print_roles(store)


if __name__ == "__main__":
    sys.exit(main())
