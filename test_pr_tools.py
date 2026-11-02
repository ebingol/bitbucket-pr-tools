import unittest
import tempfile
import json
from pathlib import Path
from contextlib import redirect_stdout
import io
from datetime import datetime
from unittest.mock import patch
import pr_tools as p

USER = {'id': 1, 'name': 'alice', 'emailAddress': 'alice@example.org'}
OTHER = {'id': 2, 'name': 'bob'}

def ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)

def comment(cid, date='2026-09-30T23:59:00+03:00', author=USER, replies=None):
    return {'id': cid, 'author': author, 'createdDate': ms(date), 'comments': replies or []}

def activity(comment_obj, action='ADDED'):
    return {'id': 100, 'action': 'COMMENTED', 'commentAction': action, 'comment': comment_obj}

class Tests(unittest.TestCase):
    def test_reply_and_edit_are_counted_once(self):
        reply = comment(2, '2026-10-01T00:01:00+03:00')
        root = comment(1, author=OTHER, replies=[reply])
        comments, reviews, history = p.extract_events([activity(root), activity(reply), activity(reply, 'UPDATED')], USER)
        self.assertEqual(list(comments), ['2'])
        self.assertEqual(comments['2'].strftime('%Y-%m'), '2026-10')
        self.assertTrue(history)

    def test_deleted_comment_keeps_candidate_history(self):
        cs, rs, history = p.extract_events([activity(comment(1)), activity(comment(1), 'DELETED')], USER)
        self.assertEqual(cs, {})
        self.assertTrue(history)
        record = {'comments': cs, 'has_comment_history': history, 'pr': {'state': 'OPEN', 'author': {'user': OTHER}}}
        self.assertFalse(p.ready(record, USER))

    def test_month_boundary_and_unique_total(self):
        records = [{'key': 'P/R#1', 'comments': {'1': p.timestamp(ms('2026-09-30T21:30:00+00:00'))},
                    'reviews': {'a': p.timestamp(ms('2026-09-01T10:00:00+03:00'))}},
                   {'key': 'P/R#2', 'comments': {'2': p.timestamp(ms('2026-09-30T20:30:00+00:00'))},
                    'reviews': {}}]
        rows = p.summarize(records, ['2026-09', '2026-10'])
        self.assertEqual(rows[0]['commented_prs'], 1)
        self.assertEqual(rows[0]['review_only_prs'], 1)
        self.assertEqual(rows[1]['commented_prs'], 1)
        self.assertEqual(rows[2]['all_reviewed_prs'], 2)
        self.assertEqual(rows[2]['review_only_prs'], 0)
        self.assertEqual(rows[2]['comments'], 2)

    def test_formal_review_actions_ignore_assignment(self):
        actions = ['APPROVED', 'REVIEWED', 'UNAPPROVED', 'OPENED', 'UPDATED', 'COMMENTED']
        events = [{'id': i, 'action': action, 'createdDate': ms('2026-10-01T10:00:00+03:00'), 'user': USER}
                  for i, action in enumerate(actions)]
        self.assertEqual(len(p.extract_events(events, USER)[1]), 3)

    def test_identity_id_overrides_email_collision(self):
        self.assertFalse(p.same_user({'id': 2, 'emailAddress': USER['emailAddress']}, USER))

    def test_missing_timestamp_fails_instead_of_zero(self):
        bad = comment(1)
        del bad['createdDate']
        with self.assertRaises(p.ScanError):
            p.extract_events([activity(bad)], USER)

    def test_pagination_uses_server_cursor(self):
        client = p.Client('https://example.org/git', 'dummy')
        with patch.object(client, 'get', side_effect=[
            {'values': [1], 'isLastPage': False, 'nextPageStart': 17},
            {'values': [2], 'isLastPage': True}]) as get:
            self.assertEqual(list(client.pages('repos')), [1, 2])
            self.assertEqual(get.call_args_list[1].args[1]['start'], 17)

    def test_pagination_bad_cursor_is_error(self):
        client = p.Client('https://example.org/git', 'dummy')
        with patch.object(client, 'get', return_value={'values': [], 'isLastPage': False, 'nextPageStart': 0}):
            with self.assertRaises(p.ScanError):
                list(client.pages('repos'))

    def test_scan_failure_does_not_make_candidate(self):
        client = p.Client('https://example.org/git', 'dummy')
        repo = {'slug': 'r', 'project': {'key': 'P'}}
        def pages(path, **kwargs):
            if path.endswith('/activities'):
                raise p.ScanError('permission denied')
            return iter([{'id': 1, 'state': 'OPEN', 'author': {'user': OTHER}}])
        errors = []
        with patch.object(client, 'pages', side_effect=pages):
            self.assertEqual(p.scan(client, [repo], USER, 'ready', errors), [])
        self.assertEqual(len(errors), 1)

    def test_author_draft_and_closed_excluded(self):
        for pr in [{'state': 'MERGED'}, {'state': 'OPEN', 'draft': True},
                   {'state': 'OPEN', 'author': {'user': USER}}]:
            self.assertFalse(p.ready({'pr': pr, 'comments': {}}, USER))

    def test_no_redirect_with_token(self):
        with self.assertRaises(p.ScanError):
            p.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example')

