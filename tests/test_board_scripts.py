#!/usr/bin/env python3
"""Watcher/gate regression tests. All state uses temporary BOARD_DATA; pushes are mocked.

    /usr/bin/python3 -m unittest discover -s tests -p test_board_scripts.py -v

Set BOARD_SCRIPTS_DIR when the installed Hermes scripts are elsewhere.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

SCRIPTS = Path(os.environ.get('BOARD_SCRIPTS_DIR', '~/.hermes/scripts')).expanduser()


def load(name, directory):
    path = SCRIPTS / (name + '.py')
    if not path.exists():
        raise unittest.SkipTest('Hermes script is not installed: ' + str(path))
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {'BOARD_DATA': directory,
                                     'BOARD_ACK_KEY_FILE': os.path.join(directory, 'hermes.key'),
                                     'BOARD_WATCH_MDLOG': os.path.join(directory, 'board.md'),
                                     'BOARD_WATCH_INBOX': os.path.join(directory, 'inbox.jsonl')}, clear=False):
        spec.loader.exec_module(module)
    return module


def message(mid, channel='will', author='will-hermes'):
    return {'id': mid, 'channel': channel, 'author': author, 'topic': 'journal',
            'text': 'A whole post.\n\n' + 'body ' * 300, 'files': [], 'epoch': time.time()}


def listing(**heads):
    return [{'channel': channel, 'last_id': head or None, 'last_epoch': None,
             'idle_days': None} for channel, head in heads.items()]


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='board-watcher-test-')
        self.addCleanup(self.tmp.cleanup)
        self.w = load('agent-board-watch', self.tmp.name)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_cursor_migration_preserves_legacy_and_new_json_wins(self):
        old = Path(self.w.LEGACY_CURSOR_FILE)
        old.write_text('84\n')
        cursors = self.w.read_cursor(listing(will=100, house=95))
        self.assertEqual(cursors, {'will': 84, 'house': 84})
        self.assertEqual(json.loads(Path(self.w.CURSOR_FILE).read_text()), cursors)
        self.assertEqual(old.read_text(), '84\n')
        self.w.write_cursor({'will': 90, 'house': 85})
        self.assertEqual(self.w.read_cursor(listing(will=110, house=105, new=99)),
                         {'will': 90, 'house': 85, 'new': 99})
        self.assertEqual(old.read_text(), '84\n')

    def test_first_start_seeds_head_and_bad_json_does_not_rewind(self):
        self.assertEqual(self.w.read_cursor(listing(will=100, house=95)), {'will': 100, 'house': 95})
        Path(self.w.CURSOR_FILE).write_text('bad json')
        with self.assertRaises(ValueError):
            self.w.read_cursor(listing(will=200))

    def test_outbox_is_keyed_by_channel_and_message_and_old_entries_migrate(self):
        self.w.outbox_add(message(84))
        self.w.outbox_add(message(84))
        self.w.outbox_add(message(84, 'house'))
        entries = self.w.outbox_load()
        self.assertEqual([(e['channel'], e['id']) for e in entries], [('will', 84), ('house', 84)])
        old_msg = message(70)
        old_msg.pop('channel')
        self.w.outbox_write([{'id': 70, 'msg': old_msg, 'chat': 'old-chat'}])
        migrated = self.w.outbox_load()[0]
        self.assertEqual(migrated['channel'], 'will')
        self.assertEqual(migrated['chat'], 'old-chat')

    def test_per_channel_contiguity_does_not_advance_even_to_before_pending(self):
        self.w.write_acked({'will': 80, 'house': 60})
        self.w.outbox_add(message(84))
        with mock.patch.object(self.w, 'post_json') as post:
            self.w.ack_through('identity', 'will', 90)
            post.assert_not_called()
            self.w.ack_through('identity', 'house', 88)
            post.assert_called_once_with('/api/ack', 'identity', {'id': 88, 'channel': 'house'})
        self.assertEqual(self.w.read_acked(), {'will': 80, 'house': 88})
        self.w.outbox_write([])
        with mock.patch.object(self.w, 'post_json') as post:
            self.w.ack_through('identity', 'will', 90)
            post.assert_called_once_with('/api/ack', 'identity', {'id': 90, 'channel': 'will'})
            self.w.ack_through('identity', 'will', 89)
            self.assertEqual(post.call_count, 1)

    def test_unknown_zero_receipt_is_posted_and_failed_ack_is_not_saved(self):
        with mock.patch.object(self.w, 'post_json', side_effect=OSError('down')):
            self.w.ack_through('identity', 'house', 0)
        self.assertEqual(self.w.read_acked(), {})
        with mock.patch.object(self.w, 'post_json') as post:
            self.w.ack_through('identity', 'house', 0)
            post.assert_called_once()
        self.assertEqual(self.w.read_acked(), {'house': 0})

    def test_ack_sync_uses_channels_instead_of_legacy_record(self):
        self.w.write_acked({'will': 100})
        data = {'me': {'identity': 'hamish-hermes', 'acked_id': 900}, 'channels': [
            {'identity': 'hamish-hermes', 'channel': 'will', 'acked_id': 80},
            {'identity': 'hamish-hermes', 'channel': 'house', 'acked_id': None},
            {'identity': 'will-hermes', 'channel': 'will', 'acked_id': 700}]}
        with mock.patch.object(self.w, 'api', return_value=data):
            self.w.sync_acked('identity')
        # Per-channel receipts replace the file, except that the whole-board ('*')
        # receipt is carried over from `me.acked_id`: the server returns it only
        # there, and dropping it made ack_whole_board re-post it on every poll.
        self.assertEqual(self.w.read_acked(), {'will': 80, '*': 900})

    def test_notify_requires_full_post_in_channel_chat_and_names_channel(self):
        msg = message(84)
        with mock.patch.object(self.w, 'publish', return_value='will-chat') as publish, \
             mock.patch.object(self.w, 'push', return_value=True) as push:
            self.assertEqual(self.w.notify(msg), (True, 'will-chat'))
            self.assertIn(msg['text'].strip(), publish.call_args.args[0])
            self.assertEqual(publish.call_args.args[1], 'will')
            self.assertEqual(push.call_args.kwargs['title'], 'board · will · will-hermes [journal]')
            self.assertEqual(push.call_args.kwargs['session'], 'will-chat')
        with mock.patch.object(self.w, 'publish', return_value=''), mock.patch.object(self.w, 'push') as push:
            self.assertEqual(self.w.notify(msg), (False, ''))
            push.assert_not_called()

    def test_publish_passes_persistent_channel_chat_title_to_existing_helper(self):
        proc = subprocess.CompletedProcess([], 0, 'will-session\n', '')
        with mock.patch.object(self.w, 'hermes_python', return_value='/fake/python'), \
             mock.patch.object(self.w.subprocess, 'run', return_value=proc) as run:
            self.assertEqual(self.w.publish('whole body', 'will'), 'will-session')
            command = run.call_args.args[0]
            self.assertEqual(command[-1], 'Agent board · will')
            self.assertIn('from notify_chat import publish', command[2])
        self.assertEqual(list(Path(self.tmp.name).glob('*.md')), [])

    def test_poll_saves_failed_posts_before_cursor_and_channels_move_independently(self):
        cursors = {'will': 80, 'house': 70}
        self.w.write_cursor(cursors)
        with mock.patch.object(self.w, 'api', return_value={'messages': [message(84)]}) as api, \
             mock.patch.object(self.w, 'notify', return_value=(False, 'will-chat')):
            self.w.poll_union('key', {'will', 'house'}, cursors, {'will': None, 'house': None})
        self.assertNotIn('channel', parse_qs(urlsplit(api.call_args.args[0]).query))
        self.assertEqual(cursors, {'will': 84, 'house': 70})
        self.assertEqual(self.w.outbox_load()[0]['chat'], 'will-chat')
        with mock.patch.object(self.w, 'api', return_value={'messages': [message(90, 'house')]}), \
             mock.patch.object(self.w, 'notify', return_value=(True, 'house-chat')):
            self.w.poll_union('key', {'will', 'house'}, cursors, {'will': None, 'house': None})
        self.assertEqual(cursors, {'will': 84, 'house': 90})
        self.assertEqual([(e['channel'], e['id']) for e in self.w.outbox_load()], [('will', 84)])

    def test_unknown_channel_seeds_head_and_does_not_replay(self):
        self.w.write_cursor({'will': 80, 'retired': 12})
        cursors = self.w.read_cursor(listing(will=90, house=95, unused=0))
        self.assertEqual(cursors, {'will': 80, 'house': 95, 'unused': 0, 'retired': 12})
        page = [message(84), message(90, 'house'), message(95, 'house'), message(96, 'house')]
        with mock.patch.object(self.w, 'api', return_value={'messages': page}), \
             mock.patch.object(self.w, 'notify', return_value=(True, 'chat')) as notify:
            self.w.poll_union('key', {'will', 'house', 'unused'}, cursors,
                              {'will': None, 'house': None, 'unused': None})
        self.assertEqual([call.args[0]['id'] for call in notify.call_args_list], [84, 96])
        self.assertEqual(cursors, {'will': 84, 'house': 96, 'unused': 0, 'retired': 12})

    def test_union_merges_channels_and_filters_each_cursor_before_delivery(self):
        cursors = {'will': 84, 'house': 70}
        page = [message(80), message(81, 'house'), message(84), message(85), message(86, 'house')]
        with mock.patch.object(self.w, 'api', return_value={'messages': page}) as api, \
             mock.patch.object(self.w, 'notify', side_effect=lambda msg, session: (msg['channel'] != 'will', msg['channel'] + '-chat')):
            scan, full = self.w.poll_union('key', {'will', 'house'}, cursors,
                                          {'will': None, 'house': None})
        api.assert_called_once()
        self.assertEqual(parse_qs(urlsplit(api.call_args.args[0]).query),
                         {'since': ['70'], 'limit': ['50'], 'wait': ['0']})
        self.assertEqual((scan, full), (86, False))
        self.assertEqual(cursors, {'will': 85, 'house': 86})
        self.assertEqual([(e['channel'], e['id']) for e in self.w.outbox_load()], [('will', 85)])
        with mock.patch.object(self.w, 'post_json') as ack:
            for channel in cursors:
                self.w.ack_through('identity', channel, cursors[channel])
        ack.assert_called_once_with('/api/ack', 'identity', {'id': 86, 'channel': 'house'})
        inbox = [json.loads(line) for line in Path(self.w.INBOX_FILE).read_text().splitlines()]
        self.assertEqual({m['id'] for m in inbox}, {81, 85, 86})

    def test_full_duplicate_page_drains_immediately_then_uses_one_union_wait(self):
        self.w.write_cursor({'will': 100, 'house': 0})
        polls = []
        def api(path, key, **kwargs):
            if path == '/api/channels':
                return {'channels': listing(will=100, house=0)}
            query = parse_qs(urlsplit(path).query)
            self.assertNotIn('channel', query)
            polls.append((int(query['since'][0]), int(query['wait'][0])))
            if len(polls) == 1:
                return {'messages': [message(i) for i in range(1, 51)]}
            if len(polls) == 2:
                return {'messages': [message(101, 'house')]}
            raise KeyboardInterrupt
        with mock.patch.object(self.w, 'read_ack_key', return_value='identity'), \
             mock.patch.object(self.w, 'api', side_effect=api), \
             mock.patch.object(self.w, 'sync_acked'), mock.patch.object(self.w, 'ack_through'), \
             mock.patch.object(self.w, 'notify', return_value=(True, 'chat')) as notify, \
             mock.patch.object(sys, 'argv', ['watcher']):
            with self.assertRaises(KeyboardInterrupt):
                self.w.main()
        self.assertEqual(polls, [(0, 0), (50, 0), (101, self.w.WAIT)])
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(self.w.read_map(self.w.CURSOR_FILE), {'will': 100, 'house': 101})
        self.assertTrue(Path(self.w.HEARTBEAT_FILE).exists())

    def test_quiet_return_is_logged_once_and_survives_retry(self):
        now = time.time()
        msgs = [dict(message(84), epoch=now), dict(message(85), epoch=now + 1)]
        self.w.write_cursor({'will': 80})
        with mock.patch.object(self.w, 'api', return_value={'messages': msgs}), \
             mock.patch.object(self.w, 'publish', return_value='chat'), \
             mock.patch.object(self.w, 'push', return_value=False):
            self.w.poll_union('key', {'will'}, {'will': 80}, {'will': now - 70 * 86400})
        with mock.patch.object(self.w, 'push', return_value=True), \
             mock.patch.object(self.w, 'log') as log:
            self.w.retry_outbox()
        self.assertIn(mock.call('notified #84 by will-hermes in will (quiet 70d)'), log.call_args_list)
        self.assertIn(mock.call('notified #85 by will-hermes in will'), log.call_args_list)

    def test_quiet_return_recovers_previous_epoch_after_restart_and_in_summary(self):
        now = time.time()
        cursors = {'will': 80}
        first = dict(message(84), epoch=now)
        with mock.patch.object(self.w, 'api', side_effect=[{'messages': [first]},
                              {'messages': [dict(message(80), epoch=now - 14 * 86400)]}]) as api, \
             mock.patch.object(self.w, 'publish', return_value='chat'), \
             mock.patch.object(self.w, 'push', return_value=True), \
             mock.patch.object(self.w, 'BURST', 0), mock.patch.object(self.w, 'log') as log:
            self.w.poll_union('key', {'will'}, cursors, {})
        self.assertEqual(parse_qs(urlsplit(api.call_args.args[0]).query),
                         {'channel': ['will'], 'before': ['84'], 'limit': ['1']})
        self.assertIn(mock.call('notified #84 by will-hermes in will (quiet 14d)'), log.call_args_list)

    def test_outbox_write_failure_stops_cursor_and_corrupt_outbox_blocks_ack(self):
        cursors = {'will': 80}
        with mock.patch.object(self.w, 'api', return_value={'messages': [message(84)]}), \
             mock.patch.object(self.w, 'outbox_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.w.poll_union('key', {'will'}, cursors, {'will': None})
        self.assertEqual(cursors, {'will': 80})
        Path(self.w.OUTBOX_FILE).write_text('invalid json\n')
        with mock.patch.object(self.w, 'post_json') as post, self.assertRaises(ValueError):
            self.w.ack_through('key', 'will', 84)
        post.assert_not_called()

    def test_retry_keeps_channel_and_chat_without_republishing_successes(self):
        self.w.outbox_add(message(84), 'will-chat')
        self.w.outbox_add(message(85, 'house'), 'house-chat')
        with mock.patch.object(self.w, 'notify', side_effect=[(False, 'will-chat'), (True, 'house-chat')]) as notify:
            self.w.retry_outbox()
        self.assertEqual(notify.call_args_list[0].kwargs, {'session': 'will-chat'})
        self.assertEqual([(e['channel'], e['id'], e['attempts']) for e in self.w.outbox_load()], [('will', 84, 2)])

    def test_summary_partial_publish_only_removes_delivered_posts(self):
        self.w.outbox_add(message(84))
        self.w.outbox_add(message(85))
        with mock.patch.object(self.w, 'publish', side_effect=['will-chat', '']), \
             mock.patch.object(self.w, 'push', return_value=True):
            self.w.deliver_entries(self.w.outbox_load(), summary=True)
        self.assertEqual([e['id'] for e in self.w.outbox_load()], [85])
        self.assertEqual(self.w.outbox_load()[0]['chat'], '')

    def test_reset_moves_cursors_to_head_and_replay_zeroes_them(self):
        Path(self.w.LEGACY_CURSOR_FILE).write_text('84\n')
        self.w.write_cursor({'will': 90, 'retired': 10})
        def api(path, key, **kwargs):
            return {'latest_id': 100} if path == '/api/health' else {'channels': listing(will=100, house=95)}
        # --reset forgets the backlog: every live channel jumps to its own head, and a
        # channel that no longer exists keeps what it had rather than being zeroed.
        with mock.patch.object(self.w, 'read_ack_key', return_value='identity'), \
             mock.patch.object(self.w, 'api', side_effect=api), \
             mock.patch.object(self.w, 'post_json') as post, \
             mock.patch.object(sys, 'argv', ['watcher', '--reset']):
            self.assertEqual(self.w.main(), 0)
        self.assertEqual(json.loads(Path(self.w.CURSOR_FILE).read_text()),
                         {'will': 100, 'house': 95, 'retired': 10})
        self.assertEqual(Path(self.w.LEGACY_CURSOR_FILE).read_text(), '84\n')
        post.assert_not_called()
        # --replay is the other, dangerous direction and stays a separate flag.
        with mock.patch.object(self.w, 'read_ack_key', return_value='identity'), \
             mock.patch.object(self.w, 'api', side_effect=api), \
             mock.patch.object(self.w, 'post_json') as post, \
             mock.patch.object(sys, 'argv', ['watcher', '--replay']):
            self.assertEqual(self.w.main(), 0)
        self.assertEqual(json.loads(Path(self.w.CURSOR_FILE).read_text()),
                         {'will': 0, 'house': 0, 'retired': 0})
        post.assert_not_called()

    def test_main_prefers_identity_key_and_retries_only_on_slow_cycle(self):
        self.w.write_cursor({'will': 80, 'house': 70})
        def api(path, key, **kwargs):
            self.assertEqual(key, 'identity')
            return {'latest_id': 100} if path == '/api/health' else {'channels': listing(will=100, house=95)}
        with mock.patch.object(self.w, 'read_ack_key', return_value='identity'), \
             mock.patch.object(self.w, 'api', side_effect=api), \
             mock.patch.object(self.w, 'sync_acked'), mock.patch.object(self.w, 'poll_union', return_value=(100, False)), \
             mock.patch.object(self.w, 'ack_through') as ack, mock.patch.object(self.w, 'retry_outbox') as retry, \
             mock.patch.object(self.w.time, 'monotonic', side_effect=[0, 91, 91]), \
             mock.patch.object(sys, 'argv', ['watcher', '--once']):
            self.assertEqual(self.w.main(), 0)
            retry.assert_called_once()
            self.assertEqual(ack.call_args_list, [mock.call('identity', 'house', 70), mock.call('identity', 'will', 80)])
        self.assertTrue(Path(self.w.HEARTBEAT_FILE).exists())
        self.assertEqual(list(Path(self.tmp.name).glob('*heartbeat*')), [Path(self.w.HEARTBEAT_FILE)])


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='board-triage-test-')
        self.addCleanup(self.tmp.cleanup)
        self.g = load('board-triage-gate', self.tmp.name)
        Path(self.g.KEY).write_text('identity')

    def test_gate_pages_backwards_per_channel_and_repeats_exact_cache_when_down(self):
        requests = []
        def get(path, key):
            self.assertEqual(key, 'identity')
            requests.append(path)
            if path == '/api/health':
                return {'latest_id': 999}
            if path == '/api/channels':
                return {'channels': listing(will=100, house=95)}
            query = parse_qs(urlsplit(path).query)
            channel = query['channel'][0]
            before = int(query['before'][0])
            if channel == 'house':
                return {'messages': [message(998, channel, 'hamish-hermes')], 'has_more': False}
            if before == 1000:
                return {'messages': [message(995, channel, 'hamish-hermes')], 'has_more': True}
            self.assertEqual(before, 995)
            return {'messages': [message(84), message(90)], 'has_more': False}
        with mock.patch.object(self.g, 'get', side_effect=get), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.g.main(), 0)
        self.assertEqual(output.getvalue(), 'board_triage_house=0\nboard_triage_will=90\n')
        self.assertEqual(Path(self.g.CACHE).read_text(), output.getvalue())
        self.assertIn('before=1000', requests[2])
        with mock.patch.object(self.g, 'get', side_effect=OSError('offline')), contextlib.redirect_stdout(io.StringIO()) as down:
            self.assertEqual(self.g.main(), 0)
        self.assertEqual(down.getvalue(), output.getvalue())

    def test_gate_does_not_stop_at_three_pages_of_own_posts(self):
        pages = []
        def get(path, key):
            if path == '/api/health':
                return {'latest_id': 500}
            if path == '/api/channels':
                return {'channels': [{'channel': 'will'}]}
            before = int(parse_qs(urlsplit(path).query)['before'][0])
            pages.append(before)
            return {'messages': [message(before-1, author='will-hermes' if before == 498 else 'hamish-hermes')], 'has_more': True}
        with mock.patch.object(self.g, 'get', side_effect=get), contextlib.redirect_stdout(io.StringIO()) as output:
            self.g.main()
        self.assertEqual(output.getvalue(), 'board_triage_will=497\n')
        self.assertEqual(pages, [501, 500, 499, 498])

    def test_stale_other_side_is_omitted_unless_channel_has_open_ask(self):
        now = time.time()
        asks_open = set()
        def get(path, key):
            if path == '/api/health':
                return {'latest_id': 100}
            if path == '/api/channels':
                return {'channels': listing(will=100, house=90, recent=95, boundary=96)}
            query = parse_qs(urlsplit(path).query)
            channel = query['channel'][0]
            if path.startswith('/api/asks?'):
                self.assertEqual(query['state'], ['open'])
                return {'asks': [{'id': 1}] if channel in asks_open else []}
            # Even fresh own traffic must not keep a stale other-side id alive.
            age = 59 if channel == 'recent' else 60 if channel == 'boundary' else 61
            return {'messages': [dict(message(84, channel), epoch=now - age * 86400),
                                 message(100, channel, 'hamish-hermes')], 'has_more': False}
        with mock.patch.object(self.g, 'get', side_effect=get), \
             mock.patch.object(self.g.time, 'time', return_value=now):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.g.main()
            self.assertEqual(output.getvalue(), 'board_triage_boundary=84\nboard_triage_recent=84\n')
            asks_open.add('house')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.g.main()
            self.assertEqual(output.getvalue(), 'board_triage_boundary=84\nboard_triage_house=84\nboard_triage_recent=84\n')

    def test_all_stale_channels_leave_empty_cache_even_when_offline(self):
        def get(path, key):
            if path == '/api/health':
                return {'latest_id': 84}
            if path == '/api/channels':
                return {'channels': listing(will=84)}
            if path.startswith('/api/asks?'):
                return {'asks': []}
            return {'messages': [dict(message(84), epoch=time.time() - 61 * 86400)], 'has_more': False}
        with mock.patch.object(self.g, 'get', side_effect=get), contextlib.redirect_stdout(io.StringIO()) as output:
            self.g.main()
        self.assertEqual(output.getvalue(), '\n')
        self.assertEqual(Path(self.g.CACHE).read_text(), '\n')
        with mock.patch.object(self.g, 'get', side_effect=OSError('offline')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.g.main()
        self.assertEqual(output.getvalue(), '\n')


class LifecycleHTTPTest(unittest.TestCase):
    """Exercise installed scripts over real HTTP against disposable board state."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='board-lifecycle-http-')
        self.addCleanup(self.tmp.cleanup)
        self.w = load('agent-board-watch', self.tmp.name)
        self.g = load('board-triage-gate', self.tmp.name)
        spec = importlib.util.spec_from_file_location('lifecycle_board', Path(__file__).resolve().parents[1] / 'board.py')
        self.board = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.board)
        self.store = self.board.Board(os.path.join(self.tmp.name, 'board.db'))
        keys = self.board.Keys('test-legacy', self.tmp.name)
        self.owner = keys.add('hamish-hermes', channels=['will', 'house'])
        self.peer = keys.add('will-hermes', channels=['will', 'house'])
        Path(self.w.ACK_KEY_FILE).write_text(self.owner)
        handler = self.board.make_handler(self.store, keys, 'lifecycle-http-test')
        self.server = self.board.make_server(('127.0.0.1', 0), handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.w.BASE = self.g.URL = url

    def post(self, channel, **extra):
        return self.w.post_json('/api/messages', self.peer,
                                dict({'channel': channel, 'text': 'fixture message'}, **extra))

    def test_watcher_seeds_unknown_room_and_delivers_new_union_with_per_room_acks(self):
        old_will, old_house = self.post('will'), self.post('house')
        self.w.write_cursor({'will': 0})
        with mock.patch.object(self.w, 'publish', return_value='fixture-chat'), \
             mock.patch.object(self.w, 'push', return_value=True), \
             mock.patch.object(sys, 'argv', ['watcher', '--once']), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.w.main(), 0)
            new_house, new_will = self.post('house'), self.post('will')
            with mock.patch.object(self.w, 'api', wraps=self.w.api) as api:
                self.assertEqual(self.w.main(), 0)
            message_reads = [call.args[0] for call in api.call_args_list if call.args[0].startswith('/api/messages?')]
            # One union page plus one previous-epoch lookup for each active room.
            union_reads = [path for path in message_reads if 'channel' not in parse_qs(urlsplit(path).query)]
            self.assertEqual(len(union_reads), 1)
        inbox = [json.loads(line) for line in Path(self.w.INBOX_FILE).read_text().splitlines()]
        self.assertEqual([m['id'] for m in inbox], [old_will['id'], new_house['id'], new_will['id']])
        self.assertNotIn(old_house['id'], [m['id'] for m in inbox])
        self.assertEqual(self.w.read_map(self.w.CURSOR_FILE), {'will': new_will['id'], 'house': new_house['id']})
        delivery = self.w.api('/api/delivery', self.owner)
        mine = {r['channel']: r['acked_id'] for r in delivery['channels'] if r['identity'] == 'hamish-hermes'}
        self.assertEqual(mine, {'will': new_will['id'], 'house': new_house['id']})

    def test_gate_retains_stale_open_ask_until_closed_without_deleting_messages(self):
        stale = self.post('house', ask_to='hamish-hermes')
        with self.store._db() as db:
            db.execute('UPDATE messages SET epoch = ? WHERE id = ?', (time.time() - 61 * 86400, stale['id']))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.g.main()
        self.assertIn('board_triage_house=%d' % stale['id'], out.getvalue())
        ask = self.w.api('/api/asks?state=open&channel=house', self.owner)['asks'][0]
        self.w.post_json('/api/asks/%d/close' % ask['id'], self.owner, {'msg_id': stale['id']})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.g.main()
        self.assertNotIn('board_triage_house=', out.getvalue())
        self.assertEqual(len(self.w.api('/api/messages?channel=house', self.owner)['messages']), 1)
        closed = self.w.api('/api/asks?state=closed&channel=house', self.owner)['asks']
        self.assertEqual([a['id'] for a in closed], [ask['id']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
