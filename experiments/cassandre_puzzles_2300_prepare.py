#!/usr/bin/env python3
"""Streaming reproduction of the September 2026 Chess.com puzzle snapshot, rating >=2300."""
import argparse
import hashlib
import ijson
import json
import random
import urllib.request
import zipfile
from pathlib import Path
import chess

SOURCE="https://github.com/IchNukeDichWeg/chess.com-puzzles/releases/download/v2/puzzles-json.zip"
SOURCE_SHA="e1723a55970e9dbf36b3a2981183b571b979efe5ee7a83a500f7526813d7f406"
ALPHABET='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!?{~}(^)[_]@#$'
EXPECTED=33460

def decode(row):
    board=chess.Board(row['initialFen'])
    encoded=row['tcnMoveList']
    if len(encoded)%2:
        raise ValueError("Odd-length TCN for "+str(row['id']))
    moves=[]
    puzzle_fen=None
    for i in range(0,len(encoded),2):
        a=ALPHABET.index(encoded[i]); b=ALPHABET.index(encoded[i+1])
        promo=''
        if b>63:
            promo='qnrbk'[(b-64)//3]
            b=a+(-8 if a<16 else 8)+(b-64)%3-1
        move=chess.Move.from_uci(chess.square_name(a)+chess.square_name(b)+promo)
        if move not in board.legal_moves:
            raise ValueError(f"Illegal source move {row['id']} {move}")
        moves.append(move.uci()); board.push(move)
        if i==0:
            puzzle_fen=board.fen(en_passant='fen')
            if ('white' if board.turn else 'black')!=row['colorOfUser']:
                raise ValueError("Bad solver color "+str(row['id']))
    if len(moves)<2:
        raise ValueError("No solution "+str(row['id']))
    return dict(id=row['id'],rating=row['rating'],initialFen=row['initialFen'],
                puzzleFen=puzzle_fen,setupMoveUci=moves[0],
                solutionUci=moves[1:],colorOfUser=row['colorOfUser'])

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('dataset'))
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    archive=args.output/'source-puzzles-json.zip'
    if not archive.exists():
        with urllib.request.urlopen(urllib.request.Request(SOURCE,headers={'User-Agent':'Cassandre-Chess-Experiment'}),timeout=180) as src, archive.open('wb') as target:
            while True:
                block=src.read(1048576)
                if not block: break
                target.write(block)
    with archive.open('rb') as stream:
        sha=hashlib.file_digest(stream,'sha256').hexdigest()
    if sha!=SOURCE_SHA: raise RuntimeError(f"Dataset digest differs {sha}")
    rows=[]
    with zipfile.ZipFile(archive) as z, z.open('puzzles.json') as stream:
        for key,row in ijson.kvitems(stream,'',use_float=True):
            if row['rating']>=2300:
                rows.append(decode(row))
    if len(rows)!=EXPECTED or len({row['id'] for row in rows})!=len(rows):
        raise RuntimeError(f"Expected {EXPECTED} distinct puzzles, got {len(rows)}")
    rows.sort(key=lambda r:(r['rating'],r['id']))
    random.Random(20261021).shuffle(rows)
    dest=args.output/'puzzles_2300.jsonl'
    with dest.open('w',encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row,separators=(',',':'))+'\n')
    with dest.open('rb') as stream: prepared_sha=hashlib.file_digest(stream,'sha256').hexdigest()
    meta={'source':SOURCE,'source_sha256':sha,'min_puzzle_rating':2300,
          'total':len(rows),'dataset_sha256':prepared_sha,'shuffle_seed':20261021,
          'method':'First source move applied, all remaining recorded solver moves evaluated',
          'matching_old_2500plus_subset':19492}
    (args.output/'manifest.json').write_text(json.dumps(meta,indent=2))
    print('PREPARED',json.dumps(meta),flush=True)

if __name__=='__main__':
    main()