class EndToEndTests(unittest.TestCase):
    def test_ready_main_sorts_globally_and_exports_no_token(self):
        repo = {'slug': 'r', 'project': {'key': 'P'}}
        prs = [{'id': n, 'title': 'PR ' + str(n), 'state': 'OPEN',
                'createdDate': ms(f'2026-10-{n:02d}T10:00:00+03:00'),
                'author': {'user': OTHER}} for n in range(1, 6)]
        client = p.Client('https://example.org/git', 'secret-test-token')
        client.username = 'alice'
        def pages(path, **kwargs):
            if path == 'repos':
                return iter([repo])
            if path.endswith('/activities'):
                return iter([activity(comment(1))] if '/5/' in path else [])
            return iter(prs)
        with tempfile.TemporaryDirectory() as folder, patch('builtins.input', return_value='alice'), \
             patch.object(p.getpass, 'getpass', return_value='secret-test-token'), \
             patch.object(p, 'Client', return_value=client), \
             patch.object(client, 'get', return_value=USER), \
             patch.object(client, 'pages', side_effect=pages), redirect_stdout(io.StringIO()):
            self.assertEqual(p.main(['ready', '--base-url', 'https://bitbucket.example.org', '--output', folder]), 0)
            report_path = next(Path(folder).glob('*/report.json'))
            text = report_path.read_text()
            report = json.loads(text)
            self.assertEqual([r['pr'] for r in report['candidates']], ['P/r#4', 'P/r#3', 'P/r#2'])
            self.assertNotIn('secret-test-token', text)
            self.assertTrue(report['complete'])

class NativeTransportTests(unittest.TestCase):
    def test_token_stays_off_process_args_and_no_tls_bypass(self):
        import subprocess
        c = p.Client('https://example.org/git', 'secret-test-token')
        with patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'{"values": [], "isLastPage": true}\\n200'.replace('\\n','\n'),'')) as run:
            self.assertTrue(c.native_get('https://example.org/git/rest/api/1.0/repos','repos')['isLastPage'])
            args=run.call_args.args[0]
            self.assertNotIn('secret-test-token',' '.join(args))
            self.assertNotIn('--insecure',args)
            self.assertIn('--disable',args)
            self.assertIn('secret-test-token',run.call_args.kwargs['input'])

class TokenIdentityTests(unittest.TestCase):
    def test_synthetic_pat_principal_uses_email_search(self):
        client = p.Client('https://example.org/git', 'dummy')
        client.username = 'access-token-user%2F2%2F27318'
        with patch.object(client, 'get') as get, patch.object(client, 'pages', return_value=iter([USER])) as pages:
            self.assertEqual(p.resolve_user(client, 'alice@example.org'), USER)
            get.assert_not_called()
            pages.assert_called_once_with('users', filter='alice@example.org')

if __name__ == '__main__':
    unittest.main()
