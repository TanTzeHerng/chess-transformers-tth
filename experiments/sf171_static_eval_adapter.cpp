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
    std::cerr << "EVAL_STAGE init" << std::endl;
    Bitboards::init();
    Position::init();
    const std::string big = argv[1], small = argv[2];
    NN::Networks nets(
        NN::NetworkBig({EvalFileDefaultNameBig, "", ""}, NN::EmbeddedNNUEType::BIG),
        NN::NetworkSmall({EvalFileDefaultNameSmall, "", ""}, NN::EmbeddedNNUEType::SMALL)
    );
    std::cerr << "EVAL_STAGE net_big_load" << std::endl;
    nets.big.load("", big);
    std::cerr << "EVAL_STAGE net_small_load" << std::endl;
    nets.small.load("", small);
    std::cerr << "EVAL_STAGE create_acc" << std::endl;
    NN::AccumulatorStack acc;
    std::cerr << "EVAL_STAGE accumulator_allocated" << std::endl;
    auto caches = std::make_unique<NN::AccumulatorCaches>(nets);
    std::cerr << "EVAL_STAGE caches_allocated" << std::endl;
    std::cerr << "EVAL_STAGE ready" << std::endl;
    std::string fen;
    while (std::getline(std::cin, fen)) {
        if (fen.empty()) continue;
        StateInfo state{};
        Position p;
        std::cerr << "EVAL_STAGE fen_received" << std::endl;
        p.set(fen, false, &state);
        std::cerr << "EVAL_STAGE position_set" << std::endl;
        acc.reset(p, nets, *caches);
        std::cerr << "EVAL_STAGE accumulator_reset" << std::endl;
        // Stockfish's release build uses NDEBUG. The original evaluator
        // contains an assertion rejecting checked positions in debug builds;
        // its NNUE forward computation does not need a search or legal moves.
        const Value val = Eval::evaluate(nets, p, acc, *caches, 0);
        std::cerr << "EVAL_STAGE evaluated" << std::endl;
        std::cout << (p.side_to_move() == WHITE ? val : -val) << '\n' << std::flush;
    }
    return 0;
}
