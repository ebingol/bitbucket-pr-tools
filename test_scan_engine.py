import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
import scan_engine as e
import pr_tools as p

USER={'id':1,'name':'alice'}
REPOS=[{'project':{'key':'P'},'slug':'r'}]
DATE=int(datetime.fromisoformat('2026-10-01T12:00:00+03:00').timestamp()*1000)
class Fake:
    def __init__(self, count=8, failure=None):
        self.count=count;self.failure=failure;self.active=0;self.peak=0;self.calls=[];self.lock=threading.Lock()
    def worker_copy(self): return self
    def pages(self,path,**kwargs):
        with self.lock:self.calls.append(path)
        if path.endswith('/pull-requests'):
            return iter([{'id':i,'state':'OPEN','createdDate':DATE,'title':str(i)} for i in range(1,self.count+1)])
        i=int(path.split('/')[-2])
        with self.lock:
            self.active+=1;self.peak=max(self.peak,self.active)
        try:
            time.sleep(.015)
            if i==self.failure:raise p.ScanError('temporary test failure')
            return iter([{'id':i,'action':'COMMENTED','commentAction':'ADDED','comment':{'id':i,'author':USER,'createdDate':DATE}}])
        finally:
            with self.lock:self.active-=1

class EngineTests(unittest.TestCase):
    def test_parallel_bounded_and_live_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            client=Fake();errors=[]
            records=e.scan(client,REPOS,USER,'stats',errors,out=folder,workers=3)
            self.assertEqual(len(records),8)
            self.assertGreater(client.peak,1)
            self.assertLessEqual(client.peak,3)
            progress=json.loads((Path(folder)/'progress.json').read_text())
            self.assertEqual(progress['summary'][-1]['comments'],8)
            self.assertEqual(progress['state'],'complete')
            self.assertFalse(errors)

    def test_resume_reuses_successes_and_retries_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            first=Fake(count=4,failure=2);errors=[]
            records=e.scan(first,REPOS,USER,'stats',errors,out=folder,workers=2)
            self.assertEqual(len(records),3)
            second=Fake(count=4);errors=[]
            records=e.scan(second,REPOS,USER,'stats',errors,out=folder,workers=2)
            self.assertEqual(len(records),4)
            self.assertEqual(second.calls,['projects/P/repos/r/pull-requests/2/activities'])
            self.assertEqual(p.summarize(records,['2026-10'])[-1]['comments'],4)

    def test_fresh_folder_reads_everything(self):
        with tempfile.TemporaryDirectory() as folder:
            a=Path(folder)/'a';b=Path(folder)/'b';a.mkdir();b.mkdir()
            first=Fake(count=2);e.scan(first,REPOS,USER,'stats',[],out=a,workers=2)
            second=Fake(count=2);e.scan(second,REPOS,USER,'stats',[],out=b,workers=2)
            self.assertEqual(len(second.calls),3)
            self.assertEqual(len(list(a.glob('checkpoint/pr-*.json'))),2)

    def test_known_pr_validation_does_not_accept_zero(self):
        client=Fake(count=1)
        with self.assertRaises(p.ScanError):p.verification(client,{'id':999},'P/r#1',1)
        self.assertEqual(p.verification(client,USER,'P/r#1',1)['status'],'passed')

    def test_checkpoint_preserves_timezone_and_no_double_count(self):
        record={'key':'P/r#1','pr':{'id':1},'comments':{'1':p.timestamp(DATE)},'reviews':{}}
        restored=e.decoded(e.encoded(record))
        self.assertEqual(restored,record)

    def test_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            with e.run_lock(folder):
                with self.assertRaises(RuntimeError):
                    with e.run_lock(folder):pass
            with e.run_lock(folder):pass

class InterruptedTests(unittest.TestCase):
    def test_interrupt_keeps_already_saved_results(self):
        class Interrupting(Fake):
            def pages(self,path,**kwargs):
                if path.endswith('/2/activities'): raise KeyboardInterrupt()
                return super().pages(path,**kwargs)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(KeyboardInterrupt):
                e.scan(Interrupting(count=2),REPOS,USER,'stats',[],out=folder,workers=1)
            self.assertEqual(len(list(Path(folder).glob('checkpoint/pr-*.json'))),1)
            self.assertEqual(json.loads((Path(folder)/'progress.json').read_text())['state'],'interrupted')
            client=Fake(count=2)
            records=e.scan(client,REPOS,USER,'stats',[],out=folder,workers=1)
            self.assertEqual(len(records),2)
            self.assertEqual(client.calls,['projects/P/repos/r/pull-requests/2/activities'])

if __name__=='__main__':unittest.main()
