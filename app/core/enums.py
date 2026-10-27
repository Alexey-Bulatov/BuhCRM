# app/core/enums.py
from enum import Enum

class OrgRole(str, Enum):
    ORG_ADMIN = "ORG_ADMIN"
    ORG_USER = "ORG_USER"

class TaskStatus(str, Enum):
    OPEN = "OPEN"
    DONE = "DONE"
    CANCELED = "CANCELED"
    NEW = "OPEN"      # обратная совместимость

class TaskPriority(str, Enum):
    URGENT = "URGENT"
    MEDIUM = "MEDIUM"
    LONGTERM = "LONGTERM"
