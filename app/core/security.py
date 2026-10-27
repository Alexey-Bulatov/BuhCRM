import re
import bcrypt

PASSWORD_REGEX = re.compile(
    r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)'
    r'(?=.*[!@#$%^&*()_\+\-\=\[\]{};\'":,.<>\/\?]).{10,}$'
)

def is_password_strong(pw: str) -> bool:
    return bool(PASSWORD_REGEX.match(pw or ""))

def hash_password(plain: str) -> str:
    # bcrypt автоматически добавляет соль внутрь хеша
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), pw_hash.encode("utf-8"))
    except Exception:
        return False
