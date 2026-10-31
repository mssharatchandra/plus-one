"""Transactional, deterministic Plus One domain engine. No external service calls."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .flows import parse_expense, format_expense, format_money, learning_step, CURRENCY_PRECISION
from .timeparse import parse_reminder, parse_snooze_duration, quiet_hours_next

UTC = timezone.utc

def utcnow():
    return datetime.now(UTC)

def iso(dt):
    if dt.tzinfo is None:
        raise ValueError('An aware date/time is required.')
    return dt.astimezone(UTC).isoformat()

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 user_id TEXT PRIMARY KEY, name TEXT, timezone TEXT, currency TEXT, tone TEXT NOT NULL DEFAULT 'warm',
 quiet_start TEXT DEFAULT '22:00', quiet_end TEXT DEFAULT '08:00', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS suppressed (user_hash TEXT PRIMARY KEY, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
 key TEXT PRIMARY KEY, user_hash TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending (
 user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
 kind TEXT NOT NULL, payload TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS expenses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 title TEXT NOT NULL, due_at TEXT NOT NULL, timezone TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'scheduled',
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journeys (
 user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
 topic TEXT NOT NULL, goal TEXT NOT NULL, minutes INTEGER NOT NULL, step INTEGER NOT NULL DEFAULT 0,
 awaiting_reflection INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS reflections (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 step INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, revision INTEGER NOT NULL,
 body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', provider_id TEXT, error TEXT,
 created_at TEXT NOT NULL, UNIQUE(task_id, revision));
CREATE INDEX IF NOT EXISTS tasks_due ON tasks(state, due_at);
CREATE TABLE IF NOT EXISTS inbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
 provider TEXT NOT NULL, user_hash TEXT NOT NULL, user_id TEXT, message_id TEXT,
 payload TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
 received_at TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS reply_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, inbox_id INTEGER NOT NULL UNIQUE REFERENCES inbox(id),
 user_id TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 provider_id TEXT, error TEXT, created_at TEXT NOT NULL);

'''

class Engine:
    def __init__(self, db_path='plus_one.sqlite3', clock=None, interpreter=None):
        self.db_path = str(db_path)
        self.clock = clock or utcnow
        self.interpreter = interpreter
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if self.db_path != ':memory:':
            os.chmod(self.db_path, 0o600)
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        if 'currency' not in {r[1] for r in self.db.execute('PRAGMA table_info(users)')}:
            self.db.execute('ALTER TABLE users ADD COLUMN currency TEXT')

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def _now(self, value=None):
        value = value or self.clock()
        iso(value)
        return value.astimezone(UTC)

    def handle(self, user_id, text, message_id, now=None, inbox_id=None):
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 200:
            raise ValueError('A valid user identifier is required.')
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 200:
            raise ValueError('A unique message identifier is required.')
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('Please send between 1 and 4000 characters.')
        text = text.strip()
        now = self._now(now)
        key = digest(user_id + '\0' + message_id)
        user_hash = digest(user_id)
        interpreted = False
        normalized = self._normalize(text)
        # Optional model interpretation runs outside the write transaction. It cannot
        # execute commands and never sees control operations or duplicate messages.
        with self._lock:
            if inbox_id is not None and not self.db.execute("SELECT 1 FROM inbox WHERE id=? AND status='processing'",(inbox_id,)).fetchone():
                return 'That queued request was cancelled. Nothing was saved.'
            seen = self.db.execute('SELECT response FROM receipts WHERE key=?',(key,)).fetchone()
            paused = self.db.execute('SELECT 1 FROM suppressed WHERE user_hash=?',(user_hash,)).fetchone()
        if seen:
            return seen['response'] or 'That message was already handled. Send a new message to continue.'
        if self.interpreter is not None and not paused and not self._recognized(normalized):
            saved_profile = self.snapshot(user_id)['profile'] or {}
            profile = {k:saved_profile.get(k) for k in ('name','timezone','currency','tone')}
            try:
                result = self.interpreter.interpret(text,profile,now)
                if result:
                    normalized = result['canonical']
                    interpreted = True
            except Exception:
                # Provider failure must not prevent a safe deterministic response.
                pass
        text = normalized
        with self.transaction() as db:
            if inbox_id is not None and not db.execute("SELECT 1 FROM inbox WHERE id=? AND status='processing'",(inbox_id,)).fetchone():
                return 'That queued request was cancelled. Nothing was saved.'
            receipt = db.execute('SELECT response FROM receipts WHERE key=?', (key,)).fetchone()
            if receipt:
                return receipt['response'] or 'That message was already handled. Send a new message to continue.'
            suppressed = db.execute('SELECT reason FROM suppressed WHERE user_hash=?', (user_hash,)).fetchone()
            command = text.casefold()
            deletion_pending = db.execute("SELECT 1 FROM pending WHERE user_id=? AND kind='delete'", (user_id,)).fetchone()
            allowed_while_paused = command in ('start', 'delete my data') or (deletion_pending and command in ('confirm','yes','discard','no','cancel'))
            if suppressed and suppressed['reason'] == 'deleted' and command != 'start':
                response = 'Your saved data is already deleted. I’m paused; send START to begin fresh.'
            elif suppressed and not allowed_while_paused:
                response = 'I’m paused. No reminders will be sent. Send START to resume, or DELETE MY DATA to erase saved details.'
            else:
                if command == 'start':
                    db.execute('DELETE FROM suppressed WHERE user_hash=?', (user_hash,))
                db.execute('INSERT OR IGNORE INTO users(user_id,created_at) VALUES (?,?)', (user_id, iso(now)))
                user = dict(db.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone())
                try:
                    response = self._route(db, user, text, now, inbox_id=inbox_id)
                    if interpreted:
                        response = 'I interpreted your message as: ' + text + '\nPlease check the details below.\n' + response
                except (ValueError, ZoneInfoNotFoundError) as exc:
                    response = str(exc) or 'I could not understand that. Send HELP for examples.'
            # Data deletion intentionally keeps only hashed replay identifiers, not cached personal responses.
            deleted = db.execute('SELECT reason FROM suppressed WHERE user_hash=?', (user_hash,)).fetchone()
            cached = None if deleted and deleted['reason'] == 'deleted' else response
            db.execute('INSERT INTO receipts(key,user_hash,response,created_at) VALUES (?,?,?,?)',
                       (key, user_hash, cached, iso(now)))
            return response

    @staticmethod
    def _normalize(text):
        # Narrow conversational aliases, not claims of unrestricted language understanding.
        simple=text.casefold().rstrip(' .?!')
        aliases={'who owes me':'balances','who do i owe':'balances','what do i owe':'balances',
                 'show my balances':'balances','who owes me and who do i owe':'balances',
                 'show my reminders':'tasks','show my tasks':'tasks','what do you remember about me':'memory'}
        if simple in aliases: return aliases[simple]
        match=re.fullmatch(r"(?:(?:i['’]m overwhelmed[, ]+)?(?:can you |could you |please )?)(remind me to .+)",text,re.I)
        if match:
            return match.group(1).rstrip(' .?!')
        match=re.fullmatch(r"(?:can you |please |help me |i want to |i'd like to )?learn\s+(storytelling.*)",text,re.I)
        if match:
            return 'learn '+match.group(1).rstrip(' .?!')
        return text

    @staticmethod
    def _is_expense(text):
        return bool(re.match(r'^(?:(?:i\s+)?(?:expense|spent|paid)\s+|[^\W\d_](?:[^\W\d_]|[ \x27’\-]){0,60}\s+paid\s+)',text,re.I))

    @classmethod
    def _recognized(cls,text):
        cmd=text.casefold()
        return (cmd in {'help','hi','hello','start','stop','confirm','yes','no','discard','cancel','delete my data','memory','profile','balances','tasks','next','learn','journey'}
                or cls._is_expense(text)
                or cmd.startswith(('timezone ','currency ','name ','tone ','quiet ','remind ','done ','cancel ','snooze ','learn ','reflection ')))

    def _propose(self, db, user_id, kind, payload, now, preview):
        existing = db.execute('SELECT 1 FROM pending WHERE user_id=?', (user_id,)).fetchone()
        db.execute('INSERT OR REPLACE INTO pending VALUES (?,?,?,?)',
                   (user_id, kind, json.dumps(payload), iso(now + timedelta(minutes=15))))
        prefix = 'I replaced your previous unconfirmed proposal.\n' if existing else ''
        return prefix + preview + '\nReply CONFIRM to save, or DISCARD. This proposal expires in 15 minutes.'

    def _route(self, db, user, text, now, inbox_id=None):
        uid = user['user_id']; cmd = text.casefold()
        if cmd in ('help', 'hi', 'hello', 'start'):
            return ('Hi' + (f", {user['name']}" if user['name'] else '') + '! I’m Plus One. I can help with expenses, reminders, and tiny learning steps.\n'
                    'Try: TIMEZONE Asia/Kolkata; CURRENCY INR; NAME Alex; EXPENSE INR 1200 for dinner with Maya, Rahul; '
                    'REMIND ME TO submit report tomorrow at 9am; LEARN storytelling for work presentations in 5 minutes.\n'
                    'Also: BALANCES, TASKS, DONE <id>, SNOOZE <id> 10m, CANCEL <id>, REFLECTION <text>, NEXT, MEMORY, '
                    'QUIET 22:00-08:00, TONE warm/concise, STOP, DELETE MY DATA.\n'
                    'Expenses and reminders need your confirmation. Nothing goes to Splitwise or your friends.')
        if cmd in ('confirm', 'yes'):
            return self._confirm(db, user, now)
        if cmd in ('discard', 'no', 'cancel'):
            db.execute('DELETE FROM pending WHERE user_id=?', (uid,))
            return 'Discarded. Nothing from that proposal was saved.'
        if cmd == 'stop':
            db.execute('INSERT OR REPLACE INTO suppressed VALUES (?,?)', (digest(uid), 'stopped'))
            db.execute("UPDATE tasks SET state='cancelled' WHERE user_id=? AND state IN ('scheduled','notified')", (uid,))
            db.execute("UPDATE outbox SET status='cancelled' WHERE user_id=? AND status IN ('pending','claimed')", (uid,))
            db.execute("UPDATE reply_outbox SET status='cancelled' WHERE user_id=? AND status IN ('pending','claimed')",(uid,))
            db.execute("UPDATE inbox SET status='cancelled',payload=NULL WHERE user_id=? AND status IN ('pending','processing','transcribing') AND (? IS NULL OR id<=?)",(uid,inbox_id,inbox_id))
            db.execute('DELETE FROM pending WHERE user_id=?', (uid,))
            return 'I’m paused and all active reminders are cancelled. Your saved memory is kept. Send START to resume or DELETE MY DATA to erase it.'
        if cmd == 'delete my data':
            return self._propose(db, uid, 'delete', {}, now,
                'Delete your profile, expenses, tasks, learning reflections, and queued reminders? This cannot be undone. '
                'Minimal hashed identifiers remain only to block replay and keep messages paused until START.')
        if cmd.startswith('timezone '):
            zone = text[9:].strip()
            try: ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError): raise ValueError('Use an IANA timezone, for example Asia/Kolkata or America/New_York.')
            return self._propose(db, uid, 'profile', {'timezone': zone}, now,
                f"Set your timezone to {zone}? Current local time there is {now.astimezone(ZoneInfo(zone)).strftime('%Y-%m-%d %H:%M %Z')}. Existing task instants will not change.")
        if cmd.startswith('currency '):
            currency=text[9:].strip().upper()
            if currency not in CURRENCY_PRECISION:
                raise ValueError('Choose a supported currency: '+', '.join(CURRENCY_PRECISION)+'.')
            return self._propose(db,uid,'profile',{'currency':currency},now,f'Use {currency} when an expense omits its currency? Explicit currency in a message still takes priority.')
        if cmd.startswith('name '):
            name = text[5:].strip()
            if not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name): raise ValueError('Please use a name of 1–80 printable characters.')
            return self._propose(db, uid, 'profile', {'name': name}, now, f'Call you {name}?')
        if cmd.startswith('tone '):
            tone = cmd[5:].strip()
            if tone not in ('warm', 'concise'): raise ValueError('Choose TONE warm or TONE concise.')
            db.execute('UPDATE users SET tone=? WHERE user_id=?', (tone,uid))
            return f'Tone set to {tone}.'
        if cmd.startswith('quiet '):
            value = text[6:].strip()
            if value.casefold() == 'off': start = end = None
            else:
                match = re.fullmatch(r'(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})', value)
                if not match: raise ValueError('Use QUIET 22:00-08:00 or QUIET off.')
                start, end = match.groups()
                quiet_hours_next(now, user['timezone'] or 'UTC', start, end)
            return self._propose(db, uid, 'profile', {'quiet_start':start,'quiet_end':end}, now,
                'Turn quiet hours off?' if start is None else f'Set quiet hours to {start}–{end} in your saved timezone? Reminders due then wait until quiet hours end.')
        if cmd in ('memory', 'profile'):
            journey = db.execute('SELECT * FROM journeys WHERE user_id=?', (uid,)).fetchone()
            return (f"I remember: name={user['name'] or 'not set'}; timezone={user['timezone'] or 'not set'}; tone={user['tone']}; "
                    f"default currency={user['currency'] or 'not set'}; quiet hours={user['quiet_start'] + '–' + user['quiet_end'] if user['quiet_start'] else 'off'}.\n" +
                    (f"Learning: {journey['topic']} for {journey['goal']}, step {journey['step']+1}.\n" if journey else '') +
                    'Saved expenses, tasks, and learning reflections stay on this demo’s SQLite database. Use DELETE MY DATA to erase them.')
        if self._is_expense(text):
            proposal = parse_expense(text, default_currency=user['currency'])
            return self._propose(db, uid, 'expense', proposal, now, format_expense(proposal))
        if cmd == 'balances':
            balances = {}; labels = {}
            for row in db.execute('SELECT payload FROM expenses WHERE user_id=? ORDER BY id', (uid,)):
                p=json.loads(row['payload'])
                if p['payer']=='me':
                    deltas=[(share['name'],share['amount_minor']) for share in p['shares'] if share['name']!='me']
                else:
                    own=next(share['amount_minor'] for share in p['shares'] if share['name']=='me')
                    deltas=[(p['payer'],-own)]
                for name,amount in deltas:
                    k=(p['currency'],name.casefold()); labels.setdefault(k,name)
                    balances[k]=balances.get(k,0)+amount
            if not balances: return 'No confirmed balances yet. Draft expenses do not count.'
            lines=[]
            for (currency,key),amount in sorted(balances.items()):
                name=labels[(currency,key)]
                lines.append(f'{name} owes you {format_money(amount,currency)}' if amount>0 else
                             f'You owe {name} {format_money(-amount,currency)}' if amount<0 else
                             f'Settled with {name}: {format_money(0,currency)}')
            return 'Your private net balances (no external sync; names are your labels):\n'+'\n'.join(lines)
        if cmd.startswith('remind '):
            if not user['timezone']: raise ValueError('First set and confirm your timezone: TIMEZONE Asia/Kolkata (or your IANA timezone). Then send the reminder again.')
            p=parse_reminder(text,user['timezone'],now)
            delivery=quiet_hours_next(datetime.fromisoformat(p['due_at']),user['timezone'],user['quiet_start'],user['quiet_end'])
            preview=f"I’ve got you. Remind you to {p['title']} on {p['local_display']}?"
            if iso(delivery) != iso(datetime.fromisoformat(p['due_at'])):
                preview += f" Quiet hours delay delivery until {delivery.astimezone(ZoneInfo(user['timezone'])).strftime('%Y-%m-%d %H:%M %Z')}."
            return self._propose(db,uid,'task',p,now,preview)
        if cmd == 'tasks':
            rows=db.execute('SELECT * FROM tasks WHERE user_id=? ORDER BY id',(uid,)).fetchall()
            return '\n'.join(f"#{r['id']} [{r['state']}] {r['title']} — {datetime.fromisoformat(r['due_at']).astimezone(ZoneInfo(r['timezone'])).strftime('%Y-%m-%d %H:%M %Z')}" for r in rows) or 'No confirmed tasks yet.'
        match=re.fullmatch(r'(done|cancel|snooze)\s+#?(\d+)(?:\s+(.+))?',text,re.I)
        if match:
            action,tid,extra=match.groups(); action=action.lower()
            if len(tid)>19 or not 1 <= int(tid) <= 2**63-1:
                raise ValueError('I could not find that task. Send TASKS for your task IDs.')
            task=db.execute('SELECT * FROM tasks WHERE id=? AND user_id=?',(int(tid),uid)).fetchone()
            if not task: raise ValueError('I could not find that task. Send TASKS for your task IDs.')
            if task['state'] not in ('scheduled','notified'): raise ValueError('That task is already done or cancelled. Create a new reminder to start again.')
            if action=='snooze':
                if not extra: raise ValueError('Use SNOOZE <id> 10m (or 2 hours).')
                seconds=parse_snooze_duration(extra)
                due=now+timedelta(seconds=seconds)
                payload={'task_id':int(tid),'due_at':iso(due),'revision':task['revision']}
                local=due.astimezone(ZoneInfo(task['timezone'])).strftime('%Y-%m-%d %H:%M %Z')
                delivery=quiet_hours_next(due,user['timezone'] or task['timezone'],user['quiet_start'],user['quiet_end'])
                note='' if iso(delivery)==iso(due) else ' Delivery waits until quiet hours end.'
                return self._propose(db,uid,'snooze',payload,now,f'Snooze #{tid} until {local}?{note}')
            if extra: raise ValueError(f'Use {action.upper()} <id> without extra text.')
            state='done' if action=='done' else 'cancelled'
            db.execute('UPDATE tasks SET state=? WHERE id=?',(state,int(tid)))
            db.execute("UPDATE outbox SET status='cancelled' WHERE task_id=? AND status IN ('pending','claimed')",(int(tid),))
            return f"Nice work — #{tid} is done." if action=='done' else f'Reminder #{tid} cancelled.'
        if cmd.startswith('learn '):
            match=re.fullmatch(r'learn\s+(storytelling)(?:\s+for\s+(.+?))?(?:\s+in\s+(\d+)\s+minutes?)?',text,re.I)
            if not match: raise ValueError('Try LEARN storytelling for work presentations in 5 minutes. Storytelling is the demo journey available today.')
            topic,goal,minutes=match.groups(); goal=goal or 'everyday conversations'; minutes=int(minutes or 5)
            if not 1<=minutes<=20: raise ValueError('Choose a small learning step of 1–20 minutes.')
            if len(goal)>200: raise ValueError('Please keep your learning goal under 200 characters.')
            learning_step(topic.lower(),goal,minutes,0)
            previous=db.execute('SELECT 1 FROM journeys WHERE user_id=?',(uid,)).fetchone()
            if previous:
                return self._propose(db,uid,'journey',{'topic':topic.lower(),'goal':goal,'minutes':minutes},now,'Replace your current learning journey? Existing reflections will be cleared.')
            db.execute('INSERT INTO journeys(user_id,topic,goal,minutes) VALUES (?,?,?,?)',(uid,topic.lower(),goal,minutes))
            return self._lesson(db,uid)
        if cmd.startswith('reflection '):
            reflection=text[11:].strip()
            if not reflection: raise ValueError('What stood out? Send REFLECTION followed by a sentence.')
            journey=db.execute('SELECT * FROM journeys WHERE user_id=?',(uid,)).fetchone()
            if not journey: raise ValueError('Start a tiny journey with LEARN storytelling for work presentations in 5 minutes.')
            db.execute('INSERT INTO reflections(user_id,step,text,created_at) VALUES (?,?,?,?)',(uid,journey['step'],reflection,iso(now)))
            db.execute('UPDATE journeys SET awaiting_reflection=0 WHERE user_id=?',(uid,))
            return 'Thanks — I’ve saved that reflection for our next step. Send NEXT when you’re ready; no rush.'
        if cmd in ('next','learn','journey'):
            journey=db.execute('SELECT * FROM journeys WHERE user_id=?',(uid,)).fetchone()
            if not journey: raise ValueError('Start with LEARN storytelling for work presentations in 5 minutes.')
            if cmd=='next':
                if journey['awaiting_reflection']: return 'Before we move on, what did you notice? Send REFLECTION <your thought>, or JOURNEY to revisit this step.'
                # The helper bounds the curriculum; leave current step unchanged if it is complete.
                try: learning_step(journey['topic'],journey['goal'],journey['minutes'],journey['step']+1)
                except ValueError: return 'You’ve completed this micro-journey. Keep practising your story, or start LEARN storytelling for a new goal.'
                db.execute('UPDATE journeys SET step=step+1,awaiting_reflection=1 WHERE user_id=?',(uid,))
            return self._lesson(db,uid)
        return 'I can help, but I need a little more detail. Send HELP for expense, reminder, and learning examples. Nothing was recorded.'

    def _lesson(self,db,uid):
        j=db.execute('SELECT * FROM journeys WHERE user_id=?',(uid,)).fetchone()
        p=learning_step(j['topic'],j['goal'],j['minutes'],j['step'])
        previous=db.execute('SELECT text FROM reflections WHERE user_id=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
        excerpt=(previous['text'][:400] + ('…' if len(previous['text'])>400 else '')) if previous else ''
        continuity=f"Last time you noticed: “{excerpt}”\n" if previous else ''
        return (f"{continuity}Step {j['step']+1}: {p['title']} ({j['minutes']} min, for {j['goal']})\n"
                f"{p['lesson']}\nTry this: {p['exercise']}\n{p['reflection_prompt']}\nReply REFLECTION <your thought>. Your pace, not a streak.")

    def _confirm(self,db,user,now):
        uid=user['user_id']; row=db.execute('SELECT * FROM pending WHERE user_id=?',(uid,)).fetchone()
        if not row: return 'There’s no proposal to confirm. Nothing changed.'
        if datetime.fromisoformat(row['expires_at'])<=now:
            db.execute('DELETE FROM pending WHERE user_id=?',(uid,)); return 'That proposal expired. Please send the details again so we can confirm them safely.'
        p=json.loads(row['payload']); kind=row['kind']
        db.execute('DELETE FROM pending WHERE user_id=?',(uid,))
        if kind=='delete':
            db.execute('DELETE FROM reply_outbox WHERE user_id=?',(uid,))
            db.execute("UPDATE inbox SET user_id=NULL,message_id=NULL,payload=NULL,status='erased',error=NULL WHERE user_hash=?",(digest(uid),))
            db.execute('DELETE FROM users WHERE user_id=?',(uid,))
            db.execute('UPDATE receipts SET response=NULL WHERE user_hash=?',(digest(uid),))
            db.execute('INSERT OR REPLACE INTO suppressed VALUES (?,?)',(digest(uid),'deleted'))
            return 'Your saved profile, expenses, tasks, learning reflections, and reminder queue are deleted. I’m paused. Only hashed replay/suppression identifiers remain. Send START to begin fresh.'
        if kind=='profile':
            allowed={'timezone','currency','name','quiet_start','quiet_end'}
            if not set(p)<=allowed: raise ValueError('Invalid profile proposal.')
            for field,value in p.items(): db.execute(f'UPDATE users SET {field}=? WHERE user_id=?',(value,uid))
            return 'Saved. I’ll use that preference going forward.'
        if kind=='expense':
            cur=db.execute('INSERT INTO expenses(user_id,payload,created_at) VALUES (?,?,?)',(uid,json.dumps(p),iso(now)))
            return f'Expense #{cur.lastrowid} recorded in your private balances. No payment or external update was made.'
        if kind=='task':
            if datetime.fromisoformat(p['due_at'])<=now: return 'That reminder time has already passed. Please choose a new time.'
            cur=db.execute('INSERT INTO tasks(user_id,title,due_at,timezone,created_at) VALUES (?,?,?,?,?)',(uid,p['title'],iso(datetime.fromisoformat(p['due_at'])),p['timezone'],iso(now)))
            return f"Reminder #{cur.lastrowid} saved for {p['local_display']}. You can DONE, SNOOZE, or CANCEL it by ID. Quiet hours are respected."
        if kind=='snooze':
            task=db.execute('SELECT * FROM tasks WHERE id=? AND user_id=?',(p['task_id'],uid)).fetchone()
            if not task or task['state'] not in ('scheduled','notified') or task['revision']!=p['revision']:
                return 'That task changed since the preview. Send TASKS and try again.'
            if datetime.fromisoformat(p['due_at'])<=now: return 'That snooze time has already passed. Please choose a new duration.'
            db.execute("UPDATE tasks SET due_at=?,state='scheduled',revision=revision+1 WHERE id=?",(p['due_at'],p['task_id']))
            db.execute("UPDATE outbox SET status='cancelled' WHERE task_id=? AND status IN ('pending','claimed')",(p['task_id'],))
            return f"Reminder #{p['task_id']} snoozed."
        if kind=='journey':
            db.execute('DELETE FROM reflections WHERE user_id=?',(uid,))
            db.execute('INSERT OR REPLACE INTO journeys(user_id,topic,goal,minutes) VALUES (?,?,?,?)',(uid,p['topic'],p['goal'],p['minutes']))
            return self._lesson(db,uid)
        raise ValueError('Unknown proposal; please start again.')

    def process_due(self,now=None):
        now=self._now(now); queued=[]
        with self.transaction() as db:
            rows=db.execute("SELECT t.*,u.name,u.tone,u.timezone AS user_timezone,u.quiet_start,u.quiet_end FROM tasks t JOIN users u USING(user_id) WHERE t.state='scheduled' AND t.due_at<=? ORDER BY t.due_at,t.id",(iso(now),)).fetchall()
            for row in rows:
                if db.execute('SELECT 1 FROM suppressed WHERE user_hash=?',(digest(row['user_id']),)).fetchone(): continue
                delivery=quiet_hours_next(now,row['user_timezone'] or row['timezone'],row['quiet_start'],row['quiet_end'])
                if delivery>now: continue
                prefix=f"Hey {row['name']}, " if row['name'] else 'A gentle nudge: '
                if row['tone']=='concise': prefix='Reminder: '
                body=f"{prefix}{row['title']}. Reply DONE {row['id']}, SNOOZE {row['id']} 10m, or CANCEL {row['id']}."
                cur=db.execute('INSERT OR IGNORE INTO outbox(user_id,task_id,revision,body,created_at) VALUES (?,?,?,?,?)',(row['user_id'],row['id'],row['revision'],body,iso(now)))
                db.execute("UPDATE tasks SET state='notified' WHERE id=?",(row['id'],))
                if cur.rowcount: queued.append({'id':cur.lastrowid,'user_id':row['user_id'],'task_id':row['id'],'body':body})
        return queued

    def claim_outbox(self):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if not row: return None
            if db.execute('SELECT 1 FROM suppressed WHERE user_hash=?',(digest(row['user_id']),)).fetchone():
                db.execute("UPDATE outbox SET status='cancelled' WHERE id=?",(row['id'],)); return None
            db.execute("UPDATE outbox SET status='claimed' WHERE id=?",(row['id'],))
            return dict(row)

    def enqueue_inbound(self,provider,message_id,user_id,payload,now=None):
        """Store authenticated event quickly. Provider ID is globally deduplicated."""
        now=self._now(now)
        received=datetime.fromisoformat(payload['received_at'])
        key=digest(provider+'\0'+message_id)
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM inbox WHERE event_key=?',(key,)).fetchone(): return False
            suppressed=db.execute('SELECT reason FROM suppressed WHERE user_hash=?',(digest(user_id),)).fetchone()
            ignored=bool(suppressed and ((suppressed['reason']=='deleted' and (payload.get('text') or '').strip().casefold()!='start') or payload.get('kind')=='audio'))
            db.execute('INSERT INTO inbox(event_key,provider,user_hash,user_id,message_id,payload,status,created_at,received_at) VALUES (?,?,?,?,?,?,?,?,?)',
                       (key,provider,digest(user_id),None if ignored else user_id,None if ignored else message_id,
                        None if ignored else json.dumps(payload),'ignored' if ignored else 'pending',iso(now),iso(received)))
            return True

    def claim_inbound(self):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if not row: return None
            payload=json.loads(row['payload'])
            status='transcribing' if payload.get('kind')=='audio' else 'processing'
            db.execute('UPDATE inbox SET status=? WHERE id=?',(status,row['id']))
            result=dict(row); result['payload']=payload
            return result

    def save_inbound_transcript(self,inbox_id,text):
        with self.transaction() as db:
            row=db.execute("SELECT payload FROM inbox WHERE id=? AND status='transcribing'",(inbox_id,)).fetchone()
            if not row: return False
            payload=json.loads(row['payload']); payload.update(kind='text',text=text,media=None)
            db.execute("UPDATE inbox SET payload=?,status='processing' WHERE id=?",(json.dumps(payload),inbox_id))
            return True

    def finish_inbound(self,inbox_id,response,now=None):
        """Return False after privacy deletion: caller may send one transient reply."""
        with self.transaction() as db:
            row=db.execute('SELECT * FROM inbox WHERE id=?',(inbox_id,)).fetchone()
            if not row or row['status']=='erased': return False
            if row['status']=='cancelled' and not response.startswith('I’m paused and all active reminders'): return False
            db.execute('INSERT OR IGNORE INTO reply_outbox(inbox_id,user_id,body,created_at) VALUES (?,?,?,?)',
                       (inbox_id,row['user_id'],response,iso(self._now(now))))
            # Keep source timestamp for session proof; no longer need raw audio URL/text.
            db.execute("UPDATE inbox SET status='completed',payload=NULL WHERE id=?",(inbox_id,))
            return True

    def claim_reply(self):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM reply_outbox WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row: db.execute("UPDATE reply_outbox SET status='claimed' WHERE id=?",(row['id'],))
            return dict(row) if row else None

    def reply_is_active(self,reply_id):
        with self._lock:
            return bool(self.db.execute("SELECT 1 FROM reply_outbox WHERE id=? AND status='claimed'",(reply_id,)).fetchone())

    def finish_reply(self,reply_id,status,provider_id=None,error=None):
        if status not in ('sent','simulated','failed','uncertain'): raise ValueError('Invalid reply status.')
        with self.transaction() as db:
            db.execute("UPDATE reply_outbox SET status=?,provider_id=?,error=? WHERE id=? AND status='claimed'",(status,provider_id,error,reply_id))

    def session_is_open(self,user_id,now=None):
        now=self._now(now)
        with self._lock:
            row=self.db.execute('SELECT MAX(received_at) FROM inbox WHERE user_hash=?',(digest(user_id),)).fetchone()
            if not row or not row[0]: return False
            received=datetime.fromisoformat(row[0])
            return now-timedelta(hours=24)<received<=now+timedelta(minutes=5)

    def recover_inbox(self):
        with self.transaction() as db:
            # Pure text processing is repeatable via Engine.handle receipts. A
            # crashed external STT call may have incurred cost: never re-invoke.
            db.execute("UPDATE inbox SET status='pending' WHERE status='processing'")
            db.execute("UPDATE inbox SET status='uncertain',payload=NULL,error='Transcription interrupted; please send text instead. No automatic provider retry.' WHERE status='transcribing'")
            db.execute("UPDATE reply_outbox SET status='uncertain',error='Send interrupted; manual reconciliation required.' WHERE status='claimed'")

    def inbound_preflight(self,user_id,message_id):
        """Return cached/paused reply before invoking any voice or text provider."""
        with self._lock:
            row=self.db.execute('SELECT response FROM receipts WHERE key=?',(digest(user_id+'\0'+message_id),)).fetchone()
            if row: return row['response'] or 'That message was already handled. Send a new message to continue.'
            paused=self.db.execute('SELECT reason FROM suppressed WHERE user_hash=?',(digest(user_id),)).fetchone()
            if paused: return 'I’m paused. No audio was processed. Send START as text to resume.'
        return None

    def remember_response(self,user_id,message_id,response,now=None):
        """Cache a provider fallback without creating personal profile/flow state."""
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)',
                       (digest(user_id+'\0'+message_id),digest(user_id),response,iso(self._now(now))))

    def delivery_is_active(self,outbox_id):
        with self._lock:
            row=self.db.execute("SELECT o.user_id FROM outbox o JOIN tasks t ON t.id=o.task_id WHERE o.id=? AND o.status='claimed' AND t.state='notified' AND o.revision=t.revision",(outbox_id,)).fetchone()
            return bool(row) and not bool(self.db.execute('SELECT 1 FROM suppressed WHERE user_hash=?',(digest(row['user_id']),)).fetchone())

    def finish_delivery(self,outbox_id,status,provider_id=None,error=None):
        if status not in ('sent','simulated','failed','uncertain'): raise ValueError('Invalid delivery status.')
        with self.transaction() as db:
            db.execute("UPDATE outbox SET status=?,provider_id=?,error=? WHERE id=? AND status='claimed'",(status,provider_id,error,outbox_id))

    def recover_inflight(self):
        # Only call once when starting the sole worker, never from a second active worker.
        with self.transaction() as db:
            return db.execute("UPDATE outbox SET status='uncertain',error='Worker restarted while send outcome was unknown; manual reconciliation required.' WHERE status='claimed'").rowcount

    def snapshot(self,user_id):
        with self._lock:
            user=self.db.execute('SELECT * FROM users WHERE user_id=?',(user_id,)).fetchone()
            result={'profile':dict(user) if user else None}
            for table in ('pending','expenses','tasks','journeys','reflections','outbox','inbox','reply_outbox'):
                result[table]=[dict(r) for r in self.db.execute(f'SELECT * FROM {table} WHERE user_id=?',(user_id,))]
            suppressed=self.db.execute('SELECT reason FROM suppressed WHERE user_hash=?',(digest(user_id),)).fetchone()
            result['paused']=bool(suppressed)
            return result
