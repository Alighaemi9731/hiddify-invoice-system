"""The restore must tolerate a dump taken by a newer pg_dump (17) than the server (16):
pg_dump 17 emits `SET transaction_timeout = 0;`, a GUC PG16 rejects — strip it on restore."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bk.db")
os.environ.setdefault("SECRET_KEY", "k")

from app.services.backup import _strip_incompatible_sets  # noqa: E402

_DUMP = b"""--
-- PostgreSQL database dump
--

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';

CREATE TABLE public.t (id integer);
"""


def test_strips_pg17_transaction_timeout():
    out = _strip_incompatible_sets(_DUMP)
    assert b"transaction_timeout" not in out, "PG17-only SET must be removed"
    # everything else is preserved untouched
    assert b"SET statement_timeout = 0;" in out
    assert b"SET client_encoding = 'UTF8';" in out
    assert b"CREATE TABLE public.t (id integer);" in out


def test_no_transaction_timeout_is_noop():
    sql = b"SET statement_timeout = 0;\nCREATE TABLE x(i int);\n"
    assert _strip_incompatible_sets(sql) == sql
