#include "validate_training_data.h"

#include "fen_validation.h"
#include "movegen.h"
#include "position.h"
#include "thread.h"
#include "uci.h"
#include "variant.h"

#include "packed_sfen.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

namespace Stockfish::Tools {
namespace {

namespace fs = std::filesystem;

[[noreturn]] void validation_error(const std::string& path,
                                   const std::string& reason,
                                   std::uint64_t record = 0)
{
    std::cerr << "Validation failed for '" << path << "'";
    if (record)
        std::cerr << " at record " << record;
    std::cerr << ": " << reason << '\n';
    std::exit(EXIT_FAILURE);
}

class CheckedBitReader
{
   public:
    explicit CheckedBitReader(const PackedSfen& packed) : data(packed.data) {}

    bool read(unsigned bits, std::uint32_t& value)
    {
        if (cursor + bits > DATA_SIZE)
            return false;

        value = 0;
        for (unsigned bit = 0; bit < bits; ++bit, ++cursor)
            value |= std::uint32_t((data[cursor / 8] >> (cursor & 7)) & 1) << bit;
        return true;
    }

    bool remaining_bits_are_zero() const
    {
        for (std::size_t bit = cursor; bit < DATA_SIZE; ++bit)
            if ((data[bit / 8] >> (bit & 7)) & 1)
                return false;
        return true;
    }

