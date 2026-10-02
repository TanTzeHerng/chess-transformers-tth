#!/usr/bin/env python3
import json, os, subprocess, sys, threading, time, urllib.request
from pathlib import Path
import chess

NAME = "YOLOChess-NNUE 2.0"
AUTHOR = "OpenAI + jrahn/YOLOChess policy + Stockfish NNUE"
SPACE = "https://jrahn-yolochess.hf.space"

_out_lock = threading.Lock()
def out(s):
    with _out_lock:
        print(s, flush=True)

class OnlinePolicy:
    def __init__(self):
        self.enabled = True
        self.timeout_ms = 1200
        self.cache = {}
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.cache.clear()

    def best_move(self, board, budget_ms=None):
        if not self.enabled:
            return None
        fen = board.fen()
        with self.lock:
            u = self.cache.get(fen)
        if u:
            try:
                m = chess.Move.from_uci(u)
                if m in board.legal_moves:
                    return m
            except Exception:
                pass

        timeout = self.timeout_ms / 1000.0
        if budget_ms is not None:
            timeout = min(timeout, max(0.05, budget_ms / 1000.0))
        deadline = time.monotonic() + timeout
        try:
            body = json.dumps({"data": [fen, "", "UCI", 1]}).encode()
            req = urllib.request.Request(
                SPACE + "/call/btn_play", data=body, method="POST",
                headers={"Content-Type":"application/json","Accept":"application/json"})
            with urllib.request.urlopen(req, timeout=max(0.05, deadline-time.monotonic())) as r:
                obj = json.loads(r.read().decode())
            event_id = obj.get("event_id")
            if not event_id:
                return None

            req2 = urllib.request.Request(
                SPACE + "/call/btn_play/" + event_id,
                headers={"Accept":"text/event-stream"})
            payload = None
            with urllib.request.urlopen(req2, timeout=max(0.05, deadline-time.monotonic())) as r:
                while time.monotonic() < deadline:
                    raw = r.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8","replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        val = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if isinstance(val, list) and len(val) >= 2 and isinstance(val[1], str):
                        payload = val
                        break
            if payload is None:
                return None

            new_fen = payload[1]
            target4 = " ".join(new_fen.split()[:4])
            for mv in board.legal_moves:
                b = board.copy(stack=False)
                b.push(mv)
                if b.fen() == new_fen or " ".join(b.fen().split()[:4]) == target4:
                    with self.lock:
                        self.cache[fen] = mv.uci()
                    return mv
        except Exception as e:
            out("info string YOLOChess policy unavailable: " + str(e).replace("\n"," ")[:150])
        return None

def core_path():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    p = base / "YoloChess-NNUE-Core.exe"
    if p.exists():
        return p
    p = Path(__file__).resolve().parent / "YoloChess-NNUE-Core.exe"
    return p

