#ifndef _PACKED_SFEN_H_
#define _PACKED_SFEN_H_

#include "types.h"

#include <vector>
#include <cstddef>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <type_traits>

namespace Stockfish::Tools {

    // Legacy v1 stores only four castling-right bits. It cannot preserve the
    // rook origins required by Chess960, whether Chess960 is enabled through
    // the UCI option or by the selected variant itself.
    bool legacy_v1_chess960_selected();

    [[noreturn]] inline void legacy_move_encoding_error(const char* reason)
    {
        std::fprintf(stderr, "Cannot encode legacy training-data move: %s\n", reason);
        std::exit(EXIT_FAILURE);
    }

    // PackedSfenValue is a historical wire format. Its move field predates the
    // current, wider internal Move representation and must not be populated by
    // truncating a Move to 16 bits.
    inline bool try_encode_legacy_move(Move move,
                                       std::uint16_t& encoded,
                                       const char** reason = nullptr)
    {
        constexpr std::uint16_t SquareMask = 0x3f;
        constexpr int FromShift = 6;
        constexpr int PromotionShift = 12;
        constexpr int FlagShift = 14;

        auto fail = [reason](const char* message) {
            if (reason)
                *reason = message;
            return false;
        };

        if (!is_ok(move) || move == MOVE_NULL)
            return fail("the move is null or invalid");

        const int from = int(from_sq(move));
        const int to = int(to_sq(move));
        if (from < 0 || from > SquareMask || to < 0 || to > SquareMask)
            return fail("the move is not on an 8x8 board");

        if (from == to)
            return fail("the origin and destination squares are identical");

        if (is_gating(move))
            return fail("gating information is not representable");

        encoded = std::uint16_t(to | (from << FromShift));

        switch (type_of(move))
        {
        case NORMAL:
            break;
        case PROMOTION:
        {
            const PieceType promoted = promotion_type(move);
            if (promoted < KNIGHT || promoted > QUEEN)
                return fail("the promotion piece is outside knight-to-queen");

            encoded |= std::uint16_t((promoted - KNIGHT) << PromotionShift);
            encoded |= std::uint16_t(1 << FlagShift);
            break;
        }
        case EN_PASSANT:
            encoded |= std::uint16_t(2 << FlagShift);
            break;
        case CASTLING:
            encoded |= std::uint16_t(3 << FlagShift);
            break;
        default:
            return fail("the move type has no historical 16-bit representation");
        }

        return true;
    }

    inline std::uint16_t encode_legacy_move(Move move)
    {
        std::uint16_t encoded = 0;
        const char* reason = nullptr;
        if (!try_encode_legacy_move(move, encoded, &reason))
            legacy_move_encoding_error(reason);

        return encoded;
    }

    inline Move decode_legacy_move(std::uint16_t encoded)
    {
        constexpr std::uint16_t SquareMask = 0x3f;
        constexpr int FromShift = 6;
        constexpr int PromotionShift = 12;
        constexpr int FlagShift = 14;

        const Square to = Square(encoded & SquareMask);
        const Square from = Square((encoded >> FromShift) & SquareMask);

        switch ((encoded >> FlagShift) & 0x3)
        {
        case 0:
            return make_move(from, to);
        case 1:
            return make<PROMOTION>(from, to,
                                   PieceType(KNIGHT + ((encoded >> PromotionShift) & 0x3)));
        case 2:
            return make<EN_PASSANT>(from, to);
        case 3:
            return make<CASTLING>(from, to);
        }

        return MOVE_NONE;
    }

    // packed sfen
    struct PackedSfen { std::uint8_t data[DATA_SIZE / 8]; };

    // Structure in which PackedSfen and evaluation value are integrated
    // If you write different contents for each option, it will be a problem when reusing the teacher game
    // For the time being, write all the following members regardless of the options.
    struct PackedSfenValue
    {
        // phase
        PackedSfen sfen;

        // Evaluation value returned from Tools::search()
        std::int16_t score;

        // PV first move
        // Used when finding the match rate with the teacher
        std::uint16_t move;

        // Trouble of the phase from the initial phase.
        std::uint16_t gamePly;

        // 1 if the player on this side ultimately wins the game. -1 if you are losing.
        // 0 if a draw is reached.
        // The draw is in the teacher position generation command gensfen,
        // Only write if LEARN_GENSFEN_DRAW_RESULT is enabled.
        std::int8_t game_result;

        // When exchanging the file that wrote the teacher aspect with other people
        //Because this structure size is not fixed, pad it so that it is 72 bytes in any environment.
        std::uint8_t padding;

        // 64 + 2 + 2 + 2 + 1 + 1 = 72bytes
    };

    static_assert(std::is_standard_layout<PackedSfenValue>::value,
                  "Legacy training-data record must remain standard layout");
    static_assert(DATA_SIZE != 512 || sizeof(PackedSfenValue) == 72,
                  "Legacy training-data record size changed");
    static_assert(DATA_SIZE != 512 || offsetof(PackedSfenValue, score) == 64,
                  "Legacy training-data score offset changed");
    static_assert(DATA_SIZE != 512 || offsetof(PackedSfenValue, move) == 66,
                  "Legacy training-data move offset changed");
    static_assert(DATA_SIZE != 512 || offsetof(PackedSfenValue, gamePly) == 68,
                  "Legacy training-data ply offset changed");
    static_assert(DATA_SIZE != 512 || offsetof(PackedSfenValue, game_result) == 70,
                  "Legacy training-data result offset changed");
    static_assert(DATA_SIZE != 512 || offsetof(PackedSfenValue, padding) == 71,
                  "Legacy training-data padding offset changed");

    // Phase array: PSVector stands for packed sfen vector.
    using PSVector = std::vector<PackedSfenValue>;
}
#endif
