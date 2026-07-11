#ifndef _FEN_VALIDATION_H_
#define _FEN_VALIDATION_H_

#include "position.h"

#include <array>
#include <sstream>
#include <string>

namespace Stockfish::Tools {

namespace FenValidationDetail {

inline bool standard_ep_target_is_consistent(const Position& position,
                                             const std::string& side_to_move,
                                             const std::string& ep_field)
{
    if (ep_field.size() != 2 || ep_field[0] < 'a' || ep_field[0] > 'h'
        || (side_to_move != "w" && side_to_move != "b"))
        return false;

    const Color side = side_to_move == "w" ? WHITE : BLACK;
    const char expected_rank = side == WHITE ? '6' : '3';
    if (ep_field[1] != expected_rank)
        return false;

    const Square target =
      make_square(File(ep_field[0] - 'a'), Rank(ep_field[1] - '1'));
    const Color moved_side = ~side;
    const Direction push = pawn_push(moved_side);
    const Square pawn_square = target + push;
    const Square origin_square = target - push;

    return position.piece_on(target) == NO_PIECE
        && position.piece_on(pawn_square) == make_piece(moved_side, PAWN)
        && position.piece_on(origin_square) == NO_PIECE;
}

}  // namespace FenValidationDetail

// Compare the rule-relevant FEN fields after parsing. Fairy emits X-FEN and
// therefore drops a standard-FEN en-passant target when no opposing pawn can
// capture. Accept only that normalization, and only when the double-pushed
// pawn, empty target, and empty origin make the standard target internally
// consistent.
inline bool fen_rule_fields_match(const std::string& input, const Position& position)
{
    std::istringstream input_stream(input);
    std::istringstream normalized_stream(position.fen());
    std::array<std::string, 4> input_fields;
    std::array<std::string, 4> normalized_fields;

    for (int field = 0; field < 4; ++field)
        if (!(input_stream >> input_fields[field])
            || !(normalized_stream >> normalized_fields[field]))
            return false;

    for (int field = 0; field < 3; ++field)
        if (input_fields[field] != normalized_fields[field])
            return false;

    if (input_fields[3] == normalized_fields[3])
        return true;

    return normalized_fields[3] == "-"
        && FenValidationDetail::standard_ep_target_is_consistent(
          position, input_fields[1], input_fields[3]);
}

}  // namespace Stockfish::Tools

#endif