class Engine:
    def __init__(self):
        self.board = chess.Board()
        self.policy = OnlinePolicy()
        self.write_lock = threading.Lock()
        self.search_lock = threading.Lock()
        self.search_thread = None
        self.stop_event = threading.Event()
        self.helper_started = False

        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.core = subprocess.Popen(
            [str(core_path())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, creationflags=flags)
        self._core_send("uci")
        self._core_read_until(lambda s: s == "uciok", 30)
        self._core_send("isready")
        self._core_read_until(lambda s: s == "readyok", 30)

    def _core_send(self, line):
        with self.write_lock:
            self.core.stdin.write(line + "\n")
            self.core.stdin.flush()

    def _core_read_until(self, pred, timeout):
        end = time.monotonic() + timeout
        lines=[]
        while time.monotonic() < end:
            line = self.core.stdout.readline()
            if not line:
                if self.core.poll() is not None:
                    raise RuntimeError("NNUE core exited")
                continue
            line=line.rstrip()
            lines.append(line)
            if pred(line):
                return lines
        raise TimeoutError("NNUE core timed out")

    def stop_current(self):
        t = self.search_thread
        if not t or not t.is_alive():
            return
        self.stop_event.set()
        with self.search_lock:
            started = self.helper_started
        if started:
            try:
                self._core_send("stop")
            except Exception:
                pass
        t.join(timeout=5.0)

    def parse_position(self, line):
        toks=line.split()
        try:
            if len(toks) >= 2 and toks[1] == "startpos":
                b=chess.Board()
                i=2
            elif len(toks) >= 3 and toks[1] == "fen":
                i=2
                fen=[]
                while i < len(toks) and toks[i] != "moves":
                    fen.append(toks[i]); i+=1
                b=chess.Board(" ".join(fen))
            else:
                return
            if i < len(toks) and toks[i] == "moves":
                i+=1
                while i < len(toks):
                    mv=chess.Move.from_uci(toks[i])
                    if mv not in b.legal_moves:
                        raise ValueError("illegal move " + toks[i])
                    b.push(mv); i+=1
            self.board=b
        except Exception as e:
            out("info string position parse error: " + str(e))

    def parse_go(self, line):
        toks=line.split()
        d={"searchmoves":[]}
        numeric={"depth","nodes","movetime","wtime","btime","winc","binc","movestogo","mate"}
        i=1
        while i<len(toks):
            t=toks[i]
            if t in numeric and i+1<len(toks):
                try:d[t]=int(toks[i+1])
                except:d[t]=0
                i+=2; continue
            if t in ("infinite","ponder"):
                d[t]=True; i+=1; continue
            if t=="searchmoves":
                i+=1
                while i<len(toks):
                    d["searchmoves"].append(toks[i]); i+=1
                break
            i+=1
        return d

    def policy_budget(self, gd):
        lim=self.policy.timeout_ms
        if "movetime" in gd:
            lim=min(lim,max(50,int(gd["movetime"]*0.25)))
        else:
            remain = gd.get("wtime" if self.board.turn else "btime")
            inc = gd.get("winc" if self.board.turn else "binc",0)
            if remain is not None:
                mtg=gd.get("movestogo",28) or 28
                est=max(100, int(remain/max(8,mtg)+0.8*inc))
                lim=min(lim,max(50,int(est*0.25)))
        return lim

    def adjusted_go(self, line, elapsed_ms):
        toks=line.split()
        result=[toks[0]]
        i=1
        side_time="wtime" if self.board.turn==chess.WHITE else "btime"
        while i<len(toks):
            t=toks[i]
            if t in {"depth","nodes","movetime","wtime","btime","winc","binc","movestogo","mate"} and i+1<len(toks):
                v=toks[i+1]
                try: iv=int(v)
                except: iv=0
                if t=="movetime":
                    iv=max(1,iv-elapsed_ms)
                elif t==side_time:
                    iv=max(1,iv-elapsed_ms)
                result += [t,str(iv)]
                i+=2
            else:
                result.append(t); i+=1
        return " ".join(result)

    def start_go(self, line):
        self.stop_current()
        self.stop_event=threading.Event()
        gd=self.parse_go(line)
        board=self.board.copy(stack=True)

        def worker():
            t0=time.monotonic()
            budget=self.policy_budget(gd)
            pm=self.policy.best_move(board,budget_ms=budget)
            elapsed=int((time.monotonic()-t0)*1000)
            if pm is not None:
                out(f"info string YOLOChess policy {pm.uci()} ({elapsed} ms)")
            else:
                out(f"info string YOLOChess policy unavailable/skipped ({elapsed} ms)")

            if self.stop_event.is_set():
                legal=list(board.legal_moves)
                out("bestmove " + ((pm.uci() if pm in legal else legal[0].uci()) if legal else "0000"))
                return

            # Preserve pure YOLOChess behavior for exactly one requested node.
            if gd.get("nodes") == 1:
                legal=list(board.legal_moves)
                out("bestmove " + ((pm.uci() if pm in legal else legal[0].uci()) if legal else "0000"))
                return

            try:
                self._core_send("setoption name RootPolicyMove value " + (pm.uci() if pm else ""))
                go2=self.adjusted_go(line,elapsed)
                with self.search_lock:
                    self.helper_started=True
                self._core_send(go2)
                while True:
                    raw=self.core.stdout.readline()
                    if not raw:
                        if self.core.poll() is not None:
                            raise RuntimeError("NNUE core exited during search")
                        continue
                    s=raw.rstrip()
                    if s.startswith("info ") or s.startswith("bestmove "):
                        out(s)
                    if s.startswith("bestmove "):
                        break
            except Exception as e:
                out("info string NNUE core error: " + str(e).replace("\n"," ")[:180])
                legal=list(board.legal_moves)
                out("bestmove " + ((pm.uci() if pm in legal else legal[0].uci()) if legal else "0000"))
            finally:
                with self.search_lock:
                    self.helper_started=False

        self.search_thread=threading.Thread(target=worker,daemon=True)
        self.search_thread.start()

    def setoption(self,line):
        low=line.lower()
        if "name onlinepolicy" in low and " value " in low:
            v=line.rsplit(" value ",1)[1].strip().lower()
            self.policy.enabled=v not in ("false","0","off")
            return
        if "name policytimeout" in low and " value " in low:
            try:self.policy.timeout_ms=max(50,min(5000,int(line.rsplit(" value ",1)[1])))
            except:pass
            return
        if "name hash" in low or "name clear hash" in low:
            self._core_send(line)

    def loop(self):
        for raw in sys.stdin:
            line=raw.strip()
            if not line: continue
            cmd=line.split()[0].lower()
            if cmd=="uci":
                out("id name " + NAME)
                out("id author " + AUTHOR)
                out("option name Hash type spin default 128 min 1 max 2048")
                out("option name Clear Hash type button")
                out("option name OnlinePolicy type check default true")
                out("option name PolicyTimeout type spin default 1200 min 50 max 5000")
                out("uciok")
            elif cmd=="isready":
                out("readyok")
            elif cmd=="ucinewgame":
                self.stop_current()
                self.policy.clear()
                self.board=chess.Board()
                self._core_send("ucinewgame")
            elif cmd=="position":
                self.stop_current()
                self.parse_position(line)
                self._core_send(line)
            elif cmd=="go":
                self.start_go(line)
            elif cmd=="stop":
                self.stop_current()
            elif cmd=="setoption":
                self.setoption(line)
            elif cmd=="ponderhit":
                pass
            elif cmd=="quit":
                self.stop_current()
                try:self._core_send("quit")
                except:pass
                try:self.core.wait(timeout=3)
                except: self.core.kill()
                break

if __name__=="__main__":
    Engine().loop()
