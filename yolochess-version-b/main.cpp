/*
 YoloChess Version B Core
 Custom UCI PVS/alpha-beta search using Stockfish 16's mature hand-crafted
 classical evaluation (HCE). No NNUE is used by this engine.
 Stockfish-derived infrastructure is GPLv3.
*/
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <deque>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "bitboard.h"
#include "endgame.h"
#include "evaluate.h"
#include "misc.h"
#include "movegen.h"
#include "position.h"
#include "psqt.h"
#include "search.h"
#include "thread.h"
#include "types.h"
#include "uci.h"

using namespace Stockfish;

static constexpr Value INFV = Value(30000);
static constexpr Value MATEV = Value(29000);
static constexpr int MAX_QPLY = 20;
static constexpr const char* START_FEN =
  "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

struct StopSearch {};

struct LocalTTEntry {
    Key key = 0;
    Move move = MOVE_NONE;
    Value score = VALUE_ZERO;
    int depth = -1;
    uint8_t flag = 0; // 0 exact, 1 lower, 2 upper
};

class SimpleTT {
public:
    void resize_mb(size_t mb) {
        size_t n = std::max<size_t>(1024, (mb * 1024ULL * 1024ULL) / sizeof(LocalTTEntry));
        size_t p = 1;
        while ((p << 1) <= n) p <<= 1;
        table.assign(p, LocalTTEntry{});
        mask = p - 1;
    }
    void clear() { std::fill(table.begin(), table.end(), LocalTTEntry{}); }
    LocalTTEntry* probe(Key k) {
        if (table.empty()) return nullptr;
        auto& e = table[size_t(k) & mask];
        return e.key == k ? &e : nullptr;
    }
    void store(Key k, int depth, Value score, uint8_t flag, Move m) {
        if (table.empty()) return;
        auto& e = table[size_t(k) & mask];
        if (e.key != k || depth >= e.depth || flag == 0)
            e = LocalTTEntry{k, m, score, depth, flag};
    }
private:
    std::vector<LocalTTEntry> table;
    size_t mask = 0;
};

struct Limits {
    int depth = 0;
    uint64_t nodes = 0;
    int movetime = 0;
    int wtime = -1, btime = -1, winc = 0, binc = 0, movestogo = 0;
    bool infinite = false;
    std::vector<std::string> searchmoves;
};

static Move parse_move(const Position& pos, const std::string& u) {
    std::string s = u;
    return UCI::to_move(pos, s);
}

static int piece_order_value(PieceType pt) {
    switch (pt) {
        case PAWN: return 100;
        case KNIGHT: return 320;
        case BISHOP: return 330;
        case ROOK: return 500;
        case QUEEN: return 900;
        default: return 0;
    }
}

class Searcher {
public:
    Searcher(Position& p, SimpleTT& t, std::atomic<bool>& stopFlag,
             Limits lim, std::string policy)
        : pos(p), tt(t), stop(stopFlag), limits(std::move(lim)),
          policyUci(std::move(policy)) {

        for (auto& k : killers) { k[0] = MOVE_NONE; k[1] = MOVE_NONE; }
        for (auto& cc : history)
            for (auto& from : cc)
                from.fill(0);

        start = std::chrono::steady_clock::now();
        configure_deadline();

        rootPolicy = policyUci.empty() ? MOVE_NONE : parse_move(pos, policyUci);
        for (const auto& s : limits.searchmoves) {
            Move m = parse_move(pos, s);
            if (m != MOVE_NONE) allowedRoot.push_back(m);
        }
    }

