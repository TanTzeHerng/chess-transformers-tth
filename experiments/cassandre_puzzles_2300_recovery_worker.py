#!/usr/bin/env python3
"""Off-PC Cassandre 0.24 (Windows) 30-second/move Chess.com 2300+ puzzle worker.
Only exact source continuation counts as solved, as in BT4/Classic2/Beast runs.
"""
import argparse
import datetime
import hashlib
import json
import os
import time
import traceback
import urllib.request
import urllib.error
from pathlib import Path
import chess
import chess.engine

def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def atomic(path, data):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(data,indent=2),encoding='utf-8')
    os.replace(temp,path)

class LegacyXBoard:
    """Direct XBoard protocol adapter, avoids python-chess's ping=1 requirement."""
    def __init__(self,exe):
        import subprocess,threading,queue
        self.exe=exe
        self.out=queue.Queue()
        self.proc=subprocess.Popen([str(exe)],cwd=str(exe.parent),
             stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
             text=True,encoding='utf-8',errors='replace',bufsize=1)
        def reader():
            for line in self.proc.stdout:
                self.out.put(line.strip())
        threading.Thread(target=reader,daemon=True).start()
        self.send('xboard')
        self.send('protover 2')
        self.send('new')
        self.send('force')
    def send(self,text):
        if self.proc.poll() is not None:
            raise RuntimeError('Cassandre process exited '+str(self.proc.returncode))
        self.proc.stdin.write(text+'\n')
        self.proc.stdin.flush()
    def play(self,board,limit,game,info):
        import queue
        while True:
            try:self.out.get_nowait()
            except queue.Empty:break
        self.send('force')
        self.send('setboard '+board.fen(en_passant='fen'))
        self.send('st '+str(round(limit.time)))
        self.send('go')
        start=time.monotonic()
        deadline=start+max(limit.time+35,65)
        stop_sent=False
        snippets=[]
        while time.monotonic()<deadline:
            if not stop_sent and time.monotonic()-start>=limit.time+6:
                try:
                    self.send('?')  # XBoard: move now, after requested think time.
                    stop_sent=True
                except Exception:
                    break
            try:line=self.out.get(timeout=min(.5,max(.001,deadline-time.monotonic())))
            except queue.Empty:
                if self.proc.poll() is not None:break
                continue
            if len(snippets)<8 and line:
                snippets.append(line[:120])
            low=line.lower()
            if low.startswith('move ') or low.startswith('bestmove ') or (len(low) in (4,5) and low[0] in 'abcdefgh' and low[1] in '12345678' and low[2] in 'abcdefgh' and low[3] in '12345678'):
                token=line.split()[1] if ' ' in line else line
                try:m=chess.Move.from_uci(token.lower())
                except ValueError:continue
                if m not in board.legal_moves:
                    raise RuntimeError('Cassandre XBoard illegal '+token+'; output '+repr(snippets))
                return type('EngineReply',(),{'move':m,'info':{}})()
            if 'error' in low and len(snippets)<5: print('XBOARD_MESSAGE',line[:160],flush=True)
        raise RuntimeError('Cassandre XBoard did not return a move; stop_sent='+str(stop_sent)+' alive='+str(self.proc.poll())+' output '+repr(snippets))
    def quit(self):
        try:self.send('quit')
        except Exception:pass
        self.close()
    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:self.proc.kill()
                except Exception:pass
        for pipe in (self.proc.stdin,self.proc.stdout,self.proc.stderr):
            try:
                if pipe:pipe.close()
            except Exception:pass

def get_engine(exe):
    engine=LegacyXBoard(exe)
    print('ENGINE_PROTOCOL xboard-raw (no ping required)',flush=True)
    return engine,'xboard-raw'

def solve(engine, row, time_per_move):
    board=chess.Board(row['initialFen'])
    board.push_uci(row['setupMoveUci'])
    if board.fen(en_passant='fen')!=row['puzzleFen']:
        raise RuntimeError("Source puzzle FEN mismatch")
    moves=[]; correct_moves=0
    for ply,expected in enumerate(row['solutionUci']):
        if ply%2:
            board.push_uci(expected)
            continue
        begin=time.monotonic()
        answer=engine.play(board,chess.engine.Limit(time=time_per_move),
                           game=object(),info=chess.engine.INFO_BASIC)
        seconds=time.monotonic()-begin
        if not answer.move or answer.move not in board.legal_moves:
            raise RuntimeError('No legal engine move from Cassandre')
        chosen=answer.move.uci()
        event={'fen':board.fen(en_passant='fen'),'expected':expected,
               'chosen':chosen,'seconds':round(seconds,3),
               'correct':chosen==expected}
        moves.append(event)
        if chosen!=expected: return False,moves
        correct_moves+=1
        board.push(answer.move)
    return True,moves

