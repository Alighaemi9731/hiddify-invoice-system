"""Seed historical migration schemas without using today's ORM INSERT column list."""
from enum import Enum

from sqlalchemy import MetaData, Table


async def insert_legacy(session, instance):
    connection = await session.connection()
    model_table = instance.__table__
    table = await connection.run_sync(lambda conn: Table(
        model_table.name, MetaData(), autoload_with=conn))
    values = {}
    for col in table.columns:
        if col.name in instance.__dict__:
            value = instance.__dict__[col.name]
        else:
            default = model_table.c[col.name].default
            if default is None or not default.is_scalar:
                continue
            value = default.arg
        values[col.name] = value.value if isinstance(value, Enum) else value
    await session.execute(table.insert().values(values))
