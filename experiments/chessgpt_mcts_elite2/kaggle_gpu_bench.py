#!/usr/bin/env python3
"""Run ChessGPT-MCTS on a CUDA GPU (e.g. a Kaggle T4 Notebook).

This is a benchmark launcher, not an Elo claim and not a replacement for
running actual 2+1 matches. Both policy and SF16 classical evaluator remain
the original models; GPU accelerates ONLY the ChessGPT policy.
Requires Kaggle: GPU enabled, Internet enabled, and available GPU quota.
Run from Kaggle notebook: !python kaggle_gpu_bench.py
"""
import os
import pathlib
import subprocess
import sys
import tarfile
import time
import urllib.request

HERE=pathlib.Path(__file__).resolve().parent
BRANCH="experiment/chessgpt-mcts-gpu-20261014"
REPO="https://raw.githubusercontent.com/TanTzeHerng/chess-transformers-tth/refs/heads/"
WEIGHT_URL="https://huggingface.co/adamkarvonen/chess_llms/resolve/main/stockfish_16layers_ckpt_with_optimizer.pt"
TORCH_FILE=HERE/"stockfish_16layers_ckpt_with_optimizer.pt"
SF_URL="https://github.com/official-stockfish/Stockfish/releases/download/sf_16/stockfish-ubuntu-x86-64-avx2.tar"

def download(url,path,minbytes):
    if path.is_file() and path.stat().st_size>=minbytes:
        print("EXISTS",path,path.stat().st_size,flush=True)
        return
    print("DOWNLOAD",path.name,url,flush=True)
    temp=path.with_name(path.name+".tmp")
    with urllib.request.urlopen(url,timeout=90) as res,temp.open("wb") as out:
        while True:
            chunk=res.read(4*1024*1024)
            if not chunk:break
            out.write(chunk)
    n=temp.stat().st_size
    if n<minbytes:raise RuntimeError("Too-short download %s: %d"%(path,n))
    temp.replace(path)

def setup():
    import torch
    print("PYTORCH",torch.__version__,flush=True)
    print("CUDA_AVAILABLE",torch.cuda.is_available(),flush=True)
    if not torch.cuda.is_available():
        raise RuntimeError("No GPU. Enable GPU accelerator in Kaggle notebook Settings.")
    print("GPU",torch.cuda.get_device_name(0),flush=True)
    for name in ("chessgpt_mcts_uci.py",):
        download(REPO+BRANCH+"/experiments/chessgpt_mcts_elite2/"+name,
                 HERE/name,12000)
    download(REPO+"experiment/chessgpt-maia1900-promptfix-20261013"+
             "/experiments/chessgpt_maia1900/cloud_match.py",HERE/"cloud_match.py",7000)
    download("https://raw.githubusercontent.com/adamkarvonen/train_ChessGPT/master/model.py",
             HERE/"model.py",10000)
    download(WEIGHT_URL,TORCH_FILE,610803879)
    if TORCH_FILE.stat().st_size!=610803879:raise RuntimeError("ChessGPT checkpoint size mismatch")
    sf=HERE/"sf16_gpu_bench"
    if not sf.exists():
        tarpath=HERE/"stockfish16_linux.tar"
        download(SF_URL,tarpath,40000000)
        sf.mkdir()
        with tarfile.open(tarpath) as tf:
            tf.extractall(sf,filter="data")
    candidates=[p for p in sf.rglob("*") if p.is_file() and
                (p.name.startswith("stockfish-ubuntu") or p.name.startswith("stockfish-linux"))]
    if not candidates:
        raise RuntimeError("Stockfish 16 Linux binary not found in official archive")
    binary=max(candidates,key=lambda p:p.stat().st_size)
    binary.chmod(0o755)
    print("CLASSICAL_VALUE_EXECUTABLE",binary,flush=True)
    return binary

def main():
    import importlib.util
    if importlib.util.find_spec("chess") is None:
        subprocess.run([sys.executable,"-m","pip","install","-q","python-chess"],check=True)
    binary=setup()
    import chess
    import chess.engine
    exe=[sys.executable,"-u",str(HERE/"chessgpt_mcts_uci.py")]
    env=os.environ.copy()
    env["STOCKFISH16_BIN"]=str(binary)
    positions={"startpos":chess.Board(),"20ply":chess.Board()}
    for san in "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Nb8 d4 Nbd7".split():
        positions["20ply"].push_san(san)
    print("BENCHMARK_STARTED",flush=True)
    with chess.engine.SimpleEngine.popen_uci(exe,timeout=240,env=env,cwd=str(HERE)) as eng:
        eng.configure({"PolicyDevice":"cuda","Int8Policy":False,"PolicyMass":95,"SANBatch":32})
        # First GPU inference includes CUDA context/JIT kernel startup. Do not
        # compare that first one to warmed CUDA or CPU searches.
        warm=eng.play(chess.Board(),chess.engine.Limit(time=2),game=object())
        print("WARMUP_COMPLETE",warm.move,flush=True)
        for name,position in positions.items():
            for sec in (2.5,3.0,5.0):
                begin=time.monotonic()
                response=eng.play(position,chess.engine.Limit(time=sec),
                                  info=chess.engine.INFO_ALL,game=object())
                elapsed=time.monotonic()-begin
                print("CUDA_SIMULATIONS",name,sec,
                      "count",response.info.get("nodes"),
                      "elapsed",round(elapsed,3),
                      "move",response.move,
                      "details",response.info.get("string"),flush=True)
                assert response.move in position.legal_moves
    print("GPU_BENCH_COMPLETE",flush=True)

if __name__=="__main__":
    main()