class Reporter:
    def __init__(self,name):
        self.name=name
        self.token=os.environ.get('GH_CHECK_TOKEN','')
        self.repo=os.environ.get('GITHUB_REPOSITORY','')
        self.sha=os.environ.get('GITHUB_SHA','')
        self.id=None
        self.last=0
        if self.token and self.repo and self.sha:
            self.send({'name':name,'head_sha':self.sha,'status':'in_progress',
                       'output':{'title':'Cassandre puzzle progress','summary':'Remote worker initializing'}})
    def send(self,payload):
        method='PATCH' if self.id else 'POST'
        endpoint=f'https://api.github.com/repos/{self.repo}/check-runs'
        if self.id: endpoint+=f'/{self.id}'
        body=json.dumps(payload).encode('utf8')
        req=urllib.request.Request(endpoint,data=body,method=method,headers={
          'Authorization':'Bearer '+self.token,
          'Accept':'application/vnd.github+json',
          'X-GitHub-Api-Version':'2022-11-28',
          'User-Agent':'Cassandre-Puzzle-Benchmark',
          'Content-Type':'application/json'})
        for trial in range(3):
            try:
                with urllib.request.urlopen(req,timeout=25) as res:
                    data=json.loads(res.read())
                if not self.id: self.id=data['id']
                return
            except Exception as e:
                print('CHECK_UPDATE_ERROR',repr(e),flush=True)
                time.sleep(trial+1)
    def update(self,progress,final=False):
        if not self.id:return
        now=time.monotonic()
        if not final and now-self.last<45:return
        self.last=now
        summary=json.dumps(progress,ensure_ascii=False)
        status='completed' if final else 'in_progress'
        data={'status':status,'output':{'title':f"Cassandre {progress['completed']} / {progress['assigned']}",
                                         'summary':summary}}
        if final: data['conclusion']='success' if progress['state']=='completed' else 'neutral'
        self.send(data)

