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

def get_engine(exe):
    errors=[]
    for protocol in ('xboard','uci'):
        try:
            factory=chess.engine.SimpleEngine.popen_uci if protocol=='uci' else chess.engine.SimpleEngine.popen_xboard
            engine=factory(str(exe),cwd=str(exe.parent),timeout=65)
            engine.ping()
            print('ENGINE_PROTOCOL',protocol,'ID',engine.id,'OPTIONS',list(engine.options),flush=True)
            return engine,protocol
        except Exception as e:
            print('PROTOCOL_FAILED',protocol,repr(e),flush=True)
            errors.append(repr(e))
    raise RuntimeError('Cassandre UCI and XBoard both failed: '+' / '.join(errors))

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
        if final: data['conclusion']='success' if progress['state']=='completed' else 'failure'
        self.send(data)

def main():
    pa=argparse.ArgumentParser()
    pa.add_argument('--exe',type=Path,required=True)
    pa.add_argument('--dataset',type=Path,required=True)
    pa.add_argument('--out',type=Path,required=True)
    pa.add_argument('--start',type=int,default=0)
    pa.add_argument('--count',type=int,default=150)
    pa.add_argument('--seconds',type=float,default=30.0)
    pa.add_argument('--pilot',action='store_true')
    args=pa.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    rows=[]
    with args.dataset.open(encoding='utf8') as f:
        for idx,line in enumerate(f):
            if idx>=args.start+args.count:break
            if idx>=args.start: rows.append((idx,json.loads(line)))
    if not rows:raise RuntimeError('Empty assigned puzzle shard')
    progress={'state':'starting','engine':'Cassandre 0.24','seconds_per_move':args.seconds,
              'start_index':args.start,'assigned':len(rows),'completed':0,
              'solved':0,'failed':0,'technical_errors':0,'updated_at':stamp(),
              'current_id':None,'last':None}
    atomic(args.out/'status.json',progress)
    with args.exe.open('rb') as f:
        exe_sha=hashlib.file_digest(f,'sha256').hexdigest()
    expected='17d69f321244ab1f6530341b9207fe32fd415e80e017e6c33a827c84755839b7'
    if exe_sha!=expected:raise RuntimeError('Wrong Cassandre binary SHA '+exe_sha)
    print('CASSANDRE_SHA256',exe_sha,flush=True)
    reporter=Reporter(f'Cassandre shard {args.start:05d}')
    engine=None
    try:
        engine,protocol=get_engine(args.exe)
        progress['protocol']=protocol
        with (args.out/'results.jsonl').open('a',encoding='utf8',buffering=1) as journal:
            for idx,row in rows:
                progress.update(state='running',current_index=idx,current_id=row['id'],
                    current_rating=row['rating'],current_fen=row['puzzleFen'],updated_at=stamp())
                atomic(args.out/'status.json',progress)
                attempt=0
                while True:
                    try:
                        success,moves=solve(engine,row,args.seconds)
                        break
                    except Exception as e:
                        attempt+=1
                        progress['technical_errors']+=1
                        print('TECHNICAL_RETRY',idx,row['id'],attempt,repr(e),flush=True)
                        if attempt>=3:raise RuntimeError('Unscored puzzle paused after 3 technical errors') from e
                        try:engine.close()
                        except Exception:pass
                        engine,protocol=get_engine(args.exe)
                record={'index':idx,'id':row['id'],'puzzle_rating':row['rating'],
                        'solved':success,'moves':moves,'completed_at':stamp()}
                journal.write(json.dumps(record,separators=(',',':'))+'\n')
                journal.flush();os.fsync(journal.fileno())
                progress['completed']+=1
                progress['solved']+=int(success)
                progress['failed']+=int(not success)
                progress.update(last=record,updated_at=stamp())
                atomic(args.out/'status.json',progress)
                reporter.update(progress)
                print('PUZZLE',idx+1,'ID',row['id'],'rating',row['rating'],
                    'SOLVED' if success else 'FAILED','moves',len(moves),
                    'TOTAL',progress['completed'],'W',progress['solved'],
                    'L',progress['failed'],'protocol',protocol,flush=True)
        progress['state']='completed'
    except Exception as e:
        progress.update(state='error_paused',error=repr(e),updated_at=stamp())
        traceback.print_exc()
        raise
    finally:
        progress['updated_at']=stamp()
        atomic(args.out/'status.json',progress)
        reporter.update(progress,final=True)
        if engine:
            try:engine.quit()
            except Exception:
                try:engine.close()
                except Exception:pass
        print('FINAL_STATUS',json.dumps(progress),flush=True)

if __name__=='__main__':
    main()
