"""SQLAlchemy 声明式映射基类。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """业务库全部 ORM 表的共同元数据入口。"""
