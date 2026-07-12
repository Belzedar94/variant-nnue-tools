#include "evaluate.h"
#include "tools/atomic_data_schema.h"
#include "tools/packed_sfen.h"
#include "tools/random_seed.h"
#include "tools/sfen_stream.h"

#include <cassert>
#include <cstdint>
#include <iostream>

using namespace Stockfish;

namespace {

void test_atomic_data_schema_handshake()
{
    using namespace Stockfish::Tools;

    assert(AtomicDataSchemaSha256
           == "acca0f551f1c012c31a6c727dedccaebb7b5ebbc46810edb87e31bb208d5abe1");
#if DATA_SIZE == 512
    static_assert(LegacyAtomicV1Available);
    static_assert(LegacyAtomicV1RecordSize == sizeof(PackedSfenValue));
    assert(atomic_data_schema_json()
           == "{\"schema_sha256\":\"acca0f551f1c012c31a6c727dedccaebb7b5ebbc46810edb87e31bb208d5abe1\","
              "\"formats\":{\"legacy-atomic-v1\":{\"read\":true,\"write\":true,"
              "\"record_size\":72}}}");
#else
    static_assert(!LegacyAtomicV1Available);
    static_assert(sizeof(PackedSfenValue) == 136);
    assert(atomic_data_schema_json()
           == "{\"schema_sha256\":\"acca0f551f1c012c31a6c727dedccaebb7b5ebbc46810edb87e31bb208d5abe1\","
              "\"formats\":{}}");
#endif
}

void expect_legacy_move(Move move, std::uint16_t expected)
{
    using Stockfish::Tools::decode_legacy_move;
    using Stockfish::Tools::encode_legacy_move;

    assert(encode_legacy_move(move) == expected);

    const Move decoded = decode_legacy_move(expected);
    assert(from_sq(decoded) == from_sq(move));
    assert(to_sq(decoded) == to_sq(move));
    assert(type_of(decoded) == type_of(move));
    assert(promotion_type(decoded) == promotion_type(move));
}

void test_legacy_move_wire_format()
{
    expect_legacy_move(make_move(SQ_E2, SQ_E4), 0x031c);
    expect_legacy_move(make<CASTLING>(SQ_E1, SQ_H1), 0xc107);
    expect_legacy_move(make<EN_PASSANT>(SQ_E5, SQ_D6), 0x892b);
    expect_legacy_move(make<PROMOTION>(SQ_E7, SQ_E8, KNIGHT), 0x4d3c);
    expect_legacy_move(make<PROMOTION>(SQ_E7, SQ_E8, BISHOP), 0x5d3c);
    expect_legacy_move(make<PROMOTION>(SQ_E7, SQ_E8, ROOK), 0x6d3c);
    expect_legacy_move(make<PROMOTION>(SQ_E7, SQ_E8, QUEEN), 0x7d3c);
}

void test_unrepresentable_legacy_moves_are_rejected()
{
    using Stockfish::Tools::try_encode_legacy_move;

    std::uint16_t encoded = 0;
    assert(!try_encode_legacy_move(MOVE_NONE, encoded));
    assert(!try_encode_legacy_move(MOVE_NULL, encoded));
    assert(!try_encode_legacy_move(make<DROP>(SQ_A1, SQ_A1, PAWN), encoded));
    assert(!try_encode_legacy_move(make<PIECE_PROMOTION>(SQ_E2, SQ_E4), encoded));
    assert(!try_encode_legacy_move(
      make_gating<NORMAL>(SQ_E2, SQ_E4, KNIGHT, SQ_E2), encoded));
    assert(!try_encode_legacy_move(
      make<PROMOTION>(SQ_E7, SQ_E8, COMMONER), encoded));
}

void test_epd_output_does_not_encode_unused_moves()
{
    using Stockfish::Tools::encode_move_for_sfen_output;
    using Stockfish::Tools::SfenOutputType;

    const Move unrepresentable_drop = make_drop(SQ_E4, PAWN, PAWN);
    assert(encode_move_for_sfen_output(unrepresentable_drop, SfenOutputType::Epd) == 0);
    assert(encode_move_for_sfen_output(make_move(SQ_E2, SQ_E4), SfenOutputType::Bin)
           == 0x031c);
}

void test_replayable_prng_seed()
{
    using Stockfish::Tools::resolve_replayable_seed;

    PRNG first("tools-wire-test");
    PRNG second("tools-wire-test");
    assert(first.get_seed() == 4843478989694531390ULL);
    assert(resolve_replayable_seed("tools-wire-test") == "4843478989694531390");

    for (int i = 0; i < 64; ++i)
        assert(first.rand<std::uint64_t>() == second.rand<std::uint64_t>());

    PRNG zero("0");
    assert(zero.get_seed() != 0);

    PRNG overflow_a("18446744073709551616");
    PRNG overflow_b("18446744073709551616");
    assert(overflow_a.get_seed() == overflow_b.get_seed());

    PRNG seed_a("123456789");
    PRNG seed_b("123456789");
    assert(seed_a.next_random_seed() == seed_b.next_random_seed());
}

void test_nnue_mode_is_preserved_after_variant_matching()
{
    using namespace Stockfish::Eval::NNUE;

    assert(use_nnue_mode_for_variant_network(UseNNUEMode::False, true) == UseNNUEMode::False);
    assert(use_nnue_mode_for_variant_network(UseNNUEMode::True, true) == UseNNUEMode::True);
    assert(use_nnue_mode_for_variant_network(UseNNUEMode::Pure, true) == UseNNUEMode::Pure);
    assert(use_nnue_mode_for_variant_network(UseNNUEMode::True, false) == UseNNUEMode::False);
    assert(use_nnue_mode_for_variant_network(UseNNUEMode::Pure, false) == UseNNUEMode::False);
}

}  // namespace

int main()
{
#if DATA_SIZE == 512
    static_assert(sizeof(Stockfish::Tools::PackedSfenValue) == 72);
#else
    static_assert(sizeof(Stockfish::Tools::PackedSfenValue) == 136);
#endif

    test_atomic_data_schema_handshake();
    test_legacy_move_wire_format();
    test_unrepresentable_legacy_moves_are_rejected();
    test_epd_output_does_not_encode_unused_moves();
    test_replayable_prng_seed();
    test_nnue_mode_is_preserved_after_variant_matching();

    std::cout << "tools unit tests passed\n";
}
