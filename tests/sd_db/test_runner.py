"""Queue selection and ownership are transactions, including competing connections."""
import tempfile
import threading
import unittest
from pathlib import Path

from sd_db import runner, workflow
from sd_db.database import connect
from sd_db.errors import SdDbError
from sd_db.migrate import initialise
from sd_db.workflow import item_state
from sd_db.writes import create_assignment, create_item, upsert_repo


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'sd.db'
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, '/fixture/repo', remote='/fixture/remote')
        self.items = [create_item(self.db, kind='task', title=f'Task {n}', repo='/fixture/repo', branch=f'work/{n}', status='ready') for n in range(4)]

    def claim(self, ident, db=None):
        return runner.claim(db or self.db, ident, owner='fixture', work_root=self.root/'work', retention_root=self.root/'retained')

    def test_a_followup_with_a_repository_and_a_branch_is_not_runnable(self):
        """sd:809. A task prepared for a run and then reclassified keeps its
        repository and branch, and `create_item` accepts both for any kind, so
        the kind is what refuses: readiness, enqueue and claim alike."""
        prepared = create_item(self.db, kind='task', title='Prepared then reclassified',
                               repo='/fixture/repo', branch='work/prepared', status='planning')
        workflow.edit_item(self.db, prepared, {'kind': 'followup'}, who='alex')
        by_hand = create_item(self.db, kind='followup', title='Configured by hand',
                              repo='/fixture/repo', branch='work/by-hand', status='ready')
        reason = 'item {} is a followup item; an agent runs only work, task and report items'
        for item in (prepared, by_hand):
            with self.subTest(item=item):
                row = item_state(self.db, item)['item']
                self.assertEqual((row['kind'], row['repo'], bool(row['branch'])), ('followup', '/fixture/repo', True))
                with self.assertRaisesRegex(runner.RunnerRefused, reason.format(item)):
                    runner.enqueue(self.db, [item], who='operator')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assignment').fetchone()[0], 0)
        legacy = create_assignment(self.db, item=by_hand, role='author', status='queued')
        with self.assertRaisesRegex(runner.RunnerRefused, 'is a followup item'):
            self.claim(legacy)
        self.assertEqual(runner.active_runs(self.db), [])

    def test_duplicate_branch_selection_is_atomic(self):
        self.db.execute('UPDATE item SET branch = ? WHERE id = ?', ('work/0',self.items[1]))
        with self.assertRaisesRegex(runner.RunnerRefused, 'share'):
            runner.enqueue(self.db,self.items[:2],parallel=True, who="operator")
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assignment').fetchone()[0],0)

    def test_stale_selection_is_atomic(self):
        revisions = {item:item_state(self.db,item)['revision'] for item in self.items[:2]}
        self.db.execute('UPDATE item SET title = ? WHERE id = ?', ('changed',self.items[1]))
        with self.assertRaisesRegex(runner.RunnerRefused, 'changed'):
            runner.enqueue(self.db,self.items[:2],expected_revisions=revisions, who="operator")
        self.assertEqual(runner.queued(self.db),[])

    def test_two_connections_only_claim_once(self):
        ident = runner.enqueue(self.db,self.items[:1], who="operator")[0]['id']
        results=[]
        barrier=threading.Barrier(2)
        def contender():
            connection=connect(self.database)
            try:
                barrier.wait()
                results.append(self.claim(ident,connection))
            finally:
                connection.close()
        threads=[threading.Thread(target=contender) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(len(runner.active_runs(self.db)),1)

    def test_parallel_branches_share_repo_but_exclusive_waits(self):
        batch=runner.enqueue(self.db,self.items[:2],parallel=True, who="operator")
        first=self.claim(batch[0]['id']);second=self.claim(batch[1]['id'])
        self.assertIsNotNone(first);self.assertIsNotNone(second)
        exclusive=runner.enqueue(self.db,self.items[2:3], who="operator")[0]
        self.assertIsNone(self.claim(exclusive['id']))

    def test_earlier_exclusive_has_priority(self):
        exclusive=runner.enqueue(self.db,self.items[:1], who="operator")[0]
        later=runner.enqueue(self.db,self.items[1:2],parallel=True, who="operator")[0]
        self.assertIsNone(self.claim(later['id']))
        self.assertIsNotNone(self.claim(exclusive['id']))

    def test_sequential_waits_for_delivery_not_author_exit(self):
        batch=runner.enqueue(self.db,self.items[:2], who="operator")
        self.db.execute("UPDATE assignment SET status='done' WHERE id=?",(batch[0]['id'],))
        self.assertIsNone(self.claim(batch[1]['id']))
        runner.record_merge(self.db,self.items[0],evidence={'url':'https://example.invalid/pr/1','head':'a'*40,'merge_commit':'b'*40,'base':'main','repository':'fixture/repo','observed_at':'2026-09-08T00:00:00+00:00'})
        self.assertIsNotNone(self.claim(batch[1]['id']))

    def test_failed_predecessor_blocks_whole_chain_when_considered(self):
        batch=runner.enqueue(self.db,self.items[:3], who="operator")
        self.db.execute("UPDATE assignment SET status='blocked' WHERE id=?",(batch[0]['id'],))
        self.assertIsNone(self.claim(batch[1]['id']))
        self.assertIsNone(self.claim(batch[2]['id']))
        self.assertEqual(runner.queue_state(self.db,batch[2]['id'])['status'],'blocked')

    def test_cycle_rejected_before_claim(self):
        batch=runner.enqueue(self.db,self.items[:2], who="operator")
        self.db.execute('UPDATE assignment SET after=? WHERE id=?',(batch[1]['id'],batch[0]['id']))
        with self.assertRaisesRegex(runner.RunnerRefused,'cycle'):
            self.claim(batch[0]['id'])
        self.assertEqual(runner.active_runs(self.db),[])

    def test_an_item_with_no_branch_is_refused_naming_it(self):
        """sd:235, criterion 7. `create_item` accepts a repository with no
        branch, and `_item` is what refuses: readiness names the item, both
        enqueues refuse naming it, and nothing is queued."""
        unbranched = create_item(self.db, kind='task', title='No branch yet',
                                 repo='/fixture/repo', branch=None, status='ready')
        reason = f'item {unbranched} needs a valid branch'
        with self.assertRaisesRegex(runner.RunnerRefused, reason):
            runner.enqueue(self.db, [unbranched], who='operator')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assignment').fetchone()[0], 0)
        legacy = create_assignment(self.db, item=unbranched, role='author', status='queued')
        with self.assertRaisesRegex(runner.RunnerRefused, reason):
            self.claim(legacy)
        self.assertEqual(runner.active_runs(self.db), [])

    def test_a_chain_is_checked_acyclic_when_it_is_created(self):
        """sd:235, criterion 8. `enqueue` walks every `after` edge before it
        returns, so a cycle in the table refuses the chain being created,
        naming the assignment the walk met twice, and the refusal leaves no
        row behind. A missing predecessor never reaches the walk: the
        `after` foreign key refuses it at the insert.
        `test_cycle_rejected_before_claim` covers the same walk at the claim."""
        batch = runner.enqueue(self.db, self.items[:2], who='operator')
        self.db.execute('UPDATE assignment SET after=? WHERE id=?', (batch[1]['id'], batch[0]['id']))
        before = self.db.execute('SELECT COUNT(*) FROM assignment').fetchone()[0]
        with self.assertRaisesRegex(runner.RunnerRefused, f"dependency cycle at {batch[0]['id']}"):
            runner.enqueue(self.db, self.items[2:3], who='operator')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assignment').fetchone()[0], before)
        self.assertEqual(runner.active_runs(self.db), [])

    def test_cancel_running_records_request_without_false_terminal(self):
        row=runner.enqueue(self.db,self.items[:1], who="operator")[0]
        self.claim(row['id'])
        current=runner.queue_state(self.db,row['id'])
        changed=runner.request_cancel(self.db,row['id'],expected_revision=current['revision'], who="operator")
        self.assertEqual(changed['status'],'running')
        self.assertIn('cancelled by',changed['run']['cancel_requested'])
        self.assertIsNone(changed['run']['released_at'])
        with self.assertRaisesRegex(runner.RunnerRefused,'changed'):
            runner.request_cancel(self.db,row['id'],expected_revision=current['revision'], who="operator")

    def test_release_last_and_quarantine_holds_lease(self):
        row=runner.enqueue(self.db,self.items[:1], who="operator")[0]
        claim=self.claim(row['id']);ident=claim['run']['id']
        runner.begin_ending(self.db,ident,outcome='blocked',detail='fixture')
        with self.assertRaises(runner.RunnerRefused): runner.release(self.db,ident)
        runner.update_run(self.db,ident,end_step='retained',quarantine='holder pid 123')
        with self.assertRaises(runner.RunnerRefused): runner.release(self.db,ident)
        runner.update_run(self.db,ident,quarantine=None)
        runner.release(self.db,ident)
        state=runner.queue_state(self.db,row['id'])
        self.assertEqual(state['status'],'blocked')
        self.assertEqual(state['run']['end_step'],'released')
        runner.requeue(self.db,row['id'],expected_revision=state['revision'], who="operator")
        again=self.claim(row['id'])
        self.assertEqual(again['run']['run'],2)
        self.assertNotEqual(claim['run']['work_path'],again['run']['work_path'])

    def test_heartbeat_replaces_exactly_one_row(self):
        for n in range(3): runner.heartbeat(self.db,{'healthy':True,'interval_seconds':10,'tick':n})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM state WHERE kind='heartbeat' AND key='runner'").fetchone()[0],1)
        self.assertEqual(runner.heartbeat_state(self.db)['tick'],2)
        self.assertTrue(runner.heartbeat_state(self.db)['ok'])

    def hand_written_heartbeat(self, body):
        # Only `runner.heartbeat` writes this row, and it serialises a dict;
        # a row of any other shape got there by hand (sd:934).
        self.db.execute("INSERT INTO state (kind, key, timestamp, body) VALUES ('heartbeat', 'runner', '2026-09-15T00:00:00+00:00', ?) "
                        "ON CONFLICT(key) WHERE kind = 'heartbeat' AND key = 'runner' DO UPDATE SET body = excluded.body", (body,))
        self.db.commit()

    def test_heartbeat_state_refuses_a_row_that_is_not_a_json_object(self):
        # sd:934. The readers index the state as a dict; a hand-edited row
        # of another shape raised TypeError past every handler they name.
        # The refusal is the library's, so `runner status` and the dashboard
        # catch it where they already catch SdDbError, and it names what it
        # found and who replaces the row.
        for literal, found in (('[1, 2]', 'a JSON array'), ('"pulse"', 'a JSON string'), ('7', 'a JSON number'),
                               ('true', 'a JSON boolean'), ('null', 'JSON null'), ('not json', 'not JSON'), (None, 'an empty body')):
            with self.subTest(literal=literal):
                self.hand_written_heartbeat(literal)
                with self.assertRaises(SdDbError) as caught:
                    runner.heartbeat_state(self.db)
                self.assertIn(f'the runner heartbeat body is {found}', str(caught.exception))
                self.assertIn("the runner's next tick replaces it", str(caught.exception))
        runner.heartbeat(self.db, {'healthy': True, 'interval_seconds': 10})
        self.assertTrue(runner.heartbeat_state(self.db)['ok'])

    def test_recovery_binds_snapshot_identity_and_clears_delivery_proof(self):
        assignment = runner.enqueue(self.db, self.items[:1], who="operator")[0]
        current = self.claim(assignment['id'])['run']
        record = {**current, 'journal_version': current['journal_version'] + 2, 'delivery_proof': '{"stale":true}'}
        snapshot = runner.revision(runner.recovery_snapshot(self.db, current['id']))
        for altered, expected in (({**record, 'owner': 'foreign'}, 'identity'), (record, 'selection changed')):
            before = tuple(self.db.iterdump())
            with self.assertRaisesRegex(runner.RunnerRefused, expected):
                runner.recover_from_journal(self.db, altered, expected_snapshot=snapshot if expected == 'identity' else 'stale')
            self.assertEqual(tuple(self.db.iterdump()), before)
        recovered = runner.recover_from_journal(self.db, record, expected_snapshot=snapshot)
        self.assertEqual(recovered['outcome'], 'blocked')
        self.assertIsNone(recovered['delivery_proof'])
        self.assertIsNone(recovered['released_at'])

    def test_a_journal_written_before_migration_018_still_recovers(self):
        """sd:2581: 018 added `detached_from`; a journal written before it has no such field."""
        assignment = runner.enqueue(self.db, self.items[:1], who="operator")[0]
        current = self.claim(assignment['id'])['run']
        record = {name: value for name, value in current.items() if name != 'detached_from'}
        record['journal_version'] = current['journal_version'] + 2
        snapshot = runner.revision(runner.recovery_snapshot(self.db, current['id']))
        recovered = runner.recover_from_journal(self.db, record, expected_snapshot=snapshot)
        self.assertIsNone(recovered['detached_from'])
        self.assertEqual(recovered['outcome'], 'blocked')

    def test_recovery_refuses_competing_exclusive_lease_atomically(self):
        assignment = runner.enqueue(self.db, self.items[:1], who="operator")[0]
        current = self.claim(assignment['id'])['run']
        self.db.execute('DELETE FROM runner_lease WHERE run=?', (current['id'],))
        self.db.execute('DELETE FROM runner_run WHERE id=?', (current['id'],))
        second = runner.enqueue(self.db, self.items[1:2], who="operator")[0]
        self.assertIsNotNone(self.claim(second['id']))
        snapshot = runner.revision(runner.recovery_snapshot(self.db, current['id'], assignment=current['assignment']))
        before = tuple(self.db.iterdump())
        with self.assertRaisesRegex(runner.RunnerRefused, 'existing lease'):
            runner.recover_from_journal(self.db, current, expected_snapshot=snapshot)
        self.assertEqual(tuple(self.db.iterdump()), before)


class SessionRecord(unittest.TestCase):
    """What a run's session leaves on the row: notes, one `start` cost row, a named hard stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'sd.db'
        initialise(self.database)
        self.db = connect(self.database)
        self.addCleanup(self.db.close)
        upsert_repo(self.db, '/fixture/repo', remote='/fixture/remote')
        self.item = create_item(self.db, kind='task', title='Task', repo='/fixture/repo', branch='work/0', status='ready')
        self.db.execute("INSERT INTO bill (name, cost_basis) VALUES ('anthropic', 'subscription')")
        self.db.execute("INSERT INTO provider (name, enabled, author_rank) VALUES ('claude', 1, 1)")
        self.db.commit()
        assignment = runner.enqueue(self.db, [self.item], who="operator")[0]
        self.request = runner.claim(self.db, assignment['id'], owner='fixture', work_root=self.root/'work', retention_root=self.root/'retained')
        self.run = self.request['run']['id']

    def notes(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM note WHERE item = ? AND session = ? ORDER BY id', (self.item, self.run))]

    def test_session_notes_are_filed_on_the_item_with_the_run_as_session(self):
        ids = runner.record_session_notes(self.db, self.run, [{'kind': 'followup', 'body': ' a '}, {'kind': 'question', 'body': 'b\nOption: Yes (Recommended)\nOption: No'}])
        self.assertEqual(len(ids), 2)
        self.assertEqual([(n['kind'], n['body'], n['resolved_at']) for n in self.notes()], [('followup', 'a', None), ('question', 'b\nOption: Yes (Recommended)\nOption: No', None)])
        self.assertEqual(runner.record_session_notes(self.db, self.run, []), [])

    def test_session_notes_of_other_kinds_or_without_body_file_nothing(self):
        for notes in ([{'kind': 'exec', 'body': 'x'}], [{'kind': 'status_change', 'body': 'x'}], [{'kind': 'comment', 'body': 'x'}],
                      [{'kind': 'followup', 'body': '  '}], [{'kind': 'followup', 'body': 1}], [{'kind': 'followup', 'body': 'ok'}, 'junk']):
            with self.assertRaises(runner.RunnerRefused):
                runner.record_session_notes(self.db, self.run, notes)
        self.assertEqual(self.notes(), [])

    def test_start_session_cost_is_one_run_row_written_once(self):
        first = runner.record_session_cost(self.db, self.run, provider='claude', bill='anthropic', tokens_in=10, tokens_out=5, usd=0.25)
        again = runner.record_session_cost(self.db, self.run, provider='claude', bill='anthropic', tokens_in=999, tokens_out=999, usd=9.0)
        self.assertEqual(first, again)
        rows = [dict(r) for r in self.db.execute('SELECT * FROM cost')]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['source'], rows[0]['provider'], rows[0]['bill'], rows[0]['role'], rows[0]['repo'], rows[0]['assignment'], rows[0]['pass'], rows[0]['call_id']),
                         ('run', 'claude', 'anthropic', 'author', '/fixture/repo', self.request['id'], self.run, f'session:{self.run}'))
        self.assertEqual((rows[0]['tokens_in'], rows[0]['tokens_out'], rows[0]['usd']), (10, 5, 0.25))
        self.assertEqual(runner.session_cost(self.db, self.run)['id'], first)
        self.assertIsNone(runner.session_cost(self.db, 'no-such-run'))

    def test_start_session_cost_refuses_bad_numbers_and_unknown_provider(self):
        for kwargs in (dict(tokens_in=-1, tokens_out=0, usd=0), dict(tokens_in='10', tokens_out=0, usd=0), dict(tokens_in=0, tokens_out=0, usd=-0.1),
                       dict(tokens_in=0, tokens_out=0, usd='free'),
                       # Not finite: SQLite stores `nan` as NULL and `inf` as a cost no
                       # sum can carry, and `float` of a huge `int` raised `OverflowError`
                       # (sd:1219); a token count past SQLite's INTEGER did the same.
                       dict(tokens_in=0, tokens_out=0, usd=float('nan')), dict(tokens_in=0, tokens_out=0, usd=float('inf')),
                       dict(tokens_in=0, tokens_out=0, usd=10**400), dict(tokens_in=2**63, tokens_out=0, usd=0)):
            with self.subTest(**{key: repr(value) for key, value in kwargs.items()}), self.assertRaises(runner.RunnerRefused):
                runner.record_session_cost(self.db, self.run, provider='claude', bill='anthropic', **kwargs)
        with self.assertRaisesRegex(runner.RunnerRefused, "provider 'nobody' is not registered"):
            runner.record_session_cost(self.db, self.run, provider='nobody', bill='anthropic', tokens_in=None, tokens_out=None, usd=None)
        with self.assertRaisesRegex(runner.RunnerRefused, "bill 'nobody' is not registered"):
            runner.record_session_cost(self.db, self.run, provider='claude', bill='nobody', tokens_in=None, tokens_out=None, usd=None)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cost').fetchone()[0], 0)
        ident = runner.record_session_cost(self.db, self.run, provider='claude', bill='anthropic', tokens_in=None, tokens_out=None, usd=None)
        self.assertEqual(dict(self.db.execute('SELECT tokens_in, tokens_out, usd FROM cost WHERE id = ?', (ident,)).fetchone()), {'tokens_in': None, 'tokens_out': None, 'usd': None})

    def test_hard_stop_leaves_an_open_followup_naming_it(self):
        ident = runner.record_hard_stop(self.db, self.run, kind='failing test', evidence='tests/test_x.py::test_y exited 1')
        self.assertEqual([(n['id'], n['kind'], n['body'], n['resolved_at']) for n in self.notes()],
                         [(ident, 'followup', 'hard stop: failing test: tests/test_x.py::test_y exited 1', None)])
        with self.assertRaisesRegex(runner.RunnerRefused, 'hard stop kind'):
            runner.record_hard_stop(self.db, self.run, kind='bored', evidence='x')
        self.assertEqual(runner.HARD_STOPS, ('failing test', 'blocking review finding open past the cap', 'write outside the repository'))

    def test_ship_receipt_is_empty_off_github_and_for_an_unknown_run(self):
        self.assertEqual(runner.ship_receipt(self.db, self.run), {})
        with self.assertRaises(runner.RunnerRefused):
            runner.ship_receipt(self.db, 'no-such-run')

    def test_check_record_round_trips_and_a_second_record_replaces_the_first(self):
        self.assertIsNone(runner.check_record(self.db, self.run))
        first = runner.record_check(self.db, self.run, head='a' * 40, tree='b' * 40, exit_code=0, argv=['sd-check', '--json'],
                                    checks=[{'name': 'test', 'status': 'pass'}], now='2026-09-12T10:00:00+00:00')
        self.assertEqual(first, {'head': 'a' * 40, 'tree': 'b' * 40, 'exit_code': 0, 'argv': ['sd-check', '--json'],
                                 'checks': [{'name': 'test', 'status': 'pass'}], 'recorded_at': '2026-09-12T10:00:00+00:00'})
        self.assertEqual(runner.check_record(self.db, self.run), first)
        # A re-run is the row's latest check, not a second one: one row per
        # run id, and the replaced body is gone.
        second = runner.record_check(self.db, self.run, head='c' * 40, tree='d' * 40, exit_code=1, argv=['sd-check', '--json'], checks=None)
        rows = self.db.execute("SELECT key, timestamp, body FROM state WHERE kind = 'check'").fetchall()
        self.assertEqual([row['key'] for row in rows], [self.run])
        self.assertEqual(rows[0]['timestamp'], second['recorded_at'])
        self.assertEqual(runner.check_record(self.db, self.run), second)
        self.assertEqual((second['tree'], second['exit_code'], second['checks']), ('d' * 40, 1, None))
        self.assertNotEqual(second['recorded_at'], first['recorded_at'])

    def test_check_record_refuses_what_is_not_a_check_of_a_run(self):
        for bad in ({'head': 'HEAD'}, {'tree': 'b' * 39}, {'exit_code': '0'}, {'checks': {'name': 'test'}}):
            with self.assertRaises(runner.RunnerRefused):
                runner.record_check(self.db, self.run, **{'head': 'a' * 40, 'tree': 'b' * 40, 'exit_code': 0, 'argv': [], 'checks': [], **bad})
        with self.assertRaisesRegex(runner.RunnerRefused, 'absent'):
            runner.record_check(self.db, 'no-such-run', head='a' * 40, tree='b' * 40, exit_code=0, argv=[], checks=[])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM state WHERE kind = 'check'").fetchone()[0], 0)


if __name__ == '__main__': unittest.main()
