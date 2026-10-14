"""Independent post-game Stockfish 17.1 analysis of actual ChessGPT-Elite drawn PGNs.
No chess engine plays new games here. Score is from WHITE's perspective.
Repetition histories are deliberately removed before engine analysis."""
import io, json, csv, pathlib
import chess, chess.pgn, chess.engine

ROOT=pathlib.Path('.')
RUNS=[('T1',ROOT/'t1'/'ChessGPT_MCTS_vs_Elite_v2.pgn',ROOT/'t1'/'score.csv'),
      ('T8',ROOT/'t8'/'ChessGPT_MCTS_vs_Elite_v2.pgn',ROOT/'t8'/'score.csv')]
STOCKFISH=ROOT/'stockfish_binary'
assert STOCKFISH.exists(),STOCKFISH

def inspect(engine,board,label,seconds=6):
    # Discard repetition history so a legitimate repetition does not force an
    # automatic score of 0 for a tactically won or lost POSITION.
    fresh=chess.Board(board.fen())
    if fresh.is_game_over(claim_draw=False):
        return {'label':label,'is_game_over':True,'fen':fresh.fen(),
                'outcome':str(fresh.outcome(claim_draw=False))}
    r=engine.analyse(fresh,chess.engine.Limit(time=seconds),info=chess.engine.INFO_ALL)
    score=r['score'].pov(chess.WHITE)
    val=score.score(mate_score=100000)
    mate=score.mate()
    pv=r.get('pv',[])
    copy=fresh.copy()
    sans=[]
    for move in pv[:8]:
        if move not in copy.legal_moves:break
        sans.append(copy.san(move)); copy.push(move)
    return {'label':label,'fen':fresh.fen(),'turn':'white' if fresh.turn else 'black',
            'stockfish17_1_cp_white':val,'mate_white':mate,'depth':r.get('depth'),
            'nodes':r.get('nodes'),'sample_pv_san':' '.join(sans),
            'material':{ch:len(fresh.pieces(t,c)) for ch,t,c in
                         [('WQ',chess.QUEEN,chess.WHITE),('BQ',chess.QUEEN,chess.BLACK),
                          ('WR',chess.ROOK,chess.WHITE),('BR',chess.ROOK,chess.BLACK),
                          ('WB',chess.BISHOP,chess.WHITE),('BB',chess.BISHOP,chess.BLACK),
                          ('WN',chess.KNIGHT,chess.WHITE),('BN',chess.KNIGHT,chess.BLACK),
                          ('WP',chess.PAWN,chess.WHITE),('BP',chess.PAWN,chess.BLACK)]}}
results=[]
with chess.engine.SimpleEngine.popen_uci(str(STOCKFISH.resolve()),timeout=90) as eng:
    print('STOCKFISH_VERSION',eng.id,flush=True)
    eng.configure({'Threads':2,'Hash':192})
    for run,pgn_path,csv_path in RUNS:
        csvrows=list(csv.DictReader(csv_path.open(encoding='utf-8')))
        file=pgn_path.open(encoding='utf-8')
        n=0
        while (game:=chess.pgn.read_game(file)) is not None:
            n+=1
            if game.headers.get('Result')!='1/2-1/2':continue
            moves=list(game.mainline_moves())
            assert len(moves)==int(csvrows[n-1]['plies'])
            b=game.board()
            positions=[b.copy(stack=True)]
            for mv in moves:
                assert mv in b.legal_moves,(run,n,mv)
                b.push(mv)
                positions.append(b.copy(stack=True))
            after=positions[-1]
            termination=game.headers['Termination']
            if not ((after.is_stalemate() and termination=='STALEMATE') or
                    (after.can_claim_threefold_repetition() and termination=='THREEFOLD_REPETITION')):
                print('WARNING_DRAW_HEADER_NOT_REPRODUCED',run,n,termination,
                      'fen',after.fen(),'can_claim_threefold',after.can_claim_threefold_repetition(),
                      'is_repetition3',after.is_repetition(3),
                      'outcome',after.outcome(claim_draw=True),flush=True)
            row={'run':run,'game':n,'white':game.headers['White'],
                 'black':game.headers['Black'],
                 'termination':termination,'plies':len(moves),
                 'last_move':positions[-2].san(moves[-1]),
                 'threefold_after':after.is_repetition(3),
                 'threefold_claim_available':after.can_claim_threefold_repetition(),
                 'stalemate_after':after.is_stalemate(),
                 'outcome_claim_draw':str(after.outcome(claim_draw=True)),
                 'evals':[]}
            # Compare the near-final positions at depth using unmodified Stockfish NNUE
            # and no repetition adjudication. The position BEFORE final move
            # is especially important to judge whether the drawing continuation
            # threw away a significant advantage.
            choices=[('eight_plies_before',max(0,len(moves)-8)),
                     ('immediately_before_final_move',len(moves)-1),
                     ('final_position',len(moves))]
            for label,index in choices:
                if index==len(moves) and after.is_stalemate():
                    q={'label':label,'outcome':'stalemate','fen':after.fen()}
                else:q=inspect(eng,positions[index],label,seconds=6)
                row['evals'].append(q)
            results.append(row)
            print('DRAW_POSITION_EVAL',json.dumps(row),flush=True)
        file.close()
        assert n==20,(run,n)
(ROOT/'draw_eval_results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print('ANALYSIS_COMPLETE',len(results),'DRAWN_GAMES',flush=True)
