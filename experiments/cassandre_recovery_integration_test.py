#!/usr/bin/env python3
"""Deterministic recovery test: a technical failure must not lose or misgrade any puzzle."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import chess
import cassandre_puzzles_2300_recovery_worker as worker

class FakeHash:
    def hexdigest(self):
        return '17d69f321244ab1f6530341b9207fe32fd415e80e017e6c33a827c84755839b7'

class FakeEngine:
    def quit(self):pass
    def close(self):pass

def run_one(argv):
    saved=sys.argv
    sys.argv=['worker']+argv
    try:worker.main()
    finally:sys.argv=saved

def main():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        exe=root/'dummy-cassandre.exe'
        exe.write_bytes(b'not-executed')
        start=chess.Board()
        start.push_uci('e2e4')
        puzzle=start.fen(en_passant='fen')
        dataset=root/'two.jsonl'
        rows=[{'id':999000+i,'rating':2500,'initialFen':chess.STARTING_FEN,
               'setupMoveUci':'e2e4','puzzleFen':puzzle,'solutionUci':['e7e5']}
              for i in range(2)]
        dataset.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        worker.hashlib.file_digest=lambda stream,alg:FakeHash()
        worker.get_engine=lambda _exe:(FakeEngine(),'fake')
        calls=[]
        def first(engine,row,time_per_move):
            calls.append(row['id'])
            if row['id']==999000:raise RuntimeError('simulated dead XBoard process')
            return True,[{'expected':'e7e5','chosen':'e7e5','correct':True}]
        worker.solve=first
        out1=root/'first'
        base=['--exe',str(exe),'--dataset',str(dataset),'--start','0','--count','2','--seconds','30']
        run_one(base+['--out',str(out1),'--max-technical-retries','2'])
        status=json.loads((out1/'status.json').read_text())
        assert status['state']=='retry_pending',status
        assert status['completed']==1 and status['solved']==1 and status['failed']==0
        assert status['missing_indices']==[0]
        assert status['attempted_this_run']==2
        assert calls==[999000,999000,999001],calls
        assert len((out1/'technical.jsonl').read_text().splitlines())==2
        def second(engine,row,time_per_move):
            calls.append(row['id'])
            return True,[{'expected':'e7e5','chosen':'e7e5','correct':True}]
        worker.solve=second
        out2=root/'second'
        run_one(base+['--out',str(out2),'--resume',str(out1/'results.jsonl')])
        again=json.loads((out2/'status.json').read_text())
        assert again['state']=='completed',again
        assert again['completed']==2 and again['solved']==2 and again['failed']==0
        assert again['recovered_before_run']==1 and again['attempted_this_run']==1
        assert calls==[999000,999000,999001,999000],calls
        records=[json.loads(x) for x in (out2/'results.jsonl').read_text().splitlines()]
        assert len(records)==2 and {r['index'] for r in records}=={0,1}
        print('RECOVERY_TEST_PASSED no unscored counted wrong, later puzzles continue, failed indexes resume, no already-scored repeats')

if __name__=='__main__':
    main()
