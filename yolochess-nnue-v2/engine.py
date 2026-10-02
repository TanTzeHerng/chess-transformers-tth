#!/usr/bin/env python3
import sys, time, threading, urllib.request, urllib.error, json, subprocess, os\nfrom collections import OrderedDict
from dataclasses import dataclass
import chess

NAME = "YoloChess-NNUE-UCI 2.0"
AUTHOR = "OpenAI; YOLOChess policy + Stockfish NNUE evaluator"
DEFAULT_SPACE = "https://jrahn-yolochess.hf.space"

INF = 10**9
MATE = 100000
PIECE_VAL = {
    chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
    chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0
}

def uci_out(s):
    print(s, flush=True)

class OnlinePolicy:
    def __init__(self):
        self.url = DEFAULT_SPACE
        self.timeout = 12.0
        self.enabled = True
        self.cache = {}
        self.lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.cache.clear()

    def best_move(self, board, deadline=None):
        if not self.enabled:
            return None
        fen = board.fen()
        with self.lock:
            if fen in self.cache:
                u = self.cache[fen]
                mv = chess.Move.from_uci(u)
                return mv if mv in board.legal_moves else None
        try:
            endpoint = self.url.rstrip("/")
            body = json.dumps({"data": [fen, "", "UCI", 1]}).encode("utf-8")
            req = urllib.request.Request(
                endpoint + "/call/btn_play",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            def net_timeout():
                if deadline is None:
                    return self.timeout
                remaining = deadline - time.monotonic()
                if remaining <= 0.03:
                    raise TimeoutError("policy time budget exhausted")
                return max(0.03, min(self.timeout, remaining))

            with urllib.request.urlopen(req, timeout=net_timeout()) as r:
                obj = json.loads(r.read().decode("utf-8"))
            event_id = obj.get("event_id")
            if not event_id:
                raise RuntimeError("Gradio returned no event_id")
            req2 = urllib.request.Request(
                endpoint + "/call/btn_play/" + event_id,
                headers={"Accept": "text/event-stream"},
            )
            payload = None
            with urllib.request.urlopen(req2, timeout=net_timeout()) as r:
                event_deadline = (time.monotonic() + self.timeout) if deadline is None else deadline
                while time.monotonic() < event_deadline:
                    raw = r.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        txt = line[5:].strip()
                        try:
                            val = json.loads(txt)
                        except Exception:
                            continue
                        if isinstance(val, list) and len(val) >= 2 and isinstance(val[1], str):
                            payload = val
                            break
            if payload is None:
                raise RuntimeError("No result payload from Gradio")
            new_fen = payload[1]
            for mv in board.legal_moves:
                b = board.copy(stack=False)
                b.push(mv)
                if b.fen() == new_fen:
                    with self.lock:
                        self.cache[fen] = mv.uci()
                    return mv
            # Be tolerant of halfmove/fullmove or en-passant formatting differences.
            target4 = " ".join(new_fen.split()[:4])
            for mv in board.legal_moves:
                b = board.copy(stack=False)
                b.push(mv)
                if " ".join(b.fen().split()[:4]) == target4:
                    with self.lock:
                        self.cache[fen] = mv.uci()
                    return mv
            raise RuntimeError("Could not infer YOLOChess move from returned FEN")
        except Exception as e:
            uci_out("info string YOLOChess policy unavailable: " + str(e).replace("\n", " ")[:180])
            return None


class StockfishNNUE:
    """Persistent evaluator bridge to a patched Stockfish binary.

    The helper exposes only 'evalscore': no Stockfish search is ever invoked.
    Scores are returned in centipawns from White's perspective and converted
    to side-to-move perspective for negamax.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.cache = OrderedDict()
        self.cache_limit = 250000
        self.proc = None
        self.path = self._find_helper()
        self._start()

    def _find_helper(self):
        roots = []
        if hasattr(sys, "_MEIPASS"):
            roots.append(getattr(sys, "_MEIPASS"))
        roots += [
            os.path.dirname(os.path.abspath(sys.executable)),
            os.path.dirname(os.path.abspath(sys.argv[0])),
            os.getcwd(),
        ]
        for root in roots:
            p = os.path.join(root, "sf_eval.exe")
            if os.path.isfile(p):
                return p
        raise FileNotFoundError("embedded Stockfish NNUE evaluator helper not found")

    def _start(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=flags,
        )
        self._send("uci")
        self._read_until(lambda x: x.strip() == "uciok", 10.0)
        self._send("isready")
        self._read_until(lambda x: x.strip() == "readyok", 10.0)

    def _send(self, cmd):
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("NNUE evaluator helper is not running")
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _read_until(self, predicate, timeout):
        # The helper is local and normally responds immediately. A timeout is
        # retained as a defensive guard; readline is expected to stay hot.
        end = time.monotonic() + timeout
        lines = []
        while time.monotonic() < end:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise RuntimeError("NNUE evaluator helper exited")
                continue
            line = line.rstrip("\r\n")
            lines.append(line)
            if predicate(line):
                return line, lines
        raise TimeoutError("NNUE evaluator helper timed out")

    def clear(self):
        with self.lock:
            self.cache.clear()

    def evaluate_white(self, board):
        key = (board._transposition_key(), board.halfmove_clock)
        with self.lock:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache.move_to_end(key)
                return hit

            self._send("position fen " + board.fen())
            self._send("evalscore")
            line, _ = self._read_until(lambda x: x.startswith("evalscore "), 5.0)
            try:
                cp = int(line.split()[1])
            except Exception as e:
                raise RuntimeError("bad evalscore response: " + line) from e

            self.cache[key] = cp
            self.cache.move_to_end(key)
            if len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
            return cp

    def evaluate(self, board):
        outcome = board.outcome(claim_draw=True)
        if outcome is not None:
            if outcome.winner is None:
                return 0
            return MATE if outcome.winner == board.turn else -MATE
        return self.evaluator.evaluate(board)

    def move_score(self, board, mv, ply, tt_move=None):
        s = 0
        if tt_move is not None and mv == tt_move:
            s += 2_000_000
        if ply == 0 and self.root_policy is not None and mv == self.root_policy:
            s += 1_500_000
        if board.is_capture(mv):
            victim = board.piece_at(mv.to_square)
            if victim is None and board.is_en_passant(mv):
                victim_val = 100
            else:
                victim_val = PIECE_VAL.get(victim.piece_type, 0) if victim else 0
            attacker = board.piece_at(mv.from_square)
            attacker_val = PIECE_VAL.get(attacker.piece_type, 0) if attacker else 0
            s += 100_000 + 16 * victim_val - attacker_val
        if mv.promotion:
            s += 80_000 + PIECE_VAL.get(mv.promotion, 0)
        if board.gives_check(mv):
            s += 15_000
        return s

    def qsearch(self, board, alpha, beta, ply):
        self.nodes += 1
        if self.should_stop():
            raise InterruptedError

        outcome = board.outcome(claim_draw=True)
        if outcome is not None:
            if outcome.winner is None:
                return 0, []
            return (-MATE + ply), []

        in_check = board.is_check()
        if not in_check:
            stand = self.evaluate(board)
            if stand >= beta:
                return beta, []
            if stand > alpha:
                alpha = stand
        else:
            stand = -INF

        # NNUE is strong enough that we do not need an absurdly deep noisy
        # qsearch. Checks are included only near the horizon.
        if ply >= 12:
            return (stand if stand != -INF else alpha), []

        if in_check:
            moves = list(board.legal_moves)
        else:
            moves = [
                mv for mv in board.legal_moves
                if board.is_capture(mv) or mv.promotion or (ply <= 3 and board.gives_check(mv))
            ]
        moves.sort(key=lambda m: self.move_score(board, m, ply), reverse=True)

        best_pv = []
        for mv in moves:
            board.push(mv)
            try:
                sc, child = self.qsearch(board, -beta, -alpha, ply + 1)
                sc = -sc
            finally:
                board.pop()
            if sc >= beta:
                return beta, [mv] + child
            if sc > alpha:
                alpha = sc
                best_pv = [mv] + child
        return alpha, best_pv

    def negamax(self, board, depth, alpha, beta, ply, allow_null=True):
        self.nodes += 1
        if self.should_stop():
            raise InterruptedError

        outcome = board.outcome(claim_draw=True)
        if outcome is not None:
            if outcome.winner is None:
                return 0, []
            return (-MATE + ply), []

        if depth <= 0:
            return self.qsearch(board, alpha, beta, ply)

        in_check = board.is_check()
        key = (board._transposition_key(), depth)
        tt_entry = self.tt.get(key)
        tt_move = None
        if tt_entry is not None:
            val, flag, tt_move_uci = tt_entry
            try:
                tt_move = chess.Move.from_uci(tt_move_uci)
            except Exception:
                tt_move = None
            if flag == 0:
                return val, [tt_move] if tt_move and tt_move in board.legal_moves else []
            if flag < 0 and val <= alpha:
                return val, []
            if flag > 0 and val >= beta:
                return val, []

        # Null-move pruning. Avoid pawn-only endings where zugzwang is common.
        if allow_null and depth >= 3 and not in_check and beta < MATE - 1000:
            has_nonpawn = any(
                p.color == board.turn and p.piece_type not in (chess.PAWN, chess.KING)
                for p in board.piece_map().values()
            )
            if has_nonpawn:
                board.push(chess.Move.null())
                try:
                    reduction = 2 + (1 if depth >= 6 else 0)
                    sc, _ = self.negamax(
                        board, max(0, depth - 1 - reduction),
                        -beta, -beta + 1, ply + 1, allow_null=False
                    )
                    sc = -sc
                finally:
                    board.pop()
                if sc >= beta:
                    return beta, []

        orig_alpha = alpha
        legal = list(board.legal_moves)
        legal.sort(key=lambda m: self.move_score(board, m, ply, tt_move), reverse=True)

        best = -INF
        best_move = None
        best_pv = []
        for idx, mv in enumerate(legal):
            is_capture = board.is_capture(mv)
            gives_check = board.gives_check(mv)
            is_quiet = not is_capture and not mv.promotion and not gives_check

            board.push(mv)
            try:
                new_depth = depth - 1

                # Late-move reduction only for genuinely quiet late moves.
                reduction = 0
                if idx >= 4 and depth >= 3 and is_quiet and not in_check:
                    reduction = 1
                    if idx >= 10 and depth >= 5:
                        reduction = 2

                if idx == 0:
                    sc, child = self.negamax(
                        board, new_depth, -beta, -alpha, ply + 1, allow_null=True
                    )
                    sc = -sc
                else:
                    # Principal Variation Search: cheap zero-window probe first.
                    rd = max(0, new_depth - reduction)
                    sc, child = self.negamax(
                        board, rd, -alpha - 1, -alpha, ply + 1, allow_null=True
                    )
                    sc = -sc

                    # If a reduced search improves alpha, verify at full depth.
                    if reduction and sc > alpha:
                        sc, child = self.negamax(
                            board, new_depth, -alpha - 1, -alpha, ply + 1, allow_null=True
                        )
                        sc = -sc

                    # A real PV candidate gets a full-window re-search.
                    if sc > alpha and sc < beta:
                        sc, child = self.negamax(
                            board, new_depth, -beta, -alpha, ply + 1, allow_null=True
                        )
                        sc = -sc
            finally:
                board.pop()

            if sc > best:
                best = sc
                best_move = mv
                best_pv = [mv] + child
            if sc > alpha:
                alpha = sc
            if alpha >= beta:
                break

        if best_move is not None:
            flag = 0
            if best <= orig_alpha:
                flag = -1
            elif best >= beta:
                flag = 1
            self.tt[key] = (best, flag, best_move.uci())
        return best, best_pv

    def root_moves(self):
        legal = list(self.board.legal_moves)
        if self.limits.searchmoves:
            allowed = set(self.limits.searchmoves)
            legal = [m for m in legal if m.uci() in allowed]
        return legal

    def run(self):
        legal = self.root_moves()
        if not legal:
            return None

        # Online policy latency counts against UCI time. For deeper timed
        # searches, reserve most of the budget for local alpha-beta instead
        # of letting a sleepy web endpoint consume the whole move.
        policy_deadline = None
        if self.deadline is not None:
            now = time.monotonic()
            remaining = max(0.0, self.deadline - now)
            if self.limits.nodes == 1 or self.limits.depth == 1:
                policy_deadline = self.deadline
            else:
                policy_deadline = now + remaining * 0.40
        self.root_policy = self.policy.best_move(self.board, policy_deadline)
        if self.root_policy not in legal:
            self.root_policy = None
        fallback = self.root_policy or legal[0]
        self.best = fallback

        # Exact policy compatibility mode.
        if self.limits.nodes == 1 or self.limits.depth == 1:
            self.nodes = 1
            self.best_score = 0
            self.completed_depth = 1
            self.pv = [fallback]
            elapsed = max(1, int((time.monotonic() - self.start) * 1000))
            uci_out(f"info depth 1 seldepth 1 nodes 1 time {elapsed} nps {1000//elapsed} score cp 0 pv {fallback.uci()}")
            return fallback

        max_depth = self.limits.depth if self.limits.depth is not None else 64
        root_order = legal[:]
        root_order.sort(key=lambda m: self.move_score(self.board, m, 0), reverse=True)

        for depth in range(1, max_depth + 1):
            if self.should_stop():
                break
            alpha, beta = -INF, INF
            iter_best = None
            iter_score = -INF
            iter_pv = []
            try:
                # Root explicitly so searchmoves is respected.
                ordered = root_order[:]
                if self.best in ordered:
                    ordered.remove(self.best)
                    ordered.insert(0, self.best)
                for mv in ordered:
                    if self.should_stop():
                        raise InterruptedError
                    self.board.push(mv)
                    try:
                        sc, child = self.negamax(self.board, depth - 1, -beta, -alpha, 1)
                        sc = -sc
                    finally:
                        self.board.pop()
                    if sc > iter_score:
                        iter_score = sc
                        iter_best = mv
                        iter_pv = [mv] + child
                    if sc > alpha:
                        alpha = sc
                if iter_best is None:
                    break
            except InterruptedError:
                break

            self.best = iter_best
            self.best_score = iter_score
            self.pv = iter_pv
            self.completed_depth = depth
            root_order.sort(key=lambda m: (m == self.best, self.move_score(self.board, m, 0)), reverse=True)

            elapsed = max(1, int((time.monotonic() - self.start) * 1000))
            nps = int(self.nodes * 1000 / elapsed)
            if abs(iter_score) >= MATE - 1000:
                mate_in = max(1, (MATE - abs(iter_score) + 1) // 2)
                mate_in = mate_in if iter_score > 0 else -mate_in
                score_s = f"score mate {mate_in}"
            else:
                score_s = f"score cp {iter_score}"
            pv_s = " ".join(m.uci() for m in iter_pv[:32])
            uci_out(f"info depth {depth} seldepth {depth+16} nodes {self.nodes} time {elapsed} nps {nps} {score_s} pv {pv_s}")

        return self.best

class Engine:
    def __init__(self):
        self.board = chess.Board()
        self.policy = OnlinePolicy()
        self.evaluator = StockfishNNUE()
        self.stop_event = threading.Event()
        self.thread = None
        self.tt = {}
        self.hash_mb = 64

    def stop_search(self, wait=True):
        if self.thread and self.thread.is_alive():
            self.stop_event.set()
            if wait:
                self.thread.join(timeout=5.0)
        self.thread = None

    def parse_position(self, tokens):
        self.stop_search()
        i = 1
        if i < len(tokens) and tokens[i] == "startpos":
            self.board = chess.Board()
            i += 1
        elif i < len(tokens) and tokens[i] == "fen":
            i += 1
            fen_parts = []
            while i < len(tokens) and tokens[i] != "moves":
                fen_parts.append(tokens[i]); i += 1
            try:
                self.board = chess.Board(" ".join(fen_parts))
            except Exception as e:
                uci_out("info string invalid FEN: " + str(e))
                return
        if i < len(tokens) and tokens[i] == "moves":
            i += 1
            while i < len(tokens):
                try:
                    mv = chess.Move.from_uci(tokens[i])
                    if mv not in self.board.legal_moves:
                        raise ValueError("illegal move")
                    self.board.push(mv)
                except Exception:
                    uci_out("info string invalid move in position: " + tokens[i])
                    break
                i += 1

    def parse_go(self, tokens):
        lim = Limits()
        i = 1
        ints = {"depth","nodes","movetime","wtime","btime","winc","binc","movestogo","mate"}
        while i < len(tokens):
            t = tokens[i]
            if t in ints and i + 1 < len(tokens):
                try:
                    v = int(tokens[i+1])
                except Exception:
                    v = 0
                if t == "depth": lim.depth = max(1, v)
                elif t == "nodes": lim.nodes = max(1, v)
                elif t == "movetime": lim.movetime_ms = max(1, v)
                elif t == "wtime": lim.wtime = max(0, v)
                elif t == "btime": lim.btime = max(0, v)
                elif t == "winc": lim.winc = max(0, v)
                elif t == "binc": lim.binc = max(0, v)
                elif t == "movestogo": lim.movestogo = max(1, v)
                elif t == "mate": lim.depth = max(lim.depth or 1, max(1, v) * 2)
                i += 2
                continue
            if t == "infinite" or t == "ponder":
                lim.infinite = True
                i += 1
                continue
            if t == "searchmoves":
                i += 1
                sm = []
                while i < len(tokens) and tokens[i] not in ints and tokens[i] not in ("infinite","ponder"):
                    sm.append(tokens[i]); i += 1
                lim.searchmoves = sm
                continue
            i += 1
        return lim

    def start_search(self, lim):
        self.stop_search()
        self.stop_event = threading.Event()
        board = self.board.copy(stack=True)
        def worker():
            s = Search(board, lim, self.policy, self.evaluator, self.stop_event, self.tt)
            try:
                mv = s.run()
            except Exception as e:
                uci_out("info string search error: " + str(e).replace("\n"," ")[:180])
                moves = list(board.legal_moves)
                mv = moves[0] if moves else None
            uci_out("bestmove " + (mv.uci() if mv else "0000"))
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def setoption(self, line):
        low = line.lower()
        if "name clear hash" in low:
            self.tt.clear()
            self.evaluator.clear()
            return
        if "name hash" in low and " value " in low:
            try:
                self.hash_mb = max(1, min(1024, int(line.rsplit(" value ",1)[1])))
                # Python dict cannot enforce MB tightly; use value as a soft cap trigger.
                if len(self.tt) > self.hash_mb * 4000:
                    self.tt.clear()
            except Exception:
                pass
            return
        if "name onlinepolicy" in low and " value " in low:
            v = line.rsplit(" value ",1)[1].strip().lower()
            self.policy.enabled = v not in ("false","0","off")
            return
        if "name policytimeout" in low and " value " in low:
            try:
                self.policy.timeout = max(2.0, min(60.0, float(line.rsplit(" value ",1)[1])))
            except Exception:
                pass
            return

    def loop(self):
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            tok = line.split()
            cmd = tok[0].lower()
            if cmd == "uci":
                uci_out(f"id name {NAME}")
                uci_out(f"id author {AUTHOR}")
                uci_out("info string Evaluator: Stockfish dev 49ea5ded SFNNv16 NNUE; Stockfish search disabled")
                uci_out("option name Hash type spin default 64 min 1 max 1024")
                uci_out("option name Clear Hash type button")
                uci_out("option name OnlinePolicy type check default true")
                uci_out("option name PolicyTimeout type spin default 12 min 2 max 60")
                uci_out("uciok")
            elif cmd == "isready":
                uci_out("readyok")
            elif cmd == "ucinewgame":
                self.stop_search()
                self.tt.clear()
                self.evaluator.clear()
                self.policy.clear()
                self.board = chess.Board()
            elif cmd == "position":
                self.parse_position(tok)
            elif cmd == "go":
                self.start_search(self.parse_go(tok))
            elif cmd == "stop":
                self.stop_search()
            elif cmd == "ponderhit":
                pass
            elif cmd == "setoption":
                self.setoption(line)
            elif cmd == "quit":
                self.stop_search()
                self.evaluator.close()
                break
            elif cmd == "d":
                uci_out("info string " + self.board.fen())

if __name__ == "__main__":
    Engine().loop()
