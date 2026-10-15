import chess
import chess.pgn
from pathlib import Path
from collections import Counter
import json

files=sorted(Path('downloaded').rglob('games.pgn'))
assert len(files)==20, len(files)
games={}
score=Counter()
for file in files:
    n=0
    with file.open() as stream:
        while (game:=chess.pgn.read_game(stream)) is not None:
            n+=1
            assert not game.errors, game.errors
            h=game.headers
            rnd=int(h['Round'])
            assert rnd not in games and 1<=rnd<=100
            assert h['HashWhiteMB']==h['HashBlackMB']=='1'
            assert h['Threads']=='1024' and h['Depth']=='1' and h['SyzygyProbeLimit']=='5'
            assert h['HashClearedBeforeEachMove']=='True'
            assert h['InitialPosition']=='standard chess starting position'
            assert 'FEN' not in h and 'SetUp' not in h
            white,black=(1,256) if rnd%2 else (256,1)
            assert h['MultiPVWhite']==str(white) and h['MultiPVBlack']==str(black)
            assert h['White']==f'Stockfish 17.1 MultiPV {white}'
            assert h['Black']==f'Stockfish 17.1 MultiPV {black}'
            board=chess.Board()
            for move in game.mainline_moves():
                assert not board.is_game_over(claim_draw=True),rnd
                assert move in board.legal_moves,(rnd,move)
                board.push(move)
            outcome=board.outcome(claim_draw=True)
            assert outcome is not None,rnd
            assert outcome.result()==h['Result'],rnd
            assert outcome.termination.name==h['Termination'],rnd
            if outcome.winner is None: score['draws']+=1
            elif outcome.winner==chess.WHITE: score[f'multipv{white}_wins']+=1
            else: score[f'multipv{black}_wins']+=1
            games[rnd]=game
    assert n==5,(file,n)
assert sorted(games)==list(range(1,101))
output=Path('verified')
output.mkdir(exist_ok=True)
with (output/'verified_100_games.pgn').open('w') as stream:
    for rnd in range(1,101):
        games[rnd].accept(chess.pgn.FileExporter(stream))
        stream.write('\n')
report={'run_id':38665122125,'verified_games':100,'score':dict(score)}
(output/'audit_report.json').write_text(json.dumps(report,indent=2))
print('AUDIT_SUCCESS',json.dumps(report),flush=True)
