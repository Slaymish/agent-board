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
            'text': 'A whole post.\n\n' + 'body ' * 300, 'files': []}


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
        cursors = self.w.read_cursor(['will', 'house'], 100)
        self.assertEqual(cursors, {'will': 84, 'house': 84})
        self.assertEqual(json.loads(Path(self.w.CURSOR_FILE).read_text()), cursors)
        self.assertEqual(old.read_text(), '84\n')
        self.w.write_cursor({'will': 90, 'house': 85})
        self.assertEqual(self.w.read_cursor(['will', 'house', 'new'], 110),
                         {'will': 90, 'house': 85, 'new': 0})
        self.assertEqual(old.read_text(), '84\n')

    def test_first_start_seeds_head_and_bad_json_does_not_rewind(self):
        self.assertEqual(self.w.read_cursor(['will', 'house'], 100), {'will': 100, 'house': 100})
        Path(self.w.CURSOR_FILE).write_text('bad json')
        with self.assertRaises(ValueError):
            self.w.read_cursor(['will'], 200)

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
        self.assertEqual(self.w.read_acked(), {'will': 80})

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
            self.w.poll_channel('key', 'will', cursors)
        self.assertEqual(parse_qs(urlsplit(api.call_args.args[0]).query)['channel'], ['will'])
        self.assertEqual(cursors, {'will': 84, 'house': 70})
        self.assertEqual(self.w.outbox_load()[0]['chat'], 'will-chat')
        with mock.patch.object(self.w, 'api', return_value={'messages': [message(90, 'house')]}), \
             mock.patch.object(self.w, 'notify', return_value=(True, 'house-chat')):
            self.w.poll_channel('key', 'house', cursors)
        self.assertEqual(cursors, {'will': 84, 'house': 90})
        self.assertEqual([(e['channel'], e['id']) for e in self.w.outbox_load()], [('will', 84)])

    def test_outbox_write_failure_stops_cursor_and_corrupt_outbox_blocks_ack(self):
        cursors = {'will': 80}
        with mock.patch.object(self.w, 'api', return_value={'messages': [message(84)]}), \
             mock.patch.object(self.w, 'outbox_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.w.poll_channel('key', 'will', cursors)
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

    def test_reset_updates_all_known_cursors_and_keeps_legacy_read_only(self):
        Path(self.w.LEGACY_CURSOR_FILE).write_text('84\n')
        self.w.write_cursor({'will': 90, 'retired': 10})
        def api(path, key, **kwargs):
            return {'latest_id': 100} if path == '/api/health' else {'channels': [{'channel': 'will'}, {'channel': 'house'}]}
        with mock.patch.object(self.w, 'read_ack_key', return_value='identity'), \
             mock.patch.object(self.w, 'api', side_effect=api), \
             mock.patch.object(self.w, 'post_json') as post, \
             mock.patch.object(sys, 'argv', ['watcher', '--reset']):
            self.assertEqual(self.w.main(), 0)
        self.assertEqual(json.loads(Path(self.w.CURSOR_FILE).read_text()), {'will': 100, 'house': 100, 'retired': 100})
        self.assertEqual(Path(self.w.LEGACY_CURSOR_FILE).read_text(), '84\n')
        post.assert_not_called()

    def test_main_prefers_identity_key_and_retries_only_on_slow_cycle(self):
        self.w.write_cursor({'will': 80, 'house': 70})
        def api(path, key, **kwargs):
            self.assertEqual(key, 'identity')
            return {'latest_id': 100} if path == '/api/health' else {'channels': [{'channel': 'will'}, {'channel': 'house'}]}
        with mock.patch.object(self.w, 'read_ack_key', return_value='identity'), \
             mock.patch.object(self.w, 'api', side_effect=api), \
             mock.patch.object(self.w, 'sync_acked'), mock.patch.object(self.w, 'poll_channel', return_value=False), \
             mock.patch.object(self.w, 'ack_through') as ack, mock.patch.object(self.w, 'retry_outbox') as retry, \
             mock.patch.object(self.w.time, 'monotonic', side_effect=[0, 91, 91]), \
             mock.patch.object(sys, 'argv', ['watcher', '--once']):
            self.assertEqual(self.w.main(), 0)
            retry.assert_called_once()
            self.assertEqual(ack.call_args_list, [mock.call('identity', 'will', 80), mock.call('identity', 'house', 70)])
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
                return {'channels': [{'channel': 'will'}, {'channel': 'house'}]}
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