    Move run() {
        MoveList<LEGAL> rootList(pos);
        if (rootList.size() == 0) return MOVE_NONE;

        Move fallback = MOVE_NONE;
        if (rootPolicy != MOVE_NONE && rootList.contains(rootPolicy))
            fallback = rootPolicy;
        if (fallback == MOVE_NONE) fallback = *rootList.begin();

        bestMove = fallback;
        bestPV = {fallback};

        int maxDepth = limits.depth ? limits.depth : 64;
        Value prev = VALUE_ZERO;

        for (int depth = 1; depth <= maxDepth; ++depth) {
            try {
                check_stop(true);

                Value alpha = -INFV, beta = INFV;
                int window = 40;
                if (depth >= 4) {
                    alpha = std::max<Value>(-INFV, Value(prev - window));
                    beta  = std::min<Value>( INFV, Value(prev + window));
                }

                std::vector<Move> pv;
                Value score;
                for (;;) {
                    pv.clear();
                    score = negamax(depth, alpha, beta, 0, true, pv);
                    if (score <= alpha && alpha > -INFV) {
                        window *= 2;
                        alpha = std::max<Value>(-INFV, Value(score - window));
                        beta  = std::min<Value>( INFV, Value(score + window / 2));
                        continue;
                    }
                    if (score >= beta && beta < INFV) {
                        window *= 2;
                        beta  = std::min<Value>( INFV, Value(score + window));
                        alpha = std::max<Value>(-INFV, Value(score - window / 2));
                        continue;
                    }
                    break;
                }

                if (!pv.empty()) {
                    bestMove = pv[0];
                    bestPV = pv;
                    bestScore = score;
                    completedDepth = depth;
                    prev = score;
                }
                print_info(depth, score, pv);
            } catch (const StopSearch&) {
                break;
            }
        }
        return bestMove;
    }

private:
    Position& pos;
    SimpleTT& tt;
    std::atomic<bool>& stop;
    Limits limits;
    std::string policyUci;
    Move rootPolicy = MOVE_NONE;
    std::vector<Move> allowedRoot;

    uint64_t nodes = 0;
    uint64_t selDepth = 0;
    std::chrono::steady_clock::time_point start, deadline;
    bool hasDeadline = false;

    Move bestMove = MOVE_NONE;
    Value bestScore = VALUE_ZERO;
    int completedDepth = 0;
    std::vector<Move> bestPV;
    std::array<std::array<Move,2>, MAX_PLY> killers{};
    std::array<std::array<std::array<int,64>,64>, COLOR_NB> history{};

    void configure_deadline() {
        using namespace std::chrono;
        int ms = 0;
        if (limits.movetime > 0) {
            ms = limits.movetime;
        } else {
            int remain = pos.side_to_move() == WHITE ? limits.wtime : limits.btime;
            int inc = pos.side_to_move() == WHITE ? limits.winc : limits.binc;
            if (remain >= 0) {
                int mtg = limits.movestogo > 0 ? limits.movestogo : 28;
                double alloc = double(remain) / std::max(8, mtg) + 0.80 * inc;
                alloc = std::min(alloc, remain * 0.22);
                int reserve = std::max(15, std::min(1000, remain / 30));
                ms = std::max(1, std::min<int>(int(alloc), std::max(1, remain - reserve)));
            }
        }
        if (ms > 0 && !limits.infinite) {
            hasDeadline = true;
            deadline = start + milliseconds(ms);
        }
    }

    uint64_t elapsed_ms() const {
        using namespace std::chrono;
        return duration_cast<milliseconds>(steady_clock::now() - start).count();
    }

    void check_stop(bool force = false) {
        if (!force && (nodes & 1023ULL)) return;
        if (stop.load(std::memory_order_relaxed)) throw StopSearch{};
        if (limits.nodes && nodes >= limits.nodes) throw StopSearch{};
        if (hasDeadline && std::chrono::steady_clock::now() >= deadline) throw StopSearch{};
    }

    Value static_eval() {
        if (pos.checkers()) return VALUE_ZERO;
        // Eval::useNNUE is forced false in main(), so this executes the mature
        // Stockfish 16 hand-crafted Evaluation<NO_TRACE> path only.
        return Eval::evaluate(pos);
    }

    bool root_allowed(Move m) const {
        if (allowedRoot.empty()) return true;
        return std::find(allowedRoot.begin(), allowedRoot.end(), m) != allowedRoot.end();
    }