def main():
    pa=argparse.ArgumentParser()
    pa.add_argument('--exe',type=Path,required=True)
    pa.add_argument('--dataset',type=Path,required=True)
    pa.add_argument('--out',type=Path,required=True)
    pa.add_argument('--start',type=int,default=0)
    pa.add_argument('--count',type=int,default=135)
    pa.add_argument('--seconds',type=float,default=30.0)
    pa.add_argument('--resume',type=Path,default=None)
    pa.add_argument('--max-technical-retries',type=int,default=2)
    args=pa.parse_args()
    if args.max_technical_retries<1:raise ValueError('Must try every unscored puzzle')
    args.out.mkdir(parents=True,exist_ok=True)
    rows=[]
    with args.dataset.open(encoding='utf8') as f:
        for idx,line in enumerate(f):
            if idx>=args.start+args.count:break
            if idx>=args.start:rows.append((idx,json.loads(line)))
    if not rows:raise RuntimeError('Empty assigned puzzle shard')
    expected={idx:row for idx,row in rows}
    prior={}
    def load_journal(path):
        if not path or not path.exists():return
        for ln in path.read_text(encoding='utf8').splitlines():
            try:record=json.loads(ln)
            except ValueError:continue
            idx=record.get('index')
            if idx not in expected or record.get('id')!=expected[idx]['id']:
                raise ValueError('Checkpoint puzzle index or ID mismatches current dataset: '+str(idx))
            if isinstance(record.get('solved'),bool) or record.get('invalid_source'):
                prior[idx]=record
    load_journal(args.resume)
    # Recovered records are always carried forward; technical attempts are never scored.
    journal_path=args.out/'results.jsonl'
    if journal_path.exists():load_journal(journal_path)
    with journal_path.open('w',encoding='utf8') as file:
        for idx in sorted(prior):
            file.write(json.dumps(prior[idx],separators=(',',':'))+'\n')
    progress={'state':'starting','engine':'Cassandre 0.24','seconds_per_move':args.seconds,
              'start_index':args.start,'assigned':len(rows),
              'completed':len(prior),
              'solved':sum(r.get('solved') is True for r in prior.values()),
              'failed':sum(r.get('solved') is False for r in prior.values()),
              'invalid_source':sum(bool(r.get('invalid_source')) for r in prior.values()),
              'technical_errors':0,'technical_pending':0,'attempted_this_run':0,
              'recovered_before_run':len(prior),'updated_at':stamp(),'current_id':None,'last':None}
    atomic(args.out/'status.json',progress)
    with args.exe.open('rb') as f:
        exe_sha=hashlib.file_digest(f,'sha256').hexdigest()
    expected_sha='17d69f321244ab1f6530341b9207fe32fd415e80e017e6c33a827c84755839b7'
    if exe_sha!=expected_sha:raise RuntimeError('Wrong Cassandre binary SHA '+exe_sha)
    print('CASSANDRE_SHA256',exe_sha,flush=True)
    reporter=Reporter(f'Cassandre recovery shard {args.start:05d}')
    pending_indices=[]
    try:
        with journal_path.open('a',encoding='utf8',buffering=1) as journal, (args.out/'technical.jsonl').open('a',encoding='utf8',buffering=1) as technical:
            for idx,row in rows:
                if idx in prior:continue
                progress.update(state='running',current_index=idx,current_id=row['id'],
                    current_rating=row['rating'],current_fen=row['puzzleFen'],updated_at=stamp())
                atomic(args.out/'status.json',progress)
                progress['attempted_this_run']+=1
                if not chess.Board(row['puzzleFen']).is_valid():
                    record={'index':idx,'id':row['id'],'puzzle_rating':row['rating'],
                            'solved':None,'invalid_source':'python-chess-invalid-FEN',
                            'moves':[],'completed_at':stamp()}
                else:
                    record=None
                    for attempt in range(1,args.max_technical_retries+1):
                        engine=None
                        try:
                            # Fresh Cassandre per attempt prevents stale XBoard searches contaminating the next puzzle.
                            engine,protocol=get_engine(args.exe)
                            success,moves=solve(engine,row,args.seconds)
                            record={'index':idx,'id':row['id'],'puzzle_rating':row['rating'],
                                    'solved':success,'moves':moves,'completed_at':stamp()}
                            break
                        except Exception as exc:
                            progress['technical_errors']+=1
                            event={'index':idx,'id':row['id'],'attempt':attempt,
                                   'error':repr(exc),'at':stamp(),'unscored':True}
                            technical.write(json.dumps(event,separators=(',',':'))+'\n')
                            technical.flush();os.fsync(technical.fileno())
                            print('TECHNICAL_RETRY',idx,row['id'],attempt,repr(exc),flush=True)
                        finally:
                            if engine:
                                try:engine.quit()
                                except Exception:
                                    try:engine.close()
                                    except Exception:pass
                    if record is None:
                        pending_indices.append(idx)
                        progress['technical_pending']=len(pending_indices)
                        progress.update(updated_at=stamp())
                        atomic(args.out/'status.json',progress)
                        reporter.update(progress)
                        print('PENDING_NEXT_RUN',idx,row['id'],flush=True)
                        continue
                journal.write(json.dumps(record,separators=(',',':'))+'\n')
                journal.flush();os.fsync(journal.fileno())
                prior[idx]=record
                progress['completed']+=1
                progress['solved']+=int(record.get('solved') is True)
                progress['failed']+=int(record.get('solved') is False)
                progress['invalid_source']+=int(bool(record.get('invalid_source')))
                progress.update(last=record,updated_at=stamp())
                atomic(args.out/'status.json',progress)
                reporter.update(progress)
                print('PUZZLE',idx+1,'ID',row['id'],'SOLVED' if record.get('solved') else 'FAILED_OR_INVALID',
                      'TOTAL',progress['completed'],'PENDING',progress['technical_pending'],flush=True)
        progress['state']='completed' if progress['completed']==len(rows) else 'retry_pending'
        progress['missing_indices']=sorted(set(expected)-set(prior))
        assert len(progress['missing_indices'])==len(rows)-len(prior)
    except Exception as exc:
        progress.update(state='error_paused',error=repr(exc),updated_at=stamp())
        traceback.print_exc()
        raise
    finally:
        progress['updated_at']=stamp()
        atomic(args.out/'status.json',progress)
        reporter.update(progress,final=True)
        print('FINAL_STATUS',json.dumps(progress),flush=True)

if __name__=='__main__':
    main()