   private:
    const std::uint8_t* data;
    std::size_t cursor = 0;
};

bool packed_shape_is_safe(const PackedSfen& packed,
                          const Variant& variant,
                          std::string& reason)
{
    if (DATA_SIZE != 512 || variant.maxFile != FILE_H || variant.maxRank != RANK_8)
    {
        reason = "the historical v1 validator supports only 512-bit 8x8 positions";
        return false;
    }

    std::array<PieceType, 17> indexed_piece{};
    indexed_piece.fill(NO_PIECE_TYPE);
    for (PieceSet pieces = variant.pieceTypes; pieces;)
    {
        const PieceType type = pop_lsb(pieces);
        const int index = variant.pieceIndex[type] + 1;
        if (index >= 1 && index < int(indexed_piece.size()))
            indexed_piece[index] = type;
    }

    struct Occupant
    {
        PieceType type = NO_PIECE_TYPE;
        Color color = WHITE;
    };
    std::array<Occupant, 64> board{};

    CheckedBitReader reader(packed);
    std::uint32_t value = 0;
    if (!reader.read(1, value))
    {
        reason = "truncated side-to-move field";
        return false;
    }
    const Color side_to_move = Color(value);

    std::array<Square, COLOR_NB> king_squares{};
    for (Color color : {WHITE, BLACK})
    {
        if (!reader.read(7, value) || value >= 64)
        {
            reason = "invalid or absent NNUE king square";
            return false;
        }
        king_squares[color] = Square(value);
        if (color == BLACK && king_squares[WHITE] == king_squares[BLACK])
        {
            reason = "both NNUE kings occupy the same square";
            return false;
        }
        board[value] = {variant.nnueKing, color};
    }

    for (Rank rank = RANK_8; rank >= RANK_1; --rank)
        for (File file = FILE_A; file <= FILE_H; ++file)
        {
            const Square square = make_square(file, rank);
            if (square == king_squares[WHITE] || square == king_squares[BLACK])
                continue;

            std::uint32_t first_bit = 0;
            if (!reader.read(1, first_bit))
            {
                reason = "truncated board field";
                return false;
            }
            if (!first_bit)
                continue;

            std::uint32_t remaining_code = 0;
            std::uint32_t color_bit = 0;
            if (!reader.read(4, remaining_code) || !reader.read(1, color_bit))
            {
                reason = "truncated board piece";
                return false;
            }

            const std::uint32_t code = first_bit | (remaining_code << 1);
            const std::uint32_t piece_index = (code + 1) / 2;
            if (piece_index >= indexed_piece.size()
                || indexed_piece[piece_index] == NO_PIECE_TYPE)
            {
                reason = "board contains a piece index not supported by the selected variant";
                return false;
            }
            if (indexed_piece[piece_index] == variant.nnueKing)
            {
                reason = "board contains an extra NNUE king";
                return false;
            }
            board[square] = {indexed_piece[piece_index], Color(color_bit)};
        }

    for (Color color : {WHITE, BLACK})
    {
        (void) color;
        for (PieceSet pieces = variant.pieceTypes; pieces;)
        {
            pop_lsb(pieces);
            if (!reader.read(5, value))
            {
                reason = "truncated hand-piece field";
                return false;
            }
            if (!variant.pieceDrops && value != 0)
            {
                reason = "non-drop variant contains hand pieces";
                return false;
            }
        }
    }

    const std::array<std::pair<Color, Square>, 4> castling_rooks{{
      {WHITE, SQ_H1}, {WHITE, SQ_A1}, {BLACK, SQ_H8}, {BLACK, SQ_A8}}};
    for (const auto& [color, rook_square] : castling_rooks)
    {
        if (!reader.read(1, value))
        {
            reason = "truncated castling field";
            return false;
        }
        if (value)
        {
            const Square king_square = relative_square(color, SQ_E1);
            if (!variant.castling
                || board[king_square].type != variant.castlingKingPiece[color]
                || board[king_square].color != color
                || board[rook_square].type != ROOK
                || !(variant.castlingRookPieces[color] & ROOK)
                || board[rook_square].color != color)
            {
                reason = "castling right has no matching king and corner rook";
                return false;
            }
        }
    }

    if (!reader.read(1, value))
    {
        reason = "truncated en-passant field";
        return false;
    }
    if (value)
    {
        if (!reader.read(7, value) || value >= 64)
        {
            reason = "invalid en-passant square";
            return false;
        }
        const Square target = Square(value);
        const Rank expected_rank = side_to_move == WHITE ? RANK_6 : RANK_3;
        if (rank_of(target) != expected_rank)
        {
            reason = "en-passant square is on the wrong rank";
            return false;
        }

        // set_from_packed_sfen() preserves the encoded target verbatim, so a
        // canonical re-pack alone cannot prove that it came from a double pawn
        // push. Mirror the structural consistency required for plain FENs.
        const Color moved_side = ~side_to_move;
        const Direction push = pawn_push(moved_side);
        const Square pawn_square = target + push;
        const Square origin_square = target - push;
        if (!(variant.enPassantRegion[side_to_move] & target)
            || board[target].type != NO_PIECE_TYPE
            || board[pawn_square].type != PAWN
            || board[pawn_square].color != moved_side
            || board[origin_square].type != NO_PIECE_TYPE)
        {
            reason = "en-passant square is inconsistent with a double pawn push";
            return false;
        }
    }

    for (unsigned bits : {6U, 8U, 8U, 1U})
        if (!reader.read(bits, value))
        {
            reason = "truncated clock field";
            return false;
        }

    if (!reader.remaining_bits_are_zero())
    {
        reason = "non-zero reserved bits make the packed position non-canonical";
        return false;
    }

    return true;
}

bool move_is_legal(const Position& position, Move move)
{
    for (Move legal : MoveList<LEGAL>(position))
        if (legal == move)
            return true;
    return false;
}

void validate_bin(const std::string& path)
{
    if (Options["UCI_Chess960"])
        validation_error(path, "legacy v1 data cannot represent Chess960 castling state");
    if (sizeof(PackedSfenValue) != 72)
        validation_error(path, "this build does not implement the 72-byte legacy v1 layout");

    std::error_code size_error;
    const std::uintmax_t bytes = fs::file_size(path, size_error);
    if (size_error)
        validation_error(path, "cannot determine file size: " + size_error.message());
    if (bytes == 0)
        validation_error(path, "dataset is empty");
    if (bytes % sizeof(PackedSfenValue) != 0)
        validation_error(path, "file size is not a multiple of 72 bytes");

    std::ifstream input(path, std::ios::binary);
    if (!input)
        validation_error(path, "cannot open input file");

    const auto variant_it = variants.find(std::string(Options["UCI_Variant"]));
    if (variant_it == variants.end())
        validation_error(path, "the selected UCI variant is not registered");
    const Variant& variant = *variant_it->second;

    Thread* thread = Threads.main();
    Position& position = thread->rootPos;
    StateInfo state;
    PackedSfenValue record{};
    std::uint64_t count = 0;
    while (input.read(reinterpret_cast<char*>(&record), sizeof(record)))
    {
        ++count;
        if (record.padding != 0)
            validation_error(path, "padding byte is not zero", count);
        if (record.game_result < -1 || record.game_result > 1)
            validation_error(path, "game result is outside -1, 0, 1", count);

        std::string shape_error;
        if (!packed_shape_is_safe(record.sfen, variant, shape_error))
            validation_error(path, shape_error, count);

        if (position.set_from_packed_sfen(record.sfen, &state, thread) != 0)
            validation_error(path, "packed position could not be decoded", count);

        PackedSfen canonical{};
        position.sfen_pack(canonical);
        if (std::memcmp(&canonical, &record.sfen, sizeof(canonical)) != 0)
        {
            std::size_t offset = 0;
            while (offset < sizeof(canonical)
                   && canonical.data[offset] == record.sfen.data[offset])
                ++offset;
            validation_error(
              path,
              "packed position is not canonical for the selected variant (first differing byte "
                + std::to_string(offset) + ": stored "
                + std::to_string(record.sfen.data[offset]) + ", canonical "
                + std::to_string(canonical.data[offset]) + ", decoded FEN "
                + position.fen() + ")",
              count);
        }

        const Move move = decode_legacy_move(record.move);
        std::uint16_t encoded = 0;
        if (!try_encode_legacy_move(move, encoded) || encoded != record.move)
            validation_error(path, "move field is not canonical legacy 16-bit encoding", count);
        if (!move_is_legal(position, move))
            validation_error(path, "move is not legal in the packed position", count);
    }
    if (!input.eof())
        validation_error(path, "read error before end of file", count + 1);

    std::cout << "Validation passed: " << count
              << " canonical legacy v1 records (72 bytes each).\n";
}

void validate_plain(const std::string& path)
{
    if (Options["UCI_Chess960"])
        validation_error(path, "legacy v1 data cannot represent Chess960 castling state");

    std::ifstream input(path);
    if (!input)
        validation_error(path, "cannot open input file");

    const auto variant_it = variants.find(std::string(Options["UCI_Variant"]));
    if (variant_it == variants.end())
        validation_error(path, "the selected UCI variant is not registered");

    Thread* thread = Threads.main();
    Position& position = thread->rootPos;
    StateInfo state;
    bool record_started = false;
    bool has_fen = false;
    bool has_move = false;
    std::uint64_t record_count = 0;
    std::uint64_t line_number = 0;
    std::string line;
    while (std::getline(input, line))
    {
        ++line_number;
        std::istringstream fields(line);
        std::string token;
        fields >> token;
        if (token.empty())
            continue;

        if (token == "fen")
        {
            if (record_started)
                validation_error(path, "new FEN before previous record terminator", record_count + 1);
            const std::size_t value_start = line.find_first_not_of(" \t", line.find(token) + token.size());
            if (value_start == std::string::npos)
                validation_error(path, "missing FEN at line " + std::to_string(line_number));
            const std::string fen = line.substr(value_start);
            position.set(variant_it->second, fen, false, &state, thread);
            if (!fen_rule_fields_match(fen, position))
                validation_error(path, "invalid or non-canonical FEN at line "
                                       + std::to_string(line_number));
            record_started = true;
            has_fen = true;
            has_move = false;
        }
        else if (token == "move")
        {
            std::string text;
            fields >> text;
            const Move move = has_fen ? UCI::to_move(position, text) : MOVE_NONE;
            std::uint16_t encoded = 0;
            if (!try_encode_legacy_move(move, encoded))
                validation_error(path, "invalid, illegal, or unrepresentable move at line "
                                       + std::to_string(line_number));
            has_move = true;
            record_started = true;
        }
        else if (token == "score")
        {
            double score = 0;
            if (!(fields >> score) || !std::isfinite(score))
                validation_error(path, "invalid score at line " + std::to_string(line_number));
            record_started = true;
        }
        else if (token == "ply")
        {
            std::int64_t ply = -1;
            if (!(fields >> ply) || ply < 0
                || ply > std::numeric_limits<std::uint16_t>::max())
                validation_error(path, "invalid ply at line " + std::to_string(line_number));
            record_started = true;
        }
        else if (token == "result")
        {
            int result = 2;
            if (!(fields >> result) || result < -1 || result > 1)
                validation_error(path, "invalid result at line " + std::to_string(line_number));
            record_started = true;
        }
        else if (token == "e")
        {
            if (!record_started || !has_fen || !has_move)
                validation_error(path, "incomplete record at line " + std::to_string(line_number));
            ++record_count;
            record_started = has_fen = has_move = false;
        }
        else
            validation_error(path, "unknown token '" + token + "' at line "
                                   + std::to_string(line_number));
    }

    if (record_started)
        validation_error(path, "unterminated final record", record_count + 1);
    if (record_count == 0)
        validation_error(path, "dataset is empty");

    std::cout << "Validation passed: " << record_count << " plain records.\n";
}

}  // namespace

void validate_training_data(std::istringstream& input)
{
    std::vector<std::string> arguments;
    for (std::string argument; input >> argument;)
        arguments.push_back(argument);

    if (arguments.size() != 1)
    {
        std::cerr << "Usage: validate_training_data <input.plain|input.bin>\n";
        std::exit(EXIT_FAILURE);
    }

    const std::string& path = arguments.front();
    std::error_code status_error;
    if (!fs::is_regular_file(path, status_error) || status_error)
        validation_error(path, "input is not a readable regular file");

    std::string extension = fs::path(path).extension().string();
    std::transform(extension.begin(), extension.end(), extension.begin(),
                   [](unsigned char c) { return char(std::tolower(c)); });
    if (extension == ".bin")
        validate_bin(path);
    else if (extension == ".plain")
        validate_plain(path);
    else
        validation_error(path, "supported extensions are .plain and .bin");
}

}  // namespace Stockfish::Tools