    int move_order_score(Move m, Move ttMove, int ply) {
        if (m == ttMove) return 8'000'000;
        if (ply == 0 && m == rootPolicy) return 7'000'000;

        int s = 0;
        if (pos.capture(m)) {
            Piece victim = pos.piece_on(to_sq(m));
            int vv = victim == NO_PIECE ? 100 : piece_order_value(type_of(victim));
            int av = piece_order_value(type_of(pos.moved_piece(m)));
            s += 2'000'000 + 16 * vv - av;
            if (pos.see_ge(m, VALUE_ZERO)) s += 100'000;
        } else {
            if (ply < MAX_PLY && m == killers[ply][0]) s += 1'500'000;
            else if (ply < MAX_PLY && m == killers[ply][1]) s += 1'400'000;
            Color us = pos.side_to_move();
            s += history[us][from_sq(m)][to_sq(m)];
        }

        if (type_of(m) == PROMOTION)
            s += 1'000'000 + piece_order_value(promotion_type(m));
        if (pos.gives_check(m)) s += 80'000;
        return s;
    }

    std::vector<Move> ordered_moves(Move ttMove, int ply, bool qsearchOnly, bool inCheck) {
        std::vector<std::pair<int,Move>> tmp;
        for (Move m : MoveList<LEGAL>(pos)) {
            if (ply == 0 && !root_allowed(m)) continue;
            if (qsearchOnly && !inCheck && !pos.capture(m) && type_of(m) != PROMOTION)
                continue;
            tmp.emplace_back(move_order_score(m, ttMove, ply), m);
        }
        std::stable_sort(tmp.begin(), tmp.end(),
            [](const auto& a, const auto& b){ return a.first > b.first; });

        std::vector<Move> out;
        out.reserve(tmp.size());
        for (auto& x : tmp) out.push_back(x.second);
        return out;
    }

    Value qsearch(Value alpha, Value beta, int ply, std::vector<Move>& pv) {
        ++nodes;
        selDepth = std::max<uint64_t>(selDepth, ply);
        check_stop();

        if (pos.is_draw(ply)) return VALUE_ZERO;
        bool inCheck = bool(pos.checkers());

        if (ply >= MAX_PLY - 2) {
            if (inCheck) return VALUE_ZERO;
            return static_eval();
        }

        MoveList<LEGAL> all(pos);
        if (all.size() == 0)
            return inCheck ? Value(-MATEV + ply) : VALUE_ZERO;

        if (!inCheck) {
            Value stand = static_eval();
            if (stand >= beta) return beta;
            if (stand > alpha) alpha = stand;
            if (ply >= MAX_QPLY) return alpha;
        }

        auto moves = ordered_moves(MOVE_NONE, ply, true, inCheck);
        for (Move m : moves) {
            if (!inCheck && pos.capture(m) && !pos.see_ge(m, Value(-80)))
                continue;

            StateInfo st;
            bool givesCheck = pos.gives_check(m);
            pos.do_move(m, st, givesCheck);

            std::vector<Move> child;
            Value score = Value(-qsearch(Value(-beta), Value(-alpha), ply + 1, child));
            pos.undo_move(m);

            if (score >= beta) return beta;
            if (score > alpha) {
                alpha = score;
                pv.clear();
                pv.push_back(m);
                pv.insert(pv.end(), child.begin(), child.end());
            }
        }
        return alpha;
    }

