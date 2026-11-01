"""Worker SQLite connections are opened and closed on the owning thread."""
from contextlib import closing
import asyncio
import logging
import sqlite3
import threading

import pytest

import main
from memory.metrics import MetricsStore


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel'])
def test_worker_owns_and_closes_its_metrics_connection(tmp_path, monkeypatch, outcome):
    creator = threading.get_ident()
    opened, closed = [], []
    path = tmp_path/'worker.db'

    class TrackedMetrics(MetricsStore):
        def __init__(self):
            opened.append(threading.get_ident())
            super().__init__(path)

        def close(self):
            connection = getattr(self._local, 'conn', None)
            super().close()
            if connection is not None:
                try:
                    connection.execute('SELECT 1')
                except sqlite3.ProgrammingError as error:
                    assert 'closed database' in str(error)
                    closed.append(threading.get_ident())
                else:
                    raise AssertionError('Worker connection is still open')

    monkeypatch.setattr(main, '_get_metrics_store', TrackedMetrics)
    monkeypatch.setattr(logging.getLogger('langhire.lifecycle'), 'level', logging.INFO)

    async def run():
        logging.getLogger('langhire.lifecycle').info('local worker test')
        if outcome=='failure':
            raise RuntimeError('local worker failure')
        if outcome=='cancel':
            raise asyncio.CancelledError

    state = {'log': [], 'running': False}
    thread = main._run_async_in_thread(run, state)
    thread.join(10)
    assert not thread.is_alive()
    assert state['error'] == {'success':None, 'failure':'local worker failure', 'cancel':'cancelled'}[outcome]
    assert opened == closed == [thread.ident] and thread.ident != creator
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT message FROM run_logs WHERE message=?', ('local worker test',)).fetchall() == [('local worker test',)]
