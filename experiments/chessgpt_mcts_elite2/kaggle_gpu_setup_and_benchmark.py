#!/usr/bin/env python3
"""Run the accelerated ChessGPT MCTS engine on a Kaggle/Colab CUDA GPU.

No local PC compute needed. Public, original ChessGPT model and Stockfish 16
are downloaded once. A 200-real-simulation benchmark and 2.5s blitz benchmark
are executed; neither manufactures nodes. A failure remains a failure.

Usage (in a CUDA notebook terminal/cell):
  !python kaggle_gpu_setup_and_benchmark.py
You must enable Internet and a GPU in the notebook's session settings.
"""
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ.setdefault('CHESSGPT_DEVICE','cuda')
os.environ.setdefault('CHESSGPT_TORCH_THREADS','4')
os.environ.setdefault('CHESSGPT_LEAF_BATCH','8')
os.environ.setdefault('CHESSGPT_SAN_BATCH','96')
MODEL_URL='https://huggingface.co/adamkarvonen/chess_llms/resolve/main/stockfish_16layers_ckpt_with_optimizer.pt'
SF16_URL='https://github.com/official-stockfish/Stockfish/releases/download/sf_16/stockfish-ubuntu-x86-64-avx2.tar'
SOURCE_BASE='https://raw.githubusercontent.com/TanTzeHerng/chess-transformers-tth/refs/heads/experiment/chessgpt-mcts-gpu-batch-20261014/experiments/chessgpt_mcts_elite2/'
NEEDED=['chessgpt_mcts_uci.py','accelerated_mcts.py','cloud_match.py','model.py']


def fetch(url,destination,min_length=None,expected_length=None):
    destination=Path(destination)
    if destination.is_file() and (expected_length is None or destination.stat().st_size==expected_length):
        print('REUSE',destination,destination.stat().st_size,flush=True)
        return destination
    destination.parent.mkdir(parents=True,exist_ok=True)
    print('DOWNLOAD',url,flush=True)
    tmp=destination.with_suffix(destination.suffix+'.part')
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'ChessGPT-batched-benchmark'}),timeout=90) as src, tmp.open('wb') as out:
        shutil.copyfileobj(src,out,1024*1024)
    if min_length is not None and tmp.stat().st_size<min_length:
        raise RuntimeError('Unexpected truncated file '+str(destination))
    if expected_length is not None and tmp.stat().st_size!=expected_length:
        raise RuntimeError('Wrong bytes '+str(destination)+' '+str(tmp.stat().st_size))
    tmp.replace(destination)
    return destination


def install():
    for name in NEEDED:
        path=ROOT/name
        if path.is_file():continue
        if name=='cloud_match.py':
            url='https://raw.githubusercontent.com/TanTzeHerng/chess-transformers-tth/refs/heads/experiment/chessgpt-maia1900-promptfix-20261013/experiments/chessgpt_maia1900/cloud_match.py'
        elif name=='model.py':
            url='https://raw.githubusercontent.com/adamkarvonen/train_ChessGPT/master/model.py'
        else:
            url=SOURCE_BASE+name
        fetch(url,path,min_length=2000)
    try:import chess
    except ImportError:subprocess.run([sys.executable,'-m','pip','install','python-chess'],check=True)
    fetch(MODEL_URL,ROOT/'stockfish_16layers_ckpt_with_optimizer.pt',expected_length=610803879)
    target=ROOT/'stockfish16'/'stockfish-windows-x64-avx2.exe'
    if not target.is_file():
        package=fetch(SF16_URL,ROOT/'stockfish16_linux_avx2.tar',min_length=30_000_000)
        archive=ROOT/'stockfish16_unpack'
        archive.mkdir(exist_ok=True)
        with tarfile.open(package) as tar:
            # Only extract the one genuine, non-NNUE Stockfish 16 executable.
            candidates=[m for m in tar.getmembers() if m.isfile() and
                'stockfish' in Path(m.name).name.lower() and
                not Path(m.name).suffix and 'avx2' in m.name.lower()]
            if not candidates:
                candidates=[m for m in tar.getmembers() if m.isfile() and
                    Path(m.name).name.lower().startswith('stockfish') and
                    not Path(m.name).suffix and m.size>1_000_000]
            if not candidates:raise RuntimeError('No executable in genuine Stockfish 16 package')
            chosen=candidates[0]
            target.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(chosen) as stream, target.open('wb') as out:
                shutil.copyfileobj(stream,out)
        target.chmod(0o755)
    print('SF16_BINARY',target,target.stat().st_size,flush=True)


def benchmark():
    import chess
    import chess.engine
    import torch
    print('TORCH',torch.__version__,'CUDA',torch.cuda.is_available(),flush=True)
    if not torch.cuda.is_available():
        raise RuntimeError('No CUDA GPU! Enable a Kaggle/Colab GPU first; do not claim GPU speed from CPU.')
    print('GPU',torch.cuda.get_device_name(),flush=True)
    environment=os.environ.copy()
    environment['CHESSGPT_DEVICE']='cuda'
    exe=[sys.executable,'-u',str(ROOT/'chessgpt_mcts_uci.py')]
    positions=[('startpos',chess.Board())]
    mid=chess.Board()
    for san in ['e4','e5','Nf3','Nc6','Bb5','a6','Ba4','Nf6',
                'O-O','Be7','Re1','b5','Bb3','d6','c3','O-O',
                'h3','Nb8','d4','Nbd7']:
        mid.push_san(san)
    positions.append(('20ply',mid))
    with chess.engine.SimpleEngine.popen_uci(exe,timeout=180,env=environment,cwd=str(ROOT)) as engine:
        engine.configure({'Accelerator':'cuda','TorchThreads':4,'LeafBatch':8,'SANBatchSize':96})
        print('UCI_ENGINE',engine.id,flush=True)
        if 'BatchedGPU' not in engine.id.get('name',''):
            raise RuntimeError('Wrong ChessGPT UCI engine')
        for title,board in positions:
            for seconds in (2.5,5.0):
                started=time.monotonic()
                result=engine.play(board,chess.engine.Limit(time=seconds),
                    info=chess.engine.INFO_ALL,game=object())
                took=time.monotonic()-started
                sims=result.info.get('nodes',0)
                print('GPU_TIMED',title,'requested_seconds',seconds,'wall',round(took,3),
                      'REAL_MCTS_SIMULATIONS',sims,'move',result.move,
                      'seldepth',result.info.get('seldepth'),flush=True)
                assert result.move in board.legal_moves
                assert sims>0
            started=time.monotonic()
            exact=engine.play(board,chess.engine.Limit(nodes=200),
                info=chess.engine.INFO_ALL,game=object())
            took=time.monotonic()-started
            print('GPU_GO_NODES_200',title,'actually_completed',exact.info.get('nodes'),
                'wall',round(took,3),'move',exact.move,flush=True)
            assert exact.info.get('nodes')==200,'Exact node benchmark must reach 200 genuine backups'
    print('GPU_ACCELERATION_BENCHMARK_COMPLETE',flush=True)


if __name__=='__main__':
    install()
    benchmark()
