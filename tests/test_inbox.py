import json
import threading
import urllib.request
import urllib.error
import unittest
from datetime import datetime,timedelta,timezone
from tempfile import TemporaryDirectory
from pathlib import Path
from plus_one.engine import Engine
from plus_one.worker import run_once,process_inbox
from plus_one.server import ServerConfig,make_server
from plus_one.gupshup import GupshupConfig

class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.path=Path(self.tmp.name)/'inbox.db'
        self.now=datetime(2026,10,31,12,tzinfo=timezone.utc); self.e=Engine(self.path,clock=lambda:self.now)
        self.uid='whatsapp:+15551234567'; self.n=0
    def tearDown(self):self.e.close();self.tmp.cleanup()
    def add(self,text='help',kind='text',mid=None,received=None):
        self.n+=1; mid=mid or str(self.n)
        p={'kind':kind,'text':text,'media':{'url':'mock-only'} if kind=='audio' else None,'received_at':(received or self.now).isoformat(),'user_id':self.uid,'message_id':mid}
        return self.e.enqueue_inbound('gupshup',mid,self.uid,p)
    def send(self,text):
        self.add(text); return run_once(self.e)
    def test_durable_replay_exactly_one_reply(self):
        self.add('expense USD 10 for tea with Ana',mid='a'); self.assertFalse(self.add('changed',mid='a'))
        self.e.close(); self.e=Engine(self.path,clock=lambda:self.now)
        run_once(self.e); self.assertEqual(len(self.e.snapshot(self.uid)['pending']),1)
        self.assertEqual(len(self.e.snapshot(self.uid)['reply_outbox']),1)
        self.assertEqual(run_once(self.e),[])
    def test_handle_commit_reply_crash_recovery(self):
        self.add('expense USD 10 for tea with Ana'); run_once(self.e)
        self.add('confirm'); row=self.e.claim_inbound()
        self.e.handle(self.uid,'confirm','gupshup:'+row['message_id'],inbox_id=row['id'])
        self.e.recover_inbox(); run_once(self.e)
        self.assertEqual(len(self.e.snapshot(self.uid)['expenses']),1)
        self.assertEqual(len(self.e.snapshot(self.uid)['reply_outbox']),2)
    def test_audio_dedup_and_paused_no_provider(self):
        class STT:
            calls=0
            def transcribe(self,media): self.calls+=1;return 'expense USD 10 for tea with Ana'
        stt=STT(); self.add(None,'audio',mid='voice')
        run_once(self.e,transcribers={'gupshup':stt}); self.add(None,'audio',mid='voice')
        run_once(self.e,transcribers={'gupshup':stt});self.assertEqual(stt.calls,1)
        self.e.handle(self.uid,'stop','stop'); self.add(None,'audio',mid='voice2')
        run_once(self.e,transcribers={'gupshup':stt});self.assertEqual(stt.calls,1)
        row=self.e.db.execute("SELECT * FROM inbox WHERE status='ignored'").fetchone()
        self.assertIsNone(row['user_id']);self.assertIsNone(row['payload'])
    def test_audio_crash_uncertain_never_reinvoked(self):
        self.add(None,'audio'); self.e.claim_inbound(); self.e.recover_inbox()
        self.assertEqual(run_once(self.e),[])
        row=self.e.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual(row['status'],'uncertain');self.assertIsNone(row['payload'])
    def test_transcript_commit_crash_safe_retry(self):
        self.add(None,'audio'); row=self.e.claim_inbound()
        self.assertTrue(self.e.save_inbound_transcript(row['id'],'expense USD 10 for tea with Ana'))
        self.e.recover_inbox();run_once(self.e)
        self.assertEqual(len(self.e.snapshot(self.uid)['pending']),1)
    def test_delete_during_stt_does_not_save_or_queue(self):
        engine=self.e;uid=self.uid
        class STT:
            def transcribe(self,media):
                engine.handle(uid,'delete my data','delete');engine.handle(uid,'confirm','confirm-delete')
                return 'expense USD 10 for tea with Ana'
        self.add(None,'audio');run_once(self.e,transcribers={'gupshup':STT()})
        state=self.e.snapshot(self.uid)
        self.assertIsNone(state['profile']);self.assertEqual(state['inbox'],[]);self.assertEqual(state['reply_outbox'],[])
    def test_delete_scrubs_queue_transient_ack_only(self):
        self.send('name Alice'); self.send('confirm');self.send('delete my data'); results=self.send('confirm')
        self.assertEqual(results[0]['kind'],'transient-deletion-ack')
        state=self.e.snapshot(self.uid);self.assertIsNone(state['profile']);self.assertEqual(state['inbox'],[]);self.assertEqual(state['reply_outbox'],[])
        for row in self.e.db.execute('SELECT * FROM inbox'):
            self.assertIsNone(row['user_id']);self.assertIsNone(row['message_id']);self.assertIsNone(row['payload'])
        self.add('help');run_once(self.e);self.assertIsNone(self.e.snapshot(self.uid)['profile'])
    def test_stop_start_does_not_revive_claimed_work(self):
        self.add('expense USD 10 for tea with Ana');row=self.e.claim_inbound()
        self.e.handle(self.uid,'stop','stop');self.e.handle(self.uid,'start','start')
        reply=self.e.handle(self.uid,'expense USD 10 for tea with Ana','gupshup:'+row['message_id'],inbox_id=row['id'])
        self.assertIn('cancelled',reply); self.assertEqual(self.e.snapshot(self.uid)['pending'],[])
    def test_stop_preserves_later_resume_and_delete_controls(self):
        self.add('stop');self.add('start');run_once(self.e)
        self.assertFalse(self.e.snapshot(self.uid)['paused'])
        self.add('stop');self.add('delete my data');self.add('confirm');run_once(self.e)
        self.assertIsNone(self.e.snapshot(self.uid)['profile'])
    def test_duplicate_cannot_extend_session(self):
        old=self.now-timedelta(hours=23); self.add('help',mid='same',received=old)
        self.assertTrue(self.e.session_is_open(self.uid));self.now+=timedelta(hours=2)
        self.add('help',mid='same',received=self.now);self.assertFalse(self.e.session_is_open(self.uid))
    def test_reply_claim_crash_uncertain(self):
        self.add('help');process_inbox(self.e);self.e.claim_reply();self.e.recover_inbox()
        self.assertEqual(self.e.snapshot(self.uid)['reply_outbox'][0]['status'],'uncertain');self.assertEqual(run_once(self.e),[])
    def test_transcriber_failure_honest_and_no_retry(self):
        class STT:
            calls=0
            def transcribe(self,media):self.calls+=1;raise RuntimeError('secret')
        stt=STT();self.add(None,'audio');run_once(self.e,transcribers={'gupshup':stt});run_once(self.e,transcribers={'gupshup':stt})
        self.assertEqual(stt.calls,1)
        reply=self.e.snapshot(self.uid)['reply_outbox'][0]['body'];self.assertIn("couldn't transcribe",reply);self.assertNotIn('secret',reply)
    def test_http_auth_ack_before_engine_work(self):
        config=GupshupConfig(api_key='fake-key',app_name='Demo',source='15557654321',allowed_recipients=frozenset([self.uid]),send_enabled=True,webhook_secret='s'*32)
        server=make_server(self.e,ServerConfig('a'*32,gupshup=config),port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        event={'app':'Demo','version':2,'type':'message','timestamp':int(datetime.now(timezone.utc).timestamp()*1000),'payload':{'id':'msg-1','source':'15551234567','type':'text','payload':{'text':'expense USD 10 for tea with Ana'},'sender':{'phone':'15551234567'}}}
        def post(secret):
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/webhooks/gupshup',json.dumps(event).encode(),{'Content-Type':'application/json','X-Gupshup-Webhook-Secret':secret})
            try:
                with urllib.request.urlopen(req) as r:return r.status,r.read()
            except urllib.error.HTTPError as r:return r.code,r.read()
        try:
            self.assertEqual(post('bad')[0],403);self.assertEqual(self.e.snapshot(self.uid)['inbox'],[])
            self.assertEqual(post('s'*32),(200,b''));self.assertEqual(post('s'*32),(200,b''))
            self.assertIsNone(self.e.snapshot(self.uid)['profile']);self.assertEqual(len(self.e.snapshot(self.uid)['inbox']),1)
            run_once(self.e);self.assertEqual(len(self.e.snapshot(self.uid)['pending']),1)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
