"""Copy the local SQLite database into an empty configured MySQL database."""
import os
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, insert, select

from bidding.config import DATA
from bidding.storage import metadata


def main():
    url = os.environ.get('DATABASE_URL', '')
    if not url.startswith('mysql+'):
        raise ValueError('Configure a MySQL DATABASE_URL first')
    source_path = DATA / 'bidding.sqlite3'
    if not source_path.exists():
        raise ValueError('Source SQLite database does not exist')
    backup_path = source_path.with_name('bidding-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.sqlite3.bak')
    with sqlite3.connect(source_path) as source_db, sqlite3.connect(backup_path) as backup_db:
        source_db.backup(backup_db)
    source = create_engine('sqlite:///' + str(backup_path))
    target = create_engine(url, pool_pre_ping=True, hide_parameters=True)
    try:
        metadata.create_all(target)
        counts = {}
        with source.connect() as src, target.begin() as dst:
            for table in metadata.sorted_tables:
                if dst.execute(select(func.count()).select_from(table)).scalar_one():
                    raise ValueError('Destination must be empty: ' + table.name)
            for table in metadata.sorted_tables:
                count = 0
                result = src.execute(select(table))
                while rows := result.mappings().fetchmany(100):
                    dst.execute(insert(table), [dict(row) for row in rows])
                    count += len(rows)
                actual = dst.execute(select(func.count()).select_from(table)).scalar_one()
                if actual != count:
                    raise ValueError('Row count mismatch: ' + table.name)
                counts[table.name] = actual
        print({'backup': str(backup_path), 'migrated_rows': counts})
    finally:
        source.dispose()
        target.dispose()


if __name__ == '__main__':
    main()
