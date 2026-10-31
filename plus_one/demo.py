"""Reproducible demo: artificial clock and local simulation, not live WhatsApp."""
from datetime import datetime,timedelta,timezone
from tempfile import TemporaryDirectory
from pathlib import Path
from .engine import Engine
from .worker import run_once

def run_demo():
    print('PLUS ONE | deterministic three-flow PoC | NO live network, voice recognition, or LLM calls')
    with TemporaryDirectory() as tmp:
        now=[datetime(2026,10,31,9,0,tzinfo=timezone.utc)]
        path=Path(tmp)/'demo.sqlite3'
        e=Engine(path,clock=lambda:now[0]); n=0
        def say(text):
            nonlocal n
            n+=1; print('\nYou:',text); print('Plus One:',e.handle('demo',text,f'demo-{n}'))
        say('name Alex'); say('confirm'); say('timezone Asia/Kolkata'); say('confirm'); say('currency INR'); say('confirm')
        print('\n=== 1. Expense: proposed exact shares, then private balances ===')
        say('expense INR 100 for dinner with Maya, Rahul')
        assert len(e.snapshot('demo')['expenses'])==0
        say('confirm'); say('balances')
        assert len(e.snapshot('demo')['expenses'])==1
        say('Maya paid 300 for lunch split with me and Rahul'); say('confirm'); say('who owes me and who do I owe?')
        print('\n=== 2. Task: explicit local-time confirmation and restart-safe delivery ===')
        say('remind me to send the project update in 1 minute'); say('confirm')
        e.close(); e=Engine(path,clock=lambda:now[0]); now[0]+=timedelta(minutes=1)
        print('After database restart + clock advance:',run_once(e))
        print('Repeated scheduler tick (must not duplicate):',run_once(e))
        assert len(e.snapshot('demo')['outbox'])==1
        say('snooze 1 10m'); say('confirm'); say('done 1'); say('tasks')
        print('\n=== 3. Curiosity: tailored small step, reflection, and continuity ===')
        say('learn storytelling for work presentations in 5 minutes')
        say('reflection One specific moment feels easier to follow than a broad summary.')
        e.close(); e=Engine(path,clock=lambda:now[0]); say('next'); say('memory')
        print('\n=== Controls: stop and confirmed deletion ===')
        say('stop'); say('delete my data'); say('confirm')
        state=e.snapshot('demo'); assert state['profile'] is None and not state['tasks'] and not state['reflections']
        e.close()
    print('\nPASS: all three offline flows demonstrated. Live WhatsApp/STT/LLM remain pending credentials and explicit authorized setup.')