    Value negamax(int depth, Value alpha, Value beta, int ply,
                  bool pvNode, std::vector<Move>& pv) {
        ++nodes;
        selDepth = std::max<uint64_t>(selDepth, ply);
        check_stop();

        if (pos.is_draw(ply)) return VALUE_ZERO;
        if (depth <= 0) return qsearch(alpha, beta, ply, pv);
        if (ply >= MAX_PLY - 2)
            return pos.checkers() ? VALUE_ZERO : static_eval();

        Key key = pos.key();
        Move ttMove = MOVE_NONE;
        if (LocalTTEntry* e = tt.probe(key)) {
            ttMove = e->move;
            if (ply > 0 && e->depth >= depth) {
                if (e->flag == 0) return e->score;
                if (e->flag == 1 && e->score >= beta) return e->score;
                if (e->flag == 2 && e->score <= alpha) return e->score;
            }
        }

        bool inCheck = bool(pos.checkers());
        auto moves = ordered_moves(ttMove, ply, false, inCheck);
        if (moves.empty())
            return inCheck ? Value(-MATEV + ply) : VALUE_ZERO;

        Value origAlpha = alpha;
        Value best = -INFV;
        Move bestM = MOVE_NONE;
        std::vector<Move> bestLine;
        int moveCount = 0;

        for (Move m : moves) {
            ++moveCount;
            bool capture = pos.capture(m);
            bool givesCheck = pos.gives_check(m);
            bool quiet = !capture && type_of(m) != PROMOTION;

            StateInfo st;
            pos.do_move(m, st, givesCheck);

            std::vector<Move> child;
            Value score;

            if (moveCount == 1) {
                score = Value(-negamax(depth - 1, Value(-beta), Value(-alpha),
                                       ply + 1, pvNode, child));
            } else {
                int reduction = 0;
                if (!pvNode && quiet && !givesCheck && depth >= 3 && moveCount >= 4)
                    reduction = 1 + (depth >= 6 && moveCount >= 10);

                int rd = std::max(0, depth - 1 - reduction);
                score = Value(-negamax(rd, Value(-alpha - 1), Value(-alpha),
                                       ply + 1, false, child));

                if (reduction && score > alpha) {
                    child.clear();
                    score = Value(-negamax(depth - 1, Value(-alpha - 1), Value(-alpha),
                                           ply + 1, false, child));
                }
                if (score > alpha && score < beta) {
                    child.clear();
                    score = Value(-negamax(depth - 1, Value(-beta), Value(-alpha),
                                           ply + 1, true, child));
                }
            }
            pos.undo_move(m);

            if (score > best) {
                best = score;
                bestM = m;
                bestLine.clear();
                bestLine.push_back(m);
                bestLine.insert(bestLine.end(), child.begin(), child.end());
            }

            if (score > alpha) {
                alpha = score;
                pv = bestLine;
            }

            if (alpha >= beta) {
                if (quiet && ply < MAX_PLY) {
                    if (killers[ply][0] != m) {
                        killers[ply][1] = killers[ply][0];
                        killers[ply][0] = m;
                    }
                    int& h = history[pos.side_to_move()][from_sq(m)][to_sq(m)];
                    h = std::min(500000, h + depth * depth * 32);
                }
                break;
            }
        }

        uint8_t flag = 0;
        if (best <= origAlpha) flag = 2;
        else if (best >= beta) flag = 1;
        tt.store(key, depth, best, flag, bestM);

        pv = bestLine;
        return best;
    }

    void print_info(int depth, Value score, const std::vector<Move>& pv) {
        uint64_t ms = std::max<uint64_t>(1, elapsed_ms());
        uint64_t nps = nodes * 1000ULL / ms;

        std::cout << "info depth " << depth
                  << " seldepth " << selDepth
                  << " nodes " << nodes
                  << " time " << ms
                  << " nps " << nps
                  << " score " << UCI::value(score)
                  << " pv";

        for (Move m : pv)
            std::cout << " " << UCI::move(m, false);
        std::cout << std::endl;
    }
};

class CoreEngine {
public:
    CoreEngine() {
        tt.resize_mb(128);
        states = std::make_unique<std::deque<StateInfo>>(1);
        pos.set(START_FEN, false, &states->back(), Threads.main());
    }

    ~CoreEngine() { stop_and_join(); }

    void loop() {
        std::string line;
        while (std::getline(std::cin, line)) {
            std::istringstream is(line);
            std::string cmd;
            is >> cmd;

            if (cmd == "uci") {
                std::cout << "id name YoloChess Version B Core\n";
                std::cout << "id author OpenAI + jrahn/YOLOChess + Stockfish 16 HCE\n";
                std::cout << "option name Hash type spin default 128 min 1 max 2048\n";
                std::cout << "option name RootPolicyMove type string default \n";
                std::cout << "option name Clear Hash type button\n";
                std::cout << "uciok" << std::endl;
            } else if (cmd == "isready") {
                std::cout << "readyok" << std::endl;
            } else if (cmd == "ucinewgame") {
                stop_and_join();
                tt.clear();
                Threads.clear();
                rootPolicy.clear();
            } else if (cmd == "setoption") {
                handle_setoption(line);
            } else if (cmd == "position") {
                stop_and_join();
                handle_position(is);
            } else if (cmd == "go") {
                stop_and_join();
                start_search(parse_go(is));
            } else if (cmd == "stop") {
                stop_and_join();
            } else if (cmd == "quit") {
                stop_and_join();
                break;
            } else if (cmd == "d") {
                std::cout << "info string " << pos.fen() << std::endl;
            }
        }
    }

private:
    Position pos;
    std::unique_ptr<std::deque<StateInfo>> states;
    SimpleTT tt;
    std::atomic<bool> stop{false};
    std::thread worker;
    std::string rootPolicy;
    size_t hashMb = 128;

