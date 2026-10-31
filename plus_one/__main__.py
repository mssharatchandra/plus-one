"""Run `python -m plus_one --help`. All defaults are local/offline."""
import argparse
import json
import os
import threading
import uuid
from .engine import Engine
from .worker import WorkerLock, run_once, run_forever

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description='Plus One — local three-flow WhatsApp backend PoC (offline by default)')
    p.add_argument('--db',default='plus_one.sqlite3',help='Private SQLite path')
    sub=p.add_subparsers(dest='command',required=True)
    chat=sub.add_parser('chat',help='Interactive deterministic text demo; /quit exits, /tick simulates due reminders')
    chat.add_argument('--user',default='demo')
    message=sub.add_parser('message',help='One deterministic message')
    message.add_argument('text'); message.add_argument('--user',default='demo'); message.add_argument('--id',default=None)
    state=sub.add_parser('state',help='Print private local state'); state.add_argument('--user',default='demo')
    tick=sub.add_parser('tick',help='Process due reminders once, locally simulated (never sends)')
    worker=sub.add_parser('worker',help='Durable scheduler; default simulated deliveries')
    worker.add_argument('--live-gupshup',action='store_true',help='Primary WhatsApp provider; requires explicitly enabled authorized tester configuration')
    worker.add_argument('--live-twilio',action='store_true',help='Requires explicit authorized tester transport + approved template configuration')
    serve=sub.add_parser('serve',help='Authenticated loopback HTTP server with local scheduler')
    serve.add_argument('--port',type=int,default=8080)
    serve.add_argument('--live-gupshup',action='store_true',help='Primary WhatsApp provider; requires explicitly enabled authorized tester configuration')
    serve.add_argument('--live-twilio',action='store_true',help='Requires explicit authorized tester transport + approved template configuration')
    demo=sub.add_parser('demo',help='Run reproducible all-three-flow scripted demo in a temporary database')
    args=p.parse_args()
    if args.command=='demo':
        from .demo import run_demo
        run_demo(); return
    from .interpretation import enabled_interpreter_from_env
    engine=Engine(args.db,interpreter=enabled_interpreter_from_env())
    try:
        if args.command=='chat':
            print('Plus One local demo. Text only: voice/STT and live WhatsApp are not connected. Type HELP, /tick, or /quit.')
            while True:
                try: text=input('You > ').strip()
                except (EOFError,KeyboardInterrupt): print(); break
                if text=='/quit': break
                if text=='/tick':
                    with WorkerLock(engine.db_path):
                        engine.recover_inflight(); engine.recover_inbox(); results=run_once(engine)
                    print('Local simulated delivery:',json.dumps(results)); continue
                if text: print('Plus One >',engine.handle(args.user,text,uuid.uuid4().hex))
        elif args.command=='message': print(engine.handle(args.user,args.text,args.id or uuid.uuid4().hex))
        elif args.command=='state': print(json.dumps(engine.snapshot(args.user),indent=2))
        elif args.command=='tick':
            with WorkerLock(engine.db_path):
                engine.recover_inflight(); engine.recover_inbox(); print(json.dumps(run_once(engine),indent=2))
        else:
            transport=None; transcribers={}; mode='LOCAL SIMULATION — no WhatsApp sends'
            if args.live_gupshup and args.live_twilio:
                raise ValueError('Choose one live provider per worker: Gupshup (primary) or Twilio (fallback).')
            from .gupshup import GupshupConfig,GupshupTransport,configured_gupshup_transcriber
            gupshup_config=GupshupConfig.from_env()
            if gupshup_config.send_enabled and not args.live_gupshup:
                raise ValueError('ENABLE_GUPSHUP_SEND requires --live-gupshup so real callbacks cannot be silently simulated.')
            if args.live_gupshup:
                gupshup_config.validate_inbound()
                if not gupshup_config.send_enabled or not gupshup_config.api_key:
                    raise ValueError('Live Gupshup requires ENABLE_GUPSHUP_SEND=true and secure API key configuration.')
                transport=GupshupTransport(gupshup_config,session_is_open=engine.session_is_open)
                transcriber=configured_gupshup_transcriber(gupshup_config)
                if transcriber: transcribers['gupshup']=transcriber
                mode='LIVE GUPSHUP (authorized testers only)'
            if args.live_twilio:
                from .transport import TwilioConfig,TwilioTransport
                config=TwilioConfig.from_env()
                config.validate_inbound()
                if not config.send_enabled or not config.content_sid:
                    raise ValueError('Live scheduler requires ENABLE_TWILIO_SEND=true and an approved TWILIO_CONTENT_SID template.')
                transport=TwilioTransport(config)
                mode='LIVE TWILIO (authorized testers only)'
            if args.command=='worker':
                print('Scheduler active. Mode:', mode)
                try: run_forever(engine,transport=transport,transcribers=transcribers)
                except KeyboardInterrupt: pass
            elif args.command=='serve':
                from .server import make_server,ServerConfig
                from .speech import configured_transcriber
                server_config=ServerConfig.from_env()
                server=make_server(engine,server_config,port=args.port,transcriber=configured_transcriber(server_config.twilio) if args.live_twilio else None)
                stop=threading.Event()
                # Acquire synchronously: failure must not leave HTTP running without a worker.
                with WorkerLock(engine.db_path):
                    engine.recover_inflight()
                    engine.recover_inbox()
                    def loop():
                        while not stop.is_set():
                            run_once(engine,transport,transcribers=transcribers)
                            stop.wait(1)
                    thread=threading.Thread(target=loop,daemon=True); thread.start()
                    print(f'Authenticated local server: http://127.0.0.1:{server.server_port}; scheduler mode: '+mode)
                    try: server.serve_forever()
                    except KeyboardInterrupt: pass
                    finally:
                        stop.set(); thread.join(timeout=65); server.server_close()
    finally: engine.close()

if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError) as exc: raise SystemExit(str(exc))
