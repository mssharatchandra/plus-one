"""Durable outbox worker. Default delivery is explicit local simulation only."""
from __future__ import annotations
import os
from pathlib import Path
import json
import threading
from .transport import DeliveryError, DeliveryUncertain, TransportDisabled
from .zai_speech import VoiceInputError

_VOICE_STAGES = {"media_validation", "media_fetch", "media_type", "media_read", "asr", "worker"}
_VOICE_CATEGORIES = {"credit", "auth", "rate", "model", "other", "dns", "tls", "timeout",
                     "connect_or_policy", "unexpected", "content_type", "empty_transcript",
                     "invalid_transcript", "invalid_json_or_size", "invalid_metadata",
                     "octet_stream", "known_audio_mismatch", "unknown_mime", "speech_error"}


def _record_voice_diagnostic(inbox_id, exc):
    """Optional local operator log: fixed enums only, never content or identifiers."""
    path = os.environ.get("VOICE_DIAGNOSTIC_PATH")
    if not path:
        return
    stage = getattr(exc, "stage", "worker")
    category = getattr(exc, "category", "unexpected")
    status = getattr(exc, "status", None)
    event = {"inbox_id": inbox_id,
             "stage": stage if stage in _VOICE_STAGES else "worker",
             "category": category if category in _VOICE_CATEGORIES else "other",
             "http_status": status if isinstance(status, int) and 100 <= status <= 599 else None}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="ascii") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        pass  # Diagnostic failure must never prevent a safe user reply.

class WorkerLock:
    """POSIX process lock: only one scheduler per database; releases on crash."""
    def __init__(self, db_path):
        self.path = str(Path(db_path).resolve()) + '.worker.lock'
        self.file = None
    def __enter__(self):
        import fcntl
        self.file = open(self.path, 'a', encoding='utf-8')
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError('Another worker is already using this database.') from None
        return self
    def __exit__(self,*args):
        self.file.close()


def _send(transport,recipient,body,key):
    if transport is None: return 'simulated',None,None
    try:
        return 'sent',transport.send(recipient,body,idempotency_key=key),None
    except TransportDisabled:
        return 'failed',None,'Transport disabled or incomplete configuration. No network send was attempted.'
    except DeliveryError:
        return 'failed',None,'Provider rejected or send policy blocked this message; inspect configuration before manual retry.'
    except DeliveryUncertain:
        return 'uncertain',None,'Provider outcome unknown. Reconcile before any retry.'
    except Exception:
        return 'uncertain',None,'Unexpected transport failure. Reconcile before any retry.'


def process_inbox(engine,transport=None,transcribers=None):
    results=[]; transcribers=transcribers or {}
    # Bound each pass so a stream of callbacks cannot starve due reminders.
    for _ in range(50):
        row=engine.claim_inbound()
        if row is None: break
        payload=row['payload']; uid=row['user_id']; mid=row['provider']+':'+row['message_id']
        text=payload.get('text'); response=None
        if payload['kind']=='audio':
            response=engine.inbound_preflight(uid,mid)
            if response is None:
                transcriber=transcribers.get(row['provider'])
                if transcriber is None:
                    response="Voice transcription isn't connected in this demo yet. Please type what you said; nothing from the note was saved."
                else:
                    try:
                        text=transcriber.transcribe(payload['media'])
                        if not isinstance(text,str) or not text.strip() or len(text)>4000: raise ValueError('Invalid transcript')
                        if os.environ.get('VOICE_TRANSCRIPTION_TEST_ONLY','').lower() == 'true':
                            response='I heard: '+text.strip()+'\nVoice transcription test only. Nothing was scheduled or saved as a task.'
                        else:
                            if not engine.save_inbound_transcript(row['id'],text): continue
                    except VoiceInputError as exc:
                        response=str(exc)
                    except Exception as exc:
                        _record_voice_diagnostic(row['id'], exc)
                        response="I couldn't transcribe that voice note. Please type the message instead; nothing from the note was saved."
        elif payload['kind']!='text':
            response='Please send text or one voice note for this demo. Images and other attachments are not processed.'
        if response is None:
            response=engine.handle(uid,text,mid,inbox_id=row['id'])
        persisted=engine.finish_inbound(row['id'],response)
        if not persisted:
            # Deletion scrubs recipient and content from every durable queue. Its
            # acknowledgement may be attempted once from transient worker memory.
            if response.startswith('Your saved profile, expenses, tasks'):
                status,pid,error=_send(transport,uid,response,'deleted:'+str(row['id']))
                results.append({'inbox_id':row['id'],'kind':'transient-deletion-ack','status':status,'provider_id':pid})
    return results


def run_once(engine, transport=None, now=None, transcribers=None):
    """Process durable inbox, then replies and reminders. Default is simulation."""
    results=process_inbox(engine,transport,transcribers)
    while True:
        row=engine.claim_reply()
        if row is None: break
        if not engine.reply_is_active(row['id']): continue
        status,pid,error=_send(transport,row['user_id'],row['body'],'reply:'+str(row['inbox_id']))
        engine.finish_reply(row['id'],status,pid,error)
        results.append({'reply_id':row['id'],'status':status,'provider_id':pid})
    engine.process_due(now)
    while True:
        row=engine.claim_outbox()
        if row is None: break
        if not engine.delivery_is_active(row['id']): continue
        status,pid,error=_send(transport,row['user_id'],row['body'],f"reminder:{row['task_id']}:{row['revision']}")
        engine.finish_delivery(row['id'],status,pid,error)
        results.append({'outbox_id':row['id'],'status':status,'provider_id':pid})
    return results


def run_forever(engine, stop_event=None, transport=None, interval=1.0, transcribers=None):
    stop_event=stop_event or threading.Event()
    if interval < 0.1: raise ValueError('Worker interval must be at least 0.1 seconds.')
    with WorkerLock(engine.db_path):
        engine.recover_inflight()
        engine.recover_inbox()
        while not stop_event.is_set():
            run_once(engine,transport,transcribers=transcribers)
            stop_event.wait(interval)
