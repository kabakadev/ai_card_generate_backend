# routes/admin/__init__.py
"""
Admin routes package - Refactored for security and maintainability.
"""

from .users import AdminDeleteUsers, AdminListUsers, AdminOnlineUsers, AdminUserStats
from .demo_users import AdminCreateDemoUsers, AdminCheckUsernames
from .deletion import AdminDeleteUsersByIds

__all__ = [
    "AdminDeleteUsers",
    "AdminListUsers", 
    "AdminOnlineUsers",
    "AdminUserStats",
    "AdminCreateDemoUsers",
    "AdminCheckUsernames",
    "AdminDeleteUsersByIds",
]