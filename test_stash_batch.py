import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import subprocess
from stash_batch import API, summarize

class BatchTests(unittest.TestCase):
    def test_native_transport_keeps_token_off_arguments(self):
        api=API('https://example.test/git',None,'fake-token')
        with patch('stash_batch.subprocess.run',return_value=subprocess.CompletedProcess([],0,'{"values":[],"isLastPage":true}\n200','')) as call:
            api.native_get('https://example.test/git/rest/api/latest/test')
            self.assertNotIn('fake-token',' '.join(call.call_args.args[0]))
            self.assertIn('fake-token',call.call_args.kwargs['input'])
            self.assertNotIn('--insecure',call.call_args.args[0])

    def test_corporate_cookie_export_without_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cookies.txt'
            path.write_text('example.test\tFALSE\t/\tTRUE\t0\ttest_session\tfake-value\n')
            api=API('https://example.test/git',str(path),'')
            processor=next(h for h in api.opener.handlers if hasattr(h,'cookiejar'))
            self.assertEqual(len(list(processor.cookiejar)),1)

    def setUp(self):
        self.pr=dict(id=7,title='Example',state='MERGED',author={'user':{'name':'someone'}},toRef={'repository':{'slug':'demo','project':{'key':'P'}}})

    def test_replies_deduplicated_and_timestamp_boundaries(self):
        reply=dict(id=2,author={'name':'ME'},createdDate=150,comments=[])
        root=dict(id=1,author={'name':'me'},createdDate=100,comments=[reply])
        excluded=dict(id=3,author={'name':'me'},createdDate=200,comments=[])
        data={'values':[{'comment':root},{'comment':root},{'comment':reply},{'comment':excluded}]}
        row,events=summarize(self.pr,data,'me',100,200,'https://example.test/git')
        self.assertEqual(row['comments'],2)
        self.assertEqual(row['replies'],1)
        self.assertEqual({e['record_id'] for e in events},{1,2})

    def test_review_statuses_separate_and_own_pr_marked(self):
        self.pr['author']['user']['name']='ME'
        data={'values':[dict(id=i,createdDate=150,user={'name':'me'},action=a) for i,a in enumerate(['APPROVED','REVIEWED','NEEDS_WORK','UNAPPROVED','MERGED','OPENED'])]}
        row,events=summarize(self.pr,data,'me',100,200,'https://example.test/git')
        self.assertEqual(len(events),4)
        self.assertTrue(row['own_PR'])
        self.assertEqual([row[k] for k in ['approvals','review_completed','needs_work','approval_withdrawn']],[1,1,1,1])

    def test_pagination_uses_server_next_start(self):
        api=API('https://example.test/git',None,'')
        requests=[]
        def get(path,query):
            requests.append(query['start'])
            return {'values':[], 'isLastPage':query['start']==17,'nextPageStart':17}
        api.get=get
        self.assertEqual(len(list(api.pages('test',{}))),2)
        self.assertEqual(requests,[0,17])

    def test_pagination_cycle_is_failure(self):
        api=API('https://example.test/git',None,'')
        api.get=lambda path,query:{'values':[],'isLastPage':False,'nextPageStart':0}
        with self.assertRaises(RuntimeError):list(api.pages('test',{}))

if __name__=='__main__':unittest.main()
