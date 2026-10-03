"""The passages checked for term proximity (search_index._pair_pids): the same ones the naive test picks (some pair of
adjacent question words has a token in the passage on each side), found without testing every passage against every
pair. A long question on a big repository (a bug report on Home Assistant: 419,000 passages, 130 pairs) took 36 s."""

from __future__ import annotations

import random

from verinoda import search_index as si


def naive(acc, pairs):
    want = set().union(*(a | b for a, b in pairs))

    def has_pair(keys):
        return any(keys & a and keys & b for a, b in pairs)
    return {pid for pid, a_p in acc.items() if len(want & a_p.keys()) >= 2 and has_pair(a_p.keys())}


def test_the_inverted_walk_finds_what_the_naive_test_finds():
    rng = random.Random(7)
    vocab = [f"t{i}" for i in range(40)]
    for _ in range(200):
        acc = {pid: {t: 1.0 for t in rng.sample(vocab, rng.randint(0, 8))} for pid in range(rng.randint(0, 60))}
        words = rng.sample(vocab, rng.randint(2, 10))
        sets = [frozenset(rng.sample(vocab, rng.randint(1, 2)) + [w]) for w in words]
        pairs = [(a, b) for a, b in zip(sets, sets[1:]) if a and b and not (a & b)]
        if not pairs:
            continue
        assert si._pair_pids(acc, pairs) == naive(acc, pairs)


def test_pairs_that_share_a_token_need_two_distinct_tokens_as_before():
    a, b = frozenset({"x", "y"}), frozenset({"y", "z"})  # overlapping sets are never made by _query_pairs, still the same answer
    acc = {1: {"y": 1.0}, 2: {"x": 1.0, "z": 1.0}, 3: {"x": 1.0}, 4: {"y": 1.0, "z": 1.0}}
    assert si._pair_pids(acc, [(a, b)]) == naive(acc, [(a, b)]) == {2, 4}


def test_no_pairs_or_no_passages_give_nothing():
    assert si._pair_pids({}, [(frozenset({"a"}), frozenset({"b"}))]) == set()
    assert si._pair_pids({1: {"a": 1.0, "b": 1.0}}, []) == set()
