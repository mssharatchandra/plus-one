import concurrent.futures
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from plus_one.engine import Engine
from plus_one.worker import WorkerLock,run_once
from plus_one.transport import DeliveryError,DeliveryUncertain

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'test.db'
        self.now=datetime(2026,10,31,9,tzinfo=timezone.utc)
        self.e=Engine(self.path,clock=lambda:self.now); self.n=0
    def tearDown(self): self.e.close(); self.tmp.cleanup()
    def say(self,text,user='alice',message_id=None):
        self.n+=1; return self.e.handle(user,text,message_id or str(self.n))
    def zone(self,zone='UTC'):
        self.say('timezone '+zone); self.say('confirm')
    def task(self):
        self.zone(); self.say('remind me to finish report in 1 minute'); self.say('confirm')
    def test_timezone_requires_confirmation(self):
        self.say('timezone Asia/Kolkata')
        self.assertIsNone(self.e.snapshot('alice')['profile']['timezone'])
        self.say('confirm'); self.assertEqual(self.e.snapshot('alice')['profile']['timezone'],'Asia/Kolkata')
    def test_expense_confirmation_exact_balance_and_replay(self):
        text='expense INR 100 for dinner with Maya, Rahul'
        self.say(text,message_id='expense')
        self.assertEqual(self.e.snapshot('alice')['expenses'],[])
        first=self.say('confirm',message_id='confirm')
        self.assertEqual(first,self.say('confirm',message_id='confirm'))
        self.assertEqual(len(self.e.snapshot('alice')['expenses']),1)
        p=json.loads(self.e.snapshot('alice')['expenses'][0]['payload'])
        self.assertEqual(sum(s['amount_minor'] for s in p['shares']),10000)
        self.assertIn('Maya owes you INR 33.33',self.say('balances'))
    def test_two_way_net_balances_with_confirmed_default_currency(self):
        self.say('currency INR'); self.assertIsNone(self.e.snapshot('alice')['profile']['currency'])
        self.say('confirm'); self.say('I paid 600 for dinner split with Maya and Rahul'); self.say('confirm')
        preview=self.say('Maya paid INR 900 for lunch with me and Rahul')
        self.assertIn('You would owe Maya INR 300.00',preview)
        self.say('confirm'); balances=self.say('who owes me and who do I owe?')
        self.assertIn('You owe Maya INR 100.00',balances); self.assertIn('Rahul owes you INR 200.00',balances)
    def test_natural_reminder_and_learning_aliases(self):
        self.zone()
        self.assertIn('CONFIRM',self.say("I'm overwhelmed, can you remind me to send the report tomorrow at 9am?"))
        self.assertEqual(self.e.snapshot('alice')['tasks'],[])
        self.say('confirm')
        self.assertIn('Step 1',self.say('Help me learn storytelling for work in 2 minutes'))
    def test_missing_currency_never_inferred(self):
        self.assertIn('Which currency',self.say('I paid 600 for dinner split with Maya and Rahul'))
        self.assertEqual(self.e.snapshot('alice')['pending'],[])
    def test_casefolded_balances(self):
        self.say('expense USD 10 for coffee with Maya'); self.say('confirm')
        self.say('maya paid USD 10 for coffee with me'); self.say('confirm')
        self.assertIn('Settled with Maya: USD 0.00',self.say('balances'))
    def test_cancel_preview(self):
        self.say('expense USD 10 for tea with Ana'); self.say('cancel'); self.say('confirm')
        self.assertEqual(self.e.snapshot('alice')['expenses'],[])
    def test_invalid_expense_no_mutation(self):
        self.say('expense 10 for tea with Ana'); self.say('confirm')
        self.assertEqual(self.e.snapshot('alice')['expenses'],[])
    def test_expired_proposal(self):
        self.say('expense USD 10 for tea with Ana'); self.now+=timedelta(minutes=15)
        self.assertIn('expired',self.say('confirm')); self.assertEqual(self.e.snapshot('alice')['expenses'],[])
    def test_new_proposal_replaces_previous(self):
        self.say('name Alex'); result=self.say('timezone UTC'); self.assertIn('replaced',result)
        self.say('confirm'); self.assertIsNone(self.e.snapshot('alice')['profile']['name'])
    def test_missing_timezone_and_invalid_dates(self):
        self.assertIn('timezone',self.say('remind me to finish report tomorrow at 9am'))
        self.zone('America/New_York')
        self.assertIn('ambiguous',self.say('remind me to finish report on 2026-11-01 at 1:30am').lower())
        self.assertEqual(self.e.snapshot('alice')['tasks'],[])
    def test_task_confirm_and_due_restart(self):
        self.task(); self.e.close(); self.e=Engine(self.path,clock=lambda:self.now)
        self.assertEqual(self.e.process_due(),[])
        self.now+=timedelta(minutes=1)
        self.assertEqual(len(self.e.process_due()),1); self.assertEqual(self.e.process_due(),[])
        self.assertEqual(len(self.e.snapshot('alice')['outbox']),1)
    def test_task_past_at_confirmation(self):
        self.zone(); self.say('remind me to finish report in 1 minute'); self.now+=timedelta(minutes=2)
        self.assertIn('already passed',self.say('confirm')); self.assertEqual(self.e.snapshot('alice')['tasks'],[])
    def test_quiet_hours_deferral(self):
        self.now=datetime(2026,10,31,21,59,tzinfo=timezone.utc); self.task()
        self.now+=timedelta(minutes=1); self.assertEqual(self.e.process_due(),[])
        self.now=datetime(2026,11,1,8,tzinfo=timezone.utc); self.assertEqual(len(self.e.process_due()),1)
    def test_quiet_off_confirmation(self):
        self.say('quiet off'); self.assertEqual(self.e.snapshot('alice')['profile']['quiet_start'],'22:00')
        self.say('confirm'); self.assertIsNone(self.e.snapshot('alice')['profile']['quiet_start'])
    def test_snooze_confirm_revision_and_done(self):
        self.task(); self.now+=timedelta(minutes=1); self.e.process_due()
        self.say('snooze 1 10m'); self.assertEqual(self.e.snapshot('alice')['tasks'][0]['revision'],1)
        self.say('confirm'); state=self.e.snapshot('alice')
        self.assertEqual(state['tasks'][0]['revision'],2); self.assertEqual(state['outbox'][0]['status'],'cancelled')
        self.now+=timedelta(minutes=10); self.assertEqual(len(self.e.process_due()),1)
        self.say('done 1'); self.assertTrue(all(x['status']=='cancelled' for x in self.e.snapshot('alice')['outbox']))
    def test_snooze_after_done_rejected(self):
        self.task(); self.say('snooze 1 10m'); self.say('done 1')
        self.assertIn('changed',self.say('confirm')); self.assertEqual(self.e.snapshot('alice')['tasks'][0]['state'],'done')
    def test_user_isolation(self):
        self.task(); self.assertIn('could not find',self.say('done 1',user='bob'))
        self.assertEqual(self.e.snapshot('alice')['tasks'][0]['state'],'scheduled')
        self.assertEqual(self.e.snapshot('bob')['tasks'],[])
    def test_invalid_learning_goal_does_not_persist(self):
        self.say('learn storytelling for work\tpresentations in 3 minutes')
        self.assertEqual(self.e.snapshot('alice')['journeys'],[])
    def test_optional_interpretation_proposes_never_confirms(self):
        class Interpreter:
            calls=0
            def interpret(inner,text,profile,now):
                inner.calls+=1
                self.assertNotIn('user_id',profile)
                return {'canonical':'expense USD 10 for tea with Ana'}
        adapter=Interpreter();self.e.interpreter=adapter
        self.say('Ten dollars on tea, me and Ana, I paid it',message_id='natural')
        self.assertEqual(self.e.snapshot('alice')['expenses'],[])
        self.assertEqual(len(self.e.snapshot('alice')['pending']),1)
        self.say('Ten dollars on tea, me and Ana, I paid it',message_id='natural')
        self.assertEqual(adapter.calls,1)
        self.say('confirm');self.assertEqual(adapter.calls,1)
        self.assertEqual(len(self.e.snapshot('alice')['expenses']),1)
    def test_negated_reminder_not_normalized(self):
        self.zone();self.say('do not remind me to pay tomorrow at 9am')
        self.assertEqual(self.e.snapshot('alice')['pending'],[])
    def test_learning_continuity_restart(self):
        self.say('learn storytelling for work presentations in 3 minutes')
        self.assertIn('Before we move on',self.say('next'))
        self.say('reflection I need a concrete moment')
        self.e.close(); self.e=Engine(self.path,clock=lambda:self.now)
        result=self.say('next'); self.assertIn('I need a concrete moment',result); self.assertIn('Step 2',result)
    def test_learning_replace_requires_confirmation(self):
        self.say('learn storytelling'); self.say('reflection a moment')
        self.say('learn storytelling for a wedding in 2 minutes')
        self.assertEqual(self.e.snapshot('alice')['journeys'][0]['goal'],'everyday conversations')
        self.say('confirm'); self.assertEqual(self.e.snapshot('alice')['reflections'],[])
        self.assertEqual(self.e.snapshot('alice')['journeys'][0]['goal'],'a wedding')
    def test_stop_cancels_never_revives(self):
        self.task(); self.now+=timedelta(minutes=1); self.e.process_due(); self.say('stop')
        self.assertEqual(self.e.snapshot('alice')['tasks'][0]['state'],'cancelled')
        self.assertEqual(self.e.snapshot('alice')['outbox'][0]['status'],'cancelled')
        self.say('confirm'); self.say('start'); self.assertEqual(self.e.process_due(),[])
    def test_delete_scrubs_all_and_cannot_resurrect_without_start(self):
        self.task(); self.say('learn storytelling'); self.say('reflection personal thought')
        self.say('expense USD 10 for tea with Ana'); self.say('confirm',message_id='old-confirm')
        self.say('delete my data'); self.say('confirm')
        for msg in ('confirm','yes','delete my data','help','reflection another'):
            self.say(msg); self.assertIsNone(self.e.snapshot('alice')['profile'])
        self.say('confirm',message_id='old-confirm')
        state=self.e.snapshot('alice')
        for field in ('tasks','expenses','reflections','journeys','outbox','pending'): self.assertEqual(state[field],[])
        rows=self.e.db.execute('SELECT response FROM receipts').fetchall(); self.assertTrue(all(r[0] is None for r in rows))
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)
        self.say('start'); self.assertIsNotNone(self.e.snapshot('alice')['profile'])
    def test_claim_restart_uncertain_not_resent(self):
        self.task(); self.now+=timedelta(minutes=1); self.e.process_due()
        self.assertIsNotNone(self.e.claim_outbox()); self.assertIsNone(self.e.claim_outbox())
        self.e.close(); self.e=Engine(self.path,clock=lambda:self.now)
        self.assertEqual(self.e.recover_inflight(),1); self.assertEqual(run_once(self.e),[])
        self.assertEqual(self.e.snapshot('alice')['outbox'][0]['status'],'uncertain')
    def test_worker_simulation_and_duplicate_tick(self):
        self.task(); self.now+=timedelta(minutes=1)
        self.assertEqual(run_once(self.e)[0]['status'],'simulated'); self.assertEqual(run_once(self.e),[])
    def test_worker_unknown_outcome_no_retry(self):
        self.task(); self.now+=timedelta(minutes=1)
        class Transport:
            calls=0
            def send(inner,*a,**k): inner.calls+=1; raise DeliveryUncertain('sensitive')
        t=Transport(); self.assertEqual(run_once(self.e,t)[0]['status'],'uncertain')
        self.assertEqual(run_once(self.e,t),[]); self.assertEqual(t.calls,1)
        self.assertNotIn('sensitive',self.e.snapshot('alice')['outbox'][0]['error'])
    def test_worker_lock(self):
        with WorkerLock(self.path):
            with self.assertRaises(RuntimeError):
                with WorkerLock(self.path): pass
    def test_multi_connection_concurrent_confirm_and_due(self):
        self.zone(); self.say('remind me to finish report in 1 minute')
        engines=[Engine(self.path,clock=lambda:self.now) for _ in range(6)]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda e:e.handle('alice','confirm','same-confirm'),engines))
            self.assertEqual(len(self.e.snapshot('alice')['tasks']),1)
            self.now+=timedelta(minutes=1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results=list(pool.map(lambda e:e.process_due(),engines))
            self.assertEqual(sum(len(r) for r in results),1)
        finally:
            for e in engines:e.close()
    def test_large_task_identifiers_are_safe(self):
        for command in ('done', 'cancel', 'snooze'):
            suffix=' 10m' if command=='snooze' else ''
            result=self.say(command+' 99999999999999999999999999999'+suffix)
            self.assertIn('could not find',result)
    def test_cancelled_claim_not_active(self):
        self.task(); self.now+=timedelta(minutes=1); self.e.process_due()
        row=self.e.claim_outbox(); self.assertTrue(self.e.delivery_is_active(row['id']))
        self.say('stop'); self.assertFalse(self.e.delivery_is_active(row['id']))
    def test_invalid_inputs(self):
        for args in [('', 'hi','1'),('alice','','1'),('alice','hi',''),('alice','x'*4001,'1')]:
            with self.assertRaises(ValueError): self.e.handle(*args)

if __name__=='__main__': unittest.main()
