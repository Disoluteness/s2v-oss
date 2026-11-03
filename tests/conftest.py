"""Test-wide configuration.

The one thing this file does is make every SQLite engine built by the test
suite enforce foreign keys, which SQLite otherwise ignores. Production runs on
MySQL/InnoDB, where the same foreign keys *are* enforced, so without this the
suite happily accepts rows that a real deployment rejects with IntegrityError
1452 (HTTP 500). Tests import ``create_engine`` from ``sqlalchemy`` at module
import time, so the swap has to happen here, before test modules load.
"""

import sqlalchemy

from apps.api.app.db import enforce_sqlite_foreign_keys

_create_engine = sqlalchemy.create_engine


def _foreign_key_enforcing_create_engine(*args, **kwargs):
    return enforce_sqlite_foreign_keys(_create_engine(*args, **kwargs))


sqlalchemy.create_engine = _foreign_key_enforcing_create_engine