    void stop_and_join() {
        stop.store(true);
        if (worker.joinable()) worker.join();
        stop.store(false);
    }

    void handle_setoption(const std::string& line) {
        std::string lname = line;
        std::transform(lname.begin(), lname.end(), lname.begin(),
            [](unsigned char c){ return char(std::tolower(c)); });

        if (lname.find("name clear hash") != std::string::npos) {
            tt.clear();
            return;
        }

        auto vp = lname.find(" value ");
        if (lname.find("name hash") != std::string::npos && vp != std::string::npos) {
            try {
                hashMb = std::clamp<size_t>(std::stoull(line.substr(vp + 7)), 1, 2048);
                tt.resize_mb(hashMb);
            } catch (...) {}
            return;
        }

        if (lname.find("name rootpolicymove") != std::string::npos) {
            if (vp != std::string::npos) rootPolicy = line.substr(vp + 7);
            else rootPolicy.clear();
        }
    }

    void reset_fen(const std::string& fen) {
        states = std::make_unique<std::deque<StateInfo>>(1);
        pos.set(fen, false, &states->back(), Threads.main());
    }

    void handle_position(std::istringstream& is) {
        std::string token;
        is >> token;

        if (token == "startpos") {
            reset_fen(START_FEN);
            if (!(is >> token)) return;
        } else if (token == "fen") {
            std::string fen, part;
            for (int i = 0; i < 6 && (is >> part); ++i) {
                if (!fen.empty()) fen += ' ';
                fen += part;
            }
            reset_fen(fen);
            if (!(is >> token)) return;
        } else return;

        if (token != "moves") return;

        while (is >> token) {
            Move m = parse_move(pos, token);
            if (m == MOVE_NONE) {
                std::cout << "info string illegal position move " << token << std::endl;
                break;
            }
            states->emplace_back();
            pos.do_move(m, states->back());
        }
    }

    Limits parse_go(std::istringstream& is) {
        Limits l;
        std::string t;

        while (is >> t) {
            if (t == "depth") is >> l.depth;
            else if (t == "nodes") is >> l.nodes;
            else if (t == "movetime") is >> l.movetime;
            else if (t == "wtime") is >> l.wtime;
            else if (t == "btime") is >> l.btime;
            else if (t == "winc") is >> l.winc;
            else if (t == "binc") is >> l.binc;
            else if (t == "movestogo") is >> l.movestogo;
            else if (t == "infinite" || t == "ponder") l.infinite = true;
            else if (t == "searchmoves") {
                std::string m;
                while (is >> m) l.searchmoves.push_back(m);
                break;
            }
        }
        return l;
    }

    void start_search(Limits lim) {
        stop.store(false);
        worker = std::thread([this, lim = std::move(lim)]() mutable {
            try {
                Searcher s(pos, tt, stop, std::move(lim), rootPolicy);
                Move bm = s.run();
                std::cout << "bestmove "
                          << (bm == MOVE_NONE ? "0000" : UCI::move(bm, false))
                          << std::endl;
            } catch (const std::exception& e) {
                std::cout << "info string core search exception " << e.what() << std::endl;
                MoveList<LEGAL> ml(pos);
                std::cout << "bestmove "
                          << (ml.size() ? UCI::move(*ml.begin(), false) : "0000")
                          << std::endl;
            }
        });
    }
};

int main(int argc, char* argv[]) {
    try {
        CommandLine::init(argc, argv);
        UCI::init(Options);
        Tune::init();
        PSQT::init();
        Bitboards::init();
        Position::init();
        Bitbases::init();
        Endgames::init();

        // One dormant Stockfish thread supplies the pawn/material hash tables
        // used by its classical evaluator. Stockfish's own search is never called.
        Threads.set(1);
        Search::clear();

        // Critical Version B invariant: never use NNUE.
        Eval::useNNUE = false;
        Options["Use NNUE"] = std::string("false");

        CoreEngine e;
        e.loop();

        Threads.set(0);
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << std::endl;
        Threads.set(0);
        return 1;
    }
}
