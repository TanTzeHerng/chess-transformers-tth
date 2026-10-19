#!/usr/bin/env python3
"""Independent LOS confirmation; one thread, depth 1, paired Variety openings."""
import argparse
import hashlib
import json
import random
import statistics
import sys
from pathlib import Path

import chess
import chess.polyglot
from scipy.stats import t as student_t

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evalfile_sf171_depth1_20261019 import (
    start_engine, finish_game, initial_openings, BASE, SMALL,
)

CANDIDATES = [
    "nn-1111cefa1111.nnue", "nn-1cedc0ffeeee.nnue",
    "nn-31337bea577c.nnue",
]
BATCH = 100
ALPHA_TOTAL = 0.01


def save(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf8")
    temp.replace(path)


def openings_from_variety(bookpath, count):
    # Reproduce and exclude every opening from the previous 1800-game screen.
    prior = set(map(tuple, initial_openings(bookpath, 100, 20261019)))
    rng = random.Random(20261020)
    seen = set(prior)
    result = []
    with chess.polyglot.open_reader(str(bookpath)) as book:
        for attempt in range(max(250000, count * 90)):
            if len(result) >= count:
                break
            board = chess.Board()
            for _ in range(12):
                entries = list(book.find_all(board))
                if not entries:
                    break
                entry = rng.choices(entries, [max(1, e.weight) for e in entries])[0]
                if not board.is_legal(entry.move):
                    break
                board.push(entry.move)
                if board.is_game_over(claim_draw=True):
                    break
            moves = tuple(m.uci() for m in board.move_stack)
            if len(moves) >= 4 and moves not in seen and not board.is_game_over(claim_draw=True):
                seen.add(moves)
                result.append(moves)
    if len(result) < BATCH:
        raise RuntimeError("Too few independent Variety openings: %d" % len(result))
    print("OPENING_POOL", len(result), "previous excluded:", len(prior), flush=True)
    assert not set(result).intersection(prior)
    return result


def los_for_paired_openings(scores):
    if len(scores) < 2:
        return 0.5
    mean = statistics.mean(scores)
    sd = statistics.stdev(scores)
    if sd == 0:
        return 1.0 if mean > 0.5 else 0.0 if mean < 0.5 else 0.5
    return float(student_t.cdf((mean - 0.5) / (sd / len(scores)**0.5), df=len(scores)-1))


def candidate_points(outcome, as_white):
    return 0.5 if outcome == "1/2-1/2" else float((outcome == "1-0") == as_white)


def run():
    parser = argparse.ArgumentParser()
    for name in ("engine", "nets", "book", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--maxpairs", type=int, default=3000)
    parser.add_argument("--maxplies", type=int, default=260)
    cfg = parser.parse_args()
    cfg.out.mkdir(parents=True, exist_ok=True)
    for name in [BASE, SMALL] + CANDIDATES:
        path = cfg.nets / name
        with path.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if digest[:12] != name[3:15]:
            raise RuntimeError("Network SHA mismatch: " + name)
        print("SHA_OK", name, path.stat().st_size, flush=True)

    openings = openings_from_variety(cfg.book, cfg.maxpairs)
    save(cfg.out / "new_variety_openings.json", openings)
    summary = {
        "engine": "unmodified Stockfish 17.1", "threads": 1,
        "depth": 1, "hash_mb": 16, "smallnet": SMALL, "defaultnet": BASE,
        "book": "Variety.bin", "prior_selection_games_included": False,
        "paired_openings_independent_of_prior": True,
        "monitor_every_pairs": BATCH, "familywise_alpha": ALPHA_TOTAL,
        "method": "LOS=Student_t_CDF((paired_mean-0.5)/(paired_SD/sqrt(n)),df=n-1)",
        "alpha_spending": "alpha per candidate and look k = .01/(3*k*(k+1))",
        "results": {}, "winner": None, "technical_errors": [],
    }
    save(cfg.out / "summary.json", summary)
    baseline = start_engine(cfg.engine, (cfg.nets/BASE).resolve(), (cfg.nets/SMALL).resolve())
    try:
        with (cfg.out/"games.pgn").open("w", encoding="utf8") as pgn, (cfg.out/"scores.jsonl").open("w", encoding="utf8") as ledger:
            for name in CANDIDATES:
                scores = []
                record = {"wins": 0, "draws": 0, "losses": 0, "technical_pairs": 0,
                          "valid_pairs": 0, "checks": [], "compatible": False}
                summary["results"][name] = record
                save(cfg.out/"summary.json", summary)
                try:
                    challenger = start_engine(cfg.engine, (cfg.nets/name).resolve(), (cfg.nets/SMALL).resolve())
                    record["compatible"] = True
                except Exception as exc:
                    record["error"] = repr(exc)
                    print("INCOMPATIBLE", name, repr(exc), flush=True)
                    save(cfg.out/"summary.json", summary)
                    continue
                try:
                    for i, opening in enumerate(openings):
                        results = []
                        for side in (0, 1):
                            challenger_white = (side == 0)
                            white = (name, challenger) if challenger_white else (BASE, baseline)
                            black = (BASE, baseline) if challenger_white else (name, challenger)
                            outcome, error, plies = finish_game(
                                white, black, opening, name, i, side, cfg.maxplies, pgn)
                            results.append((outcome, error, plies, challenger_white))
                        if any(game[1] for game in results):
                            record["technical_pairs"] += 1
                            info = {"network": name, "opening_index": i,
                                    "errors": [game[1] for game in results]}
                            summary["technical_errors"].append(info)
                            print("TECHNICAL", json.dumps(info), flush=True)
                            continue
                        points = [candidate_points(row[0], row[3]) for row in results]
                        scores.append(sum(points)/2)
                        record["valid_pairs"] += 1
                        for point in points:
                            if point == 1.0: record["wins"] += 1
                            elif point == 0.0: record["losses"] += 1
                            else: record["draws"] += 1
                        ledger.write(json.dumps({"network": name, "opening_index": i,
                             "paired_score": scores[-1], "game_scores": points}) + "\n")
                        ledger.flush()
                        n = len(scores)
                        if n % 25 == 0:
                            save(cfg.out/"summary.json", summary)
                        if n % BATCH == 0:
                            look = n // BATCH
                            los = los_for_paired_openings(scores)
                            threshold = 1 - ALPHA_TOTAL/(len(CANDIDATES)*look*(look+1))
                            observation = {"pairs":n, "games":2*n,
                                "score_pct":100*statistics.mean(scores),
                                "LOS":los,"required_LOS":threshold}
                            record["checks"].append(observation)
                            print("LOS_CHECK", name, json.dumps(observation), flush=True)
                            save(cfg.out/"summary.json", summary)
                            if los >= threshold:
                                summary["winner"] = {"network":name, **observation}
                                print("SUPERIORITY_THRESHOLD_MET",json.dumps(summary["winner"]), flush=True)
                                save(cfg.out/"summary.json", summary)
                                return
                finally:
                    challenger.quit()
                record["final_LOS"] = los_for_paired_openings(scores)
                record["score_pct"] = 100*statistics.mean(scores) if scores else None
                print("CANDIDATE_FINISHED",name,json.dumps(record),flush=True)
                save(cfg.out/"summary.json", summary)
    finally:
        baseline.quit()
    print("NO_SUPERIORITY_CONFIRMED_WITHIN_AVAILABLE_OPENINGS",flush=True)
    save(cfg.out/"summary.json", summary)


if __name__ == "__main__":
    run()
