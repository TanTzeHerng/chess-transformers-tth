// Static NNUE successor evaluator linked against official, unmodified Stockfish 19 objects.
// No search, quiescence, root heuristics or NNUE training changes.
// Output scores in original Stockfish internal units from White's perspective.
#include <iostream>
#include <memory>
#include <string>
#include "bitboard.h"
#include "position.h"
#include "evaluate.h"
#include "nnue/network.h"
#include "nnue/nnue_accumulator.h"
#include "nnue/nnue_misc.h"

int main(int argc, char** argv) {
    using namespace Stockfish;
    namespace NN = Stockfish::Eval::NNUE;
    if (argc != 2) {
        std::cerr << "usage: sf19_static_eval <nnue_network_path>\n";
        return 2;
    }
    std::ios::sync_with_stdio(false);
    Attacks::init();
    Position::init();

    auto network = std::make_unique<NN::Network>();
    NN::EvalFile evalFile{};
    std::cerr << "EVAL_STAGE loading_network " << argv[1] << std::endl;
    network->load("", argv[1], evalFile);
    if (!evalFile.current) {
        std::cerr << "EVAL_ERROR network_not_loaded\n";
        return 3;
    }
    auto acc = std::make_unique<NN::AccumulatorStack>();
    auto caches = std::make_unique<NN::AccumulatorCaches>(*network);
    std::cerr << "EVAL_STAGE ready " << evalFile.current->string() << std::endl;

    std::string fen;
    while (std::getline(std::cin, fen)) {
        if (fen.empty()) continue;
        StateInfo state{};
        Position pos;
        if (auto error = pos.set(fen, false, &state); error.has_value()) {
            std::cerr << "EVAL_ERROR invalid_fen " << fen << std::endl;
            return 4;
        }
        acc->reset();
        // Stockfish's Eval::evaluate asserts !pos.checkers() only in debug builds.
        // Release-equivalent NDEBUG lets us evaluate checked successors without
        // quiescence; exactly the same approach as the SF17.1 baseline adapter.
        const Value value = Eval::evaluate(*network, pos, *acc, *caches, VALUE_ZERO);
        std::cout << (pos.side_to_move() == WHITE ? value : -value)
                  << '\n' << std::flush;
    }
    return 0;
}
