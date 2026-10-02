from pathlib import Path

root = Path("Stockfish/src")
engine_h = root / "engine.h"
engine_cpp = root / "engine.cpp"
uci_cpp = root / "uci.cpp"

s = engine_h.read_text(encoding="utf-8")
needle = "    void trace_eval() const;\n"
if needle not in s:
    raise SystemExit("engine.h patch point not found")
s = s.replace(needle, needle + "    int eval_score() const;\n", 1)
engine_h.write_text(s, encoding="utf-8")

s = engine_cpp.read_text(encoding="utf-8")
needle = """void Engine::trace_eval() const {
    StateListPtr trace_states(new std::deque<StateInfo>(1));
    Position     p;
    p.set(pos.fen(), options["UCI_Chess960"], &trace_states->back());

    verify_network();

    sync_cout << "\\n" << Eval::trace(p, *network) << sync_endl;
}
"""
if needle not in s:
    raise SystemExit("engine.cpp patch point not found")
addition = needle + r'''
int Engine::eval_score() const {
    StateListPtr eval_states(new std::deque<StateInfo>(1));
    Position     p;

    if (auto err = p.set(pos.fen(), options["UCI_Chess960"], &eval_states->back()))
        return 0;

    verify_network();

    // Static evaluation is undefined while in check. The parent UCI engine's
    // quiescence search always searches evasions instead of calling evalscore.
    if (p.checkers())
        return 0;

    auto accumulators = std::make_unique<Eval::NNUE::AccumulatorStack>();
    auto caches       = std::make_unique<Eval::NNUE::AccumulatorCaches>(*network);

    Value v = Eval::evaluate(*network, p, *accumulators, *caches, VALUE_ZERO);

    // Return centipawns from White's perspective so the wrapper can use an
    // ordinary negamax convention.
    int cp = UCIEngine::to_cp(v, p);
    return p.side_to_move() == WHITE ? cp : -cp;
}
'''
s = s.replace(needle, addition, 1)
engine_cpp.write_text(s, encoding="utf-8")

s = uci_cpp.read_text(encoding="utf-8")
needle = '''        else if (token == "eval")
            engine.trace_eval();
'''
if needle not in s:
    raise SystemExit("uci.cpp patch point not found")
s = s.replace(
    needle,
    needle + '''        else if (token == "evalscore")
            sync_cout << "evalscore " << engine.eval_score() << sync_endl;
''',
    1,
)
uci_cpp.write_text(s, encoding="utf-8")

print("Stockfish patched with evalscore command")
