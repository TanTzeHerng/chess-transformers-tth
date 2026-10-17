// Standalone evaluation adapter. The official Stockfish 17.1 source is unchanged.
// This evaluates every successor, including positions with the opponent in check,
// by invoking its original Eval::evaluate() (not search or quiescence search).
#include <iostream>
#include <memory>
#include <string>
#include "bitboard.h"
#include "position.h"
#include "evaluate.h"
#include "nnue/network.h"
#include "nnue/nnue_accumulator.h"

int main(int argc, char** argv) {
    using namespace Stockfish;
    namespace NN = Stockfish::Eval::NNUE;
    if (argc != 3) {
        std::cerr << "usage: sf_static_eval <big_net> <small_net>\n";
        return 2;
    }
    std::ios::sync_with_stdio(false);
    Bitboards::init();
    Position::init();
    const std::string big = argv[1], small = argv[2];
    NN::Networks nets(
        NN::NetworkBig({EvalFileDefaultNameBig, big, ""}, NN::EmbeddedNNUEType::BIG),
        NN::NetworkSmall({EvalFileDefaultNameSmall, small, ""}, NN::EmbeddedNNUEType::SMALL)
    );
    nets.big.load("", big);
    nets.small.load("", small);
    NN::AccumulatorStack acc;
    auto caches = std::make_unique<NN::AccumulatorCaches>(nets);
    std::string fen;
    while (std::getline(std::cin, fen)) {
        if (fen.empty()) continue;
        StateInfo state{};
        Position p;
        p.set(fen, false, &state);
        acc.reset(p, nets, *caches);
        // Stockfish's release build uses NDEBUG. The original evaluator
        // contains an assertion rejecting checked positions in debug builds;
        // its NNUE forward computation does not need a search or legal moves.
        const Value val = Eval::evaluate(nets, p, acc, *caches, 0);
        std::cout << (p.side_to_move() == WHITE ? val : -val) << '\n' << std::flush;
    }
    return 0;
}
