"""Create initial accounts without shipped passwords or an external API."""
import argparse
from getpass import getpass

from app.core.security import hash_password, is_password_strong
from app.db import SessionLocal
from app.models import (company, company_email, department, organization,
                        task, task_history, user, user_department)  # noqa: F401
from app.models.user import User
from app.services.org import create_organization_with_admin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="Create a system administrator")
    admin.add_argument("--email", required=True)
    admin.add_argument("--name", required=True)
    org = sub.add_parser("create-organization", help="Create an organization and its administrator")
    org.add_argument("--inn", required=True)
    org.add_argument("--name", required=True)
    org.add_argument("--admin-email", required=True)
    org.add_argument("--admin-name", required=True)
    args = parser.parse_args()
    email = (args.email if args.command == "create-admin" else args.admin_email).strip().lower()
    if "@" not in email or len(email) > 255:
        parser.error("Укажите корректный email")
    if args.command == "create-organization" and (not args.inn.isdigit() or len(args.inn) not in (10, 12)):
        parser.error("ИНН должен состоять из 10 или 12 цифр")
    password = getpass("Новый пароль: ")
    if not is_password_strong(password):
        parser.error("Пароль: от 10 символов, строчные и прописные латинские буквы, цифры и спецсимвол")
    if password != getpass("Повторите пароль: "):
        parser.error("Пароли не совпадают")
    with SessionLocal() as db:
        if db.query(User).filter(User.email == email).first():
            parser.error("Такой email уже зарегистрирован")
        if args.command == "create-admin":
            db.add(User(full_name=args.name.strip(), email=email,
                        password_hash=hash_password(password), is_system_admin=True,
                        is_active=True, org_id=None, org_role=None))
            db.commit()
        else:
            create_organization_with_admin(
                db, short_name=args.name.strip(), full_name=args.name.strip(), inn=args.inn,
                ogrn=None, ceo_name=None, status="ACTIVE", admin_full_name=args.admin_name.strip(),
                admin_short_name=None, admin_email=email, admin_password=password,
            )
    print("Создано. Пароль не сохраняется в файлах или выводе команды.")


if __name__ == "__main__":
    main()
