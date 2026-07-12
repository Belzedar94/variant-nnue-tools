#include "convert.h"

#include "fen_validation.h"
#include "output_file.h"
#include "packed_sfen.h"
#include "uci.h"
#include "misc.h"
#include "thread.h"
#include "position.h"
#include "tt.h"

#include "nnue/evaluate_nnue.h"

#include "syzygy/tbprobe.h"

#include <sstream>
#include <fstream>
#include <unordered_set>
#include <iomanip>
#include <list>
#include <cmath>    // std::exp(),std::pow(),std::log()
#include <cstring>  // memcpy()
#include <memory>
#include <limits>
#include <optional>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <random>
#include <regex>
#include <filesystem>

using namespace std;
namespace sys = std::filesystem;

namespace Stockfish::Tools
{
    [[noreturn]] static void conversion_error(const std::string& message)
    {
        std::cerr << "ERROR: " << message << '\n';
        std::exit(EXIT_FAILURE);
    }

    static bool input_files_are_readable(const vector<string>& filenames, const char* command)
    {
        if (filenames.empty())
            conversion_error(std::string(command) + " requires at least one input file.");

        for (const auto& filename : filenames)
        {
            std::ifstream input(filename, std::ios::binary);
            if (!input)
                conversion_error("Cannot open input file '" + filename + "'.");
        }
        return true;
    }

    static bool legacy_bin_inputs_are_well_sized(const vector<string>& filenames)
    {
        for (const auto& filename : filenames)
        {
            std::error_code error;
            const auto size = sys::file_size(filename, error);
            if (error || size == 0 || size % sizeof(PackedSfenValue) != 0)
                conversion_error("Legacy bin input '" + filename
                                 + "' is truncated or unreadable.");
        }
        return true;
    }

    static void require_legacy_non_chess960()
    {
        if (legacy_v1_chess960_selected())
            conversion_error("Legacy v1 data cannot represent Chess960 castling state.");
    }

    // Compatibility parser for check_illegal_move=0. The opt-out may bypass
    // move legality, but it must never bypass the historical wire's 8x8/type
    // limits or fall back to truncating the wider internal Move value.
    static Move parse_representable_legacy_move(const Position& pos,
                                                const std::string& value)
    {
        if (value.size() != 4 && value.size() != 5)
            return MOVE_NONE;

        const auto parse_square = [](char file, char rank) {
            if (file < 'a' || file > 'h' || rank < '1' || rank > '8')
                return SQ_NONE;
            return make_square(File(file - 'a'), Rank(rank - '1'));
        };

        const Square from = parse_square(value[0], value[1]);
        const Square to = parse_square(value[2], value[3]);
        if (!is_ok(from) || !is_ok(to) || from == to)
            return MOVE_NONE;

        if (value.size() == 5)
        {
            PieceType promotion = NO_PIECE_TYPE;
            switch (char(std::tolower(static_cast<unsigned char>(value[4]))))
            {
            case 'n': promotion = KNIGHT; break;
            case 'b': promotion = BISHOP; break;
            case 'r': promotion = ROOK; break;
            case 'q': promotion = QUEEN; break;
            default: return MOVE_NONE;
            }
            return make<PROMOTION>(from, to, promotion);
        }

        const Piece mover = pos.piece_on(from);
        const Piece target = pos.piece_on(to);
        if (mover != NO_PIECE)
        {
            const Color color = color_of(mover);
            if ((pos.en_passant_types(color) & type_of(mover))
                && (pos.ep_squares() & to))
                return make<EN_PASSANT>(from, to);

            if (type_of(mover) == pos.castling_king_piece(color)
                && target != NO_PIECE && color_of(target) == color
                && (pos.castling_rook_pieces(color) & type_of(target)))
                return make<CASTLING>(from, to);
        }

        return make_move(from, to);
    }

    void convert_bin(
        const vector<string>& filenames,
        const string& output_file_name,
        const int ply_minimum,
        const int ply_maximum,
        const int interpolate_eval,
        const int src_score_min_value,
        const int src_score_max_value,
        const int dest_score_min_value,
        const int dest_score_max_value,
        const bool check_invalid_fen,
        const bool check_illegal_move)
    {
        require_legacy_non_chess960();
        std::cout << "check_invalid_fen=" << check_invalid_fen << std::endl;
        std::cout << "check_illegal_move=" << check_illegal_move << std::endl;

        if (!input_files_are_readable(filenames, "convert_bin"))
            return;
        if (ply_minimum < 0 || ply_maximum < ply_minimum
            || ply_maximum > std::numeric_limits<std::uint16_t>::max())
            conversion_error("Invalid ply range for the legacy 16-bit field.");
        if (!std::isfinite(src_score_min_value) || !std::isfinite(src_score_max_value)
            || !std::isfinite(dest_score_min_value) || !std::isfinite(dest_score_max_value))
            conversion_error("Score scaling bounds must be finite.");
        if (src_score_min_value == src_score_max_value)
            conversion_error("src_score_min_value and src_score_max_value must differ.");

        std::fstream fs;
        uint64_t data_size = 0;
        uint64_t total_data_size = 0;
        uint64_t filtered_size = 0;
        uint64_t filtered_size_fen = 0;
        uint64_t filtered_size_move = 0;
        uint64_t filtered_size_ply = 0;
        uint64_t filtered_size_record = 0;
        auto th = Threads.main();
        auto& tpos = th->rootPos;
        // convert plain rag to packed sfenvalue for Yaneura king
        open_new_output_file_or_exit(fs, output_file_name, ios::binary);
        StateListPtr states;
        for (auto filename : filenames) {
            std::cout << "convert " << filename << " ... ";
            std::string line;
            ifstream ifs;
            ifs.open(filename);
            PackedSfenValue p{};
            data_size = 0;
            filtered_size = 0;
            filtered_size_fen = 0;
            filtered_size_move = 0;
            filtered_size_ply = 0;
            bool ignore_flag_fen = false;
            bool ignore_flag_move = false;
            bool ignore_flag_ply = false;
            bool ignore_flag_record = false;
            bool has_fen = false;
            bool has_move = false;
            bool record_started = false;
            auto reset_record = [&]() {
                p = PackedSfenValue{};
                p.gamePly = 1; // Not included in apery format. Should be initialized
                ignore_flag_fen = false;
                ignore_flag_move = false;
                ignore_flag_ply = false;
                ignore_flag_record = false;
                has_fen = false;
                has_move = false;
                record_started = false;
            };
            reset_record();
            const Variant* v = variants.find(Options["UCI_Variant"])->second;
            while (std::getline(ifs, line)) {
                std::stringstream ss(line);
                std::string token;
                std::string value;
                ss >> token;
                if (token == "fen") {
                    if (record_started)
                    {
                        ++filtered_size;
                        ++filtered_size_record;
                    }
                    // A FEN starts a new record. Reset all fields so malformed
                    // or incomplete input cannot inherit data from its predecessor.
                    reset_record();
                    record_started = true;
                    const std::size_t fen_start = line.find_first_not_of(
                      " \t", line.find(token) + token.size());
                    if (fen_start == std::string::npos)
                    {
                        ignore_flag_fen = true;
                        ++filtered_size_fen;
                        continue;
                    }
                    has_fen = true;
                    states = StateListPtr(new std::deque<StateInfo>(1)); // Drop old and create a new one
                    std::string input_fen = line.substr(fen_start);
                    tpos.set(v, input_fen, false, &states->back(), Threads.main());
                    const bool missing_nnue_king = tpos.nnue_king()
                        && (tpos.count(WHITE, tpos.nnue_king()) != 1
                            || tpos.count(BLACK, tpos.nnue_king()) != 1);
                    if (missing_nnue_king
                        || (check_invalid_fen && !fen_rule_fields_match(input_fen, tpos))) {
                        ignore_flag_fen = true;
                        filtered_size_fen++;
                    }
                    else {
                        tpos.sfen_pack(p.sfen);
                    }
                }
                else if (token == "move") {
                    record_started = true;
                    ss >> value;
                    Move move = has_fen ? UCI::to_move(tpos, value) : MOVE_NONE;
                    if (move == MOVE_NONE && has_fen && !check_illegal_move)
                        move = parse_representable_legacy_move(tpos, value);
                    std::uint16_t encoded = 0;
                    const char* encoding_reason = nullptr;
                    if (move == MOVE_NONE
                        || !try_encode_legacy_move(move, encoded, &encoding_reason)) {
                        ignore_flag_move = true;
                        filtered_size_move++;
                    }
                    else {
                        p.move = encoded;
                        has_move = true;
                    }
                }
                else if (token == "score") {
                    record_started = true;
                    double score = 0;
                    if (!(ss >> score) || !std::isfinite(score))
                    {
                        ignore_flag_record = true;
                        ++filtered_size_record;
                        continue;
                    }
                    // Training Formula ?Issue #71 ?nodchip/Stockfish https://github.com/nodchip/Stockfish/issues/71
                    // Normalize to [0.0, 1.0].
                    score = (score - src_score_min_value) / (src_score_max_value - src_score_min_value);
                    // Scale to [dest_score_min_value, dest_score_max_value].
                    score = score * (dest_score_max_value - dest_score_min_value) + dest_score_min_value;
                    score = std::clamp(
                      score,
                      static_cast<double>(std::numeric_limits<std::int16_t>::min()),
                      static_cast<double>(std::numeric_limits<std::int16_t>::max()));
                    p.score = static_cast<std::int16_t>(std::round(score));
                }
                else if (token == "ply") {
                    record_started = true;
                    int64_t temp = 0;
                    if (!(ss >> temp)
                        || temp < ply_minimum || temp > ply_maximum
                        || temp < 0
                        || temp > std::numeric_limits<std::uint16_t>::max()) {
                        ignore_flag_ply = true;
                        filtered_size_ply++;
                    }
                    else {
                        p.gamePly = static_cast<uint16_t>(temp);
                    }
                    if (!ignore_flag_ply && interpolate_eval != 0) {
                        const int64_t interpolated = int64_t(interpolate_eval) * temp;
                        p.score = static_cast<std::int16_t>(std::clamp<int64_t>(
                          interpolated,
                          std::numeric_limits<std::int16_t>::min(),
                          3000));
                    }
                }
                else if (token == "result") {
                    record_started = true;
                    int temp = 0;
                    if (!(ss >> temp) || temp < -1 || temp > 1)
                    {
                        ignore_flag_record = true;
                        ++filtered_size_record;
                    }
                    else
                    {
                        p.game_result = static_cast<int8_t>(temp);
                        if (interpolate_eval)
                            p.score = static_cast<std::int16_t>(p.score * p.game_result);
                    }
                }
                else if (token == "e") {
                    if (!(ignore_flag_fen || ignore_flag_move || ignore_flag_ply
                          || ignore_flag_record || !has_fen || !has_move)) {
                        fs.write((char*)&p, sizeof(PackedSfenValue));
                        if (!fs)
                            output_file_error(output_file_name, "write failed");
                        data_size += 1;
                        total_data_size += 1;
                        // debug
                        // std::cout<<tpos<<std::endl;
                        // std::cout<<p.score<<","<<int(p.gamePly)<<","<<int(p.game_result)<<std::endl;
                    }
                    else {
                        filtered_size++;
                        if (!has_fen || !has_move)
                            ++filtered_size_record;
                    }
                    reset_record();
                }
                else if (!token.empty())
                {
                    record_started = true;
                    ignore_flag_record = true;
                    ++filtered_size_record;
                }
            }
            if (record_started)
            {
                ++filtered_size;
                ++filtered_size_record;
            }
            std::cout << "done " << data_size << " parsed " << filtered_size << " is filtered"
                << " (invalid fen:" << filtered_size_fen << ", illegal/unrepresentable move:"
                << filtered_size_move << ", invalid ply:" << filtered_size_ply
                << ", malformed/incomplete record:" << filtered_size_record << ")" << std::endl;
            ifs.close();
        }
        fs.flush();
        if (!fs)
            output_file_error(output_file_name, "write failed");
        fs.close();
        if (!fs)
            output_file_error(output_file_name, "close failed");
        if (total_data_size == 0)
        {
            std::remove(output_file_name.c_str());
            conversion_error("convert_bin produced no valid records.");
        }
        std::cout << "all done" << std::endl;
    }

    static inline void ltrim(std::string& s) {
        s.erase(s.begin(), std::find_if(s.begin(), s.end(), [](int ch) {
            return !std::isspace(ch);
            }));
    }

    static inline void rtrim(std::string& s) {
        s.erase(std::find_if(s.rbegin(), s.rend(), [](int ch) {
            return !std::isspace(ch);
            }).base(), s.end());
    }

    static inline void trim(std::string& s) {
        ltrim(s);
        rtrim(s);
    }

    int parse_game_result_from_pgn_extract(std::string result) {
        // White Win
        if (result == "\"1-0\"") {
            return 1;
        }
        // Black Win
        else if (result == "\"0-1\"") {
            return -1;
        }
        // Draw
        else {
            return 0;
        }
    }

    // 0.25 -->  0.25 * PawnValueEg
    // #-4  --> -mate_in(4)
    // #3   -->  mate_in(3)
    // -M4  --> -mate_in(4)
    // +M3  -->  mate_in(3)
    Value parse_score_from_pgn_extract(std::string eval, bool& success) {
        success = true;

        if (eval.substr(0, 1) == "#") {
            if (eval.substr(1, 1) == "-") {
                return -mate_in(stoi(eval.substr(2, eval.length() - 2)));
            }
            else {
                return mate_in(stoi(eval.substr(1, eval.length() - 1)));
            }
        }
        else if (eval.substr(0, 2) == "-M") {
            //std::cout << "eval=" << eval << std::endl;
            return -mate_in(stoi(eval.substr(2, eval.length() - 2)));
        }
        else if (eval.substr(0, 2) == "+M") {
            //std::cout << "eval=" << eval << std::endl;
            return mate_in(stoi(eval.substr(2, eval.length() - 2)));
        }
        else {
            char* endptr;
            double value = strtod(eval.c_str(), &endptr);

            if (*endptr != '\0') {
                success = false;
                return VALUE_ZERO;
            }
            else {
                return Value(value * static_cast<double>(PawnValueEg));
            }
        }
    }

    // for Debug
    //#define DEBUG_CONVERT_BIN_FROM_PGN_EXTRACT

    bool is_like_fen(std::string fen) {
        int count_space = std::count(fen.cbegin(), fen.cend(), ' ');
        int count_slash = std::count(fen.cbegin(), fen.cend(), '/');

#if defined(DEBUG_CONVERT_BIN_FROM_PGN_EXTRACT)
        //std::cout << "count_space=" << count_space << std::endl;
        //std::cout << "count_slash=" << count_slash << std::endl;
#endif

        return count_space == 5 && count_slash == 7;
    }

    void convert_bin_from_pgn_extract(
        const vector<string>& filenames,
        const string& output_file_name,
        const bool pgn_eval_side_to_move,
        const bool convert_no_eval_fens_as_score_zero)
    {
        require_legacy_non_chess960();
        std::cout << "pgn_eval_side_to_move=" << pgn_eval_side_to_move << std::endl;
        std::cout << "convert_no_eval_fens_as_score_zero=" << convert_no_eval_fens_as_score_zero << std::endl;

        if (!input_files_are_readable(filenames, "convert_bin_from_pgn_extract"))
            return;

        auto th = Threads.main();
        auto& pos = th->rootPos;

        std::fstream ofs;
        open_new_output_file_or_exit(ofs, output_file_name, ios::binary);

        int game_count = 0;
        int fen_count = 0;

        for (auto filename : filenames) {
            std::cout << now_string() << " convert " << filename << std::endl;
            ifstream ifs;
            ifs.open(filename);

            int game_result = 0;

            std::string line;
            while (std::getline(ifs, line)) {

                if (line.empty()) {
                    continue;
                }

                else if (line.substr(0, 1) == "[") {
                    std::regex pattern_result(R"(\[Result (.+?)\])");
                    std::smatch match;

                    // example: [Result "1-0"]
                    if (std::regex_search(line, match, pattern_result)) {
                        game_result = parse_game_result_from_pgn_extract(match.str(1));
#if defined(DEBUG_CONVERT_BIN_FROM_PGN_EXTRACT)
                        std::cout << "game_result=" << game_result << std::endl;
#endif
                        game_count++;
                        if (game_count % 10000 == 0) {
                            std::cout << now_string() << " game_count=" << game_count << ", fen_count=" << fen_count << std::endl;
                        }
                    }

                    continue;
                }

                else {
                    int gamePly = 1;
                    auto itr = line.cbegin();

                    while (true) {
                        gamePly++;

                        PackedSfenValue psv;
                        memset((char*)&psv, 0, sizeof(PackedSfenValue));

                        // fen
                        {
                            bool fen_found = false;

                            while (!fen_found) {
                                std::regex pattern_bracket(R"(\{(.+?)\})");
                                std::smatch match;
                                if (!std::regex_search(itr, line.cend(), match, pattern_bracket)) {
                                    break;
                                }

                                itr += match.position(0) + match.length(0) - 1;
                                std::string str_fen = match.str(1);
                                trim(str_fen);

                                if (is_like_fen(str_fen)) {
                                    fen_found = true;

                                    StateInfo si;
                                    pos.set(pos.variant(), str_fen, false, &si, th);
                                    pos.sfen_pack(psv.sfen);
                                }

#if defined(DEBUG_CONVERT_BIN_FROM_PGN_EXTRACT)
                                std::cout << "str_fen=" << str_fen << std::endl;
                                std::cout << "fen_found=" << fen_found << std::endl;
#endif
                            }

                            if (!fen_found) {
                                break;
                            }
                        }

                        // move
                        {
                            std::regex pattern_move(R"(\}(.+?)\{)");
                            std::smatch match;
                            if (!std::regex_search(itr, line.cend(), match, pattern_move)) {
                                break;
                            }

                            itr += match.position(0) + match.length(0) - 1;
                            std::string str_move = match.str(1);
                            trim(str_move);
#if defined(DEBUG_CONVERT_BIN_FROM_PGN_EXTRACT)
                            std::cout << "str_move=" << str_move << std::endl;
#endif
                            const Move move = UCI::to_move(pos, str_move);
                            std::uint16_t encoded = 0;
                            if (!try_encode_legacy_move(move, encoded))
                                break;
                            psv.move = encoded;
                        }

                        // eval
                        bool eval_found = false;
                        {
                            std::regex pattern_bracket(R"(\{(.+?)\})");
                            std::smatch match;
                            if (!std::regex_search(itr, line.cend(), match, pattern_bracket)) {
                                break;
                            }

                            std::string str_eval_clk = match.str(1);
                            trim(str_eval_clk);
#if defined(DEBUG_CONVERT_BIN_FROM_PGN_EXTRACT)
                            std::cout << "str_eval_clk=" << str_eval_clk << std::endl;
#endif

                            // example: { [%eval 0.25] [%clk 0:10:00] }
                            // example: { [%eval #-4] [%clk 0:10:00] }
                            // example: { [%eval #3] [%clk 0:10:00] }
                            // example: { +0.71/22 1.2s }
                            // example: { -M4/7 0.003s }
                            // example: { M3/245 0.017s }
                            // example: { +M1/245 0.010s, White mates }
                            // example: { 0.60 }
                            // example: { book }
                            // example: { rnbqkb1r/pp3ppp/2p1pn2/3p4/2PP4/2N2N2/PP2PPPP/R1BQKB1R w KQkq - 0 5 }

                            // Considering the absence of eval
                            if (!is_like_fen(str_eval_clk)) {
                                itr += match.position(0) + match.length(0) - 1;

                                if (str_eval_clk != "book") {
                                    std::regex pattern_eval1(R"(\[\%eval (.+?)\])");
                                    std::regex pattern_eval2(R"((.+?)\/)");

                                    std::string str_eval;
                                    if (std::regex_search(str_eval_clk, match, pattern_eval1) ||
                                        std::regex_search(str_eval_clk, match, pattern_eval2)) {
                                        str_eval = match.str(1);
                                        trim(str_eval);
                                    }
                                    else {
                                        str_eval = str_eval_clk;
                                    }

                                    bool success = false;
                                    Value value = parse_score_from_pgn_extract(str_eval, success);
                                    if (success) {
                                        eval_found = true;
                                        psv.score = std::clamp(value, -VALUE_MATE, VALUE_MATE);
                                    }

#if defined(DEBUG_CONVERT_BIN_FROM_PGN_EXTRACT)
                                    std::cout << "str_eval=" << str_eval << std::endl;
                                    std::cout << "success=" << success << ", psv.score=" << psv.score << std::endl;
#endif
                                }
                            }
                        }

                        // write
                        if ((eval_found || convert_no_eval_fens_as_score_zero)
                            && gamePly <= std::numeric_limits<std::uint16_t>::max()) {
                            if (!eval_found && convert_no_eval_fens_as_score_zero) {
                                psv.score = 0;
                            }

                            psv.gamePly = gamePly;
                            psv.game_result = game_result;

                            if (pos.side_to_move() == BLACK) {
                                if (!pgn_eval_side_to_move) {
                                    psv.score *= -1;
                                }
                                psv.game_result *= -1;
                            }

                            ofs.write((char*)&psv, sizeof(PackedSfenValue));
                            if (!ofs)
                                output_file_error(output_file_name, "write failed");

                            fen_count++;
                        }
                    }

                    game_result = 0;
                }
            }
        }

        std::cout << now_string() << " game_count=" << game_count << ", fen_count=" << fen_count << std::endl;
        ofs.flush();
        if (!ofs)
            output_file_error(output_file_name, "write failed");
        ofs.close();
        if (!ofs)
            output_file_error(output_file_name, "close failed");
        if (fen_count == 0)
        {
            std::remove(output_file_name.c_str());
            conversion_error("convert_bin_from_pgn_extract produced no valid records.");
        }
        std::cout << now_string() << " all done" << std::endl;
    }

    void convert_plain(
        const vector<string>& filenames,
        const string& output_file_name)
    {
        require_legacy_non_chess960();
        if (!input_files_are_readable(filenames, "convert_plain")
            || !legacy_bin_inputs_are_well_sized(filenames))
            return;

        Position tpos;
        std::ofstream ofs;
        open_new_output_file_or_exit(ofs, output_file_name, ios::out);
        auto th = Threads.main();
        for (auto filename : filenames) {
            std::cout << "convert " << filename << " ... ";

            // Just convert packedsfenvalue to text
            std::fstream fs;
            fs.open(filename, ios::in | ios::binary);
            PackedSfenValue p{};
            while (true)
            {
                if (fs.read((char*)&p, sizeof(PackedSfenValue))) {
                    StateInfo si;
                    tpos.set_from_packed_sfen(p.sfen, &si, th);

                    // write as plain text
                    ofs << "fen " << tpos.fen() << std::endl;
                    ofs << "move " << UCI::move(tpos, decode_legacy_move(p.move)) << std::endl;
                    ofs << "score " << p.score << std::endl;
                    ofs << "ply " << int(p.gamePly) << std::endl;
                    ofs << "result " << int(p.game_result) << std::endl;
                    ofs << "e" << std::endl;
                }
                else {
                    break;
                }
            }
            fs.close();
            std::cout << "done" << std::endl;
        }
        ofs.flush();
        if (!ofs)
            output_file_error(output_file_name, "write failed");
        ofs.close();
        if (!ofs)
            output_file_error(output_file_name, "close failed");
        std::cout << "all done" << std::endl;
    }

    void convert_epd(
        const vector<string>& filenames,
        const string& output_file_name)
    {
        require_legacy_non_chess960();
        if (!input_files_are_readable(filenames, "convert_epd")
            || !legacy_bin_inputs_are_well_sized(filenames))
            return;

        Position tpos;
        std::ofstream ofs;
        open_new_output_file_or_exit(ofs, output_file_name, ios::out);
        auto th = Threads.main();
        for (auto filename : filenames) {
            std::cout << "convert " << filename << " ... ";

            // Convert packedsfenvalue to EPD format (FEN only)
            std::fstream fs;
            fs.open(filename, ios::in | ios::binary);
            PackedSfenValue p{};
            while (true)
            {
                if (fs.read((char*)&p, sizeof(PackedSfenValue))) {
                    StateInfo si;
                    tpos.set_from_packed_sfen(p.sfen, &si, th);

                    // write only FEN (EPD format)
                    ofs << tpos.fen() << std::endl;
                }
                else {
                    break;
                }
            }
            fs.close();
            std::cout << "done" << std::endl;
        }
        ofs.flush();
        if (!ofs)
            output_file_error(output_file_name, "write failed");
        ofs.close();
        if (!ofs)
            output_file_error(output_file_name, "close failed");
        std::cout << "all done" << std::endl;
    }

    void convert(istringstream&)
    {
        std::cerr << "ERROR: The 'convert' command has been removed. Please use 'convert_bin' or 'convert_plain' instead.\n";
        std::cerr << "Usage:\n";
        std::cerr << "  convert_bin targetfile <file> output_file_name <output>\n";
        std::cerr << "  convert_plain targetfile <file> output_file_name <output>\n";
    }

    static void append_files_from_dir(
        std::vector<std::string>& filenames,
        const std::string& base_dir,
        const std::string& target_dir)
    {
        string kif_base_dir = Path::combine(base_dir, target_dir);

        sys::path p(kif_base_dir); // Origin of enumeration
        std::for_each(sys::directory_iterator(p), sys::directory_iterator(),
            [&](const sys::path& path) {
                if (sys::is_regular_file(path))
                    filenames.push_back(Path::combine(target_dir, path.filename().generic_string()));
            });
    }

    static void rebase_files(
        std::vector<std::string>& filenames,
        const std::string& base_dir)
    {
        for (auto& file : filenames)
        {
            file = Path::combine(base_dir, file);
        }
    }

    void convert_bin_from_pgn_extract(std::istringstream& is)
    {
        std::vector<std::string> filenames;

        string base_dir;
        string target_dir;

        bool pgn_eval_side_to_move = false;
        bool convert_no_eval_fens_as_score_zero = false;

        string output_file_name = "shuffled_sfen.bin";

        while (true)
        {
            string option;
            is >> option;

            if (option == "")
                break;

            if (option == "targetdir") is >> target_dir;
            else if (option == "targetfile")
            {
                std::string filename;
                is >> filename;
                filenames.push_back(filename);
            }

            else if (option == "basedir")   is >> base_dir;

            else if (option == "pgn_eval_side_to_move") is >> pgn_eval_side_to_move;
            else if (option == "convert_no_eval_fens_as_score_zero") is >> convert_no_eval_fens_as_score_zero;
            else if (option == "output_file_name") is >> output_file_name;
            else
            {
                conversion_error("Unknown convert_bin_from_pgn_extract option: " + option);
            }
        }

        if (!target_dir.empty())
        {
            append_files_from_dir(filenames, base_dir, target_dir);
        }
        rebase_files(filenames, base_dir);

        Eval::NNUE::init();

        cout << "convert_bin_from_pgn-extract.." << endl;
        convert_bin_from_pgn_extract(
            filenames,
            output_file_name,
            pgn_eval_side_to_move,
            convert_no_eval_fens_as_score_zero);
    }

    void convert_bin(std::istringstream& is)
    {
        std::vector<std::string> filenames;

        string base_dir;
        string target_dir;

        int ply_minimum = 0;
        int ply_maximum = std::numeric_limits<std::uint16_t>::max();
        bool interpolate_eval = 0;
        bool check_invalid_fen = true;
        bool check_illegal_move = true;

        bool pgn_eval_side_to_move = false;
        bool convert_no_eval_fens_as_score_zero = false;

        double src_score_min_value = 0.0;
        double src_score_max_value = 1.0;
        double dest_score_min_value = 0.0;
        double dest_score_max_value = 1.0;

        string output_file_name = "shuffled_sfen.bin";

        while (true)
        {
            string option;
            is >> option;

            if (option == "")
                break;

            if (option == "targetdir") is >> target_dir;
            else if (option == "targetfile")
            {
                std::string filename;
                is >> filename;
                filenames.push_back(filename);
            }

            else if (option == "basedir")   is >> base_dir;

            else if (option == "ply_minimum") is >> ply_minimum;
            else if (option == "ply_maximum") is >> ply_maximum;
            else if (option == "interpolate_eval") is >> interpolate_eval;
            else if (option == "check_invalid_fen") is >> check_invalid_fen;
            else if (option == "check_illegal_move") is >> check_illegal_move;
            else if (option == "pgn_eval_side_to_move") is >> pgn_eval_side_to_move;
            else if (option == "convert_no_eval_fens_as_score_zero") is >> convert_no_eval_fens_as_score_zero;
            else if (option == "src_score_min_value") is >> src_score_min_value;
            else if (option == "src_score_max_value") is >> src_score_max_value;
            else if (option == "dest_score_min_value") is >> dest_score_min_value;
            else if (option == "dest_score_max_value") is >> dest_score_max_value;
            else if (option == "output_file_name") is >> output_file_name;
            else
            {
                conversion_error("Unknown convert_bin option: " + option);
            }
        }

        if (!target_dir.empty())
        {
            append_files_from_dir(filenames, base_dir, target_dir);
        }
        rebase_files(filenames, base_dir);

        Eval::NNUE::init();

        cout << "convert_bin.." << endl;
            convert_bin(
                filenames,
                output_file_name,
                ply_minimum,
                ply_maximum,
                interpolate_eval,
                src_score_min_value,
                src_score_max_value,
                dest_score_min_value,
                dest_score_max_value,
                check_invalid_fen,
                check_illegal_move
            );
    }

    void convert_plain(std::istringstream& is)
    {
        std::vector<std::string> filenames;

        string base_dir;
        string target_dir;

        string output_file_name = "shuffled_sfen.bin";

        while (true)
        {
            string option;
            is >> option;

            if (option == "")
                break;

            if (option == "targetdir") is >> target_dir;
            else if (option == "targetfile")
            {
                std::string filename;
                is >> filename;
                filenames.push_back(filename);
            }

            else if (option == "basedir")   is >> base_dir;

            else if (option == "output_file_name") is >> output_file_name;
            else
            {
                conversion_error("Unknown convert_plain option: " + option);
            }
        }

        if (!target_dir.empty())
        {
            append_files_from_dir(filenames, base_dir, target_dir);
        }
        rebase_files(filenames, base_dir);

        Eval::NNUE::init();

        cout << "convert_plain.." << endl;
        convert_plain(filenames, output_file_name);
    }

    void convert_epd(std::istringstream& is)
    {
        std::vector<std::string> filenames;

        string base_dir;
        string target_dir;

        string output_file_name = "shuffled_sfen.bin";

        while (true)
        {
            string option;
            is >> option;

            if (option == "")
                break;

            if (option == "targetdir") is >> target_dir;
            else if (option == "targetfile")
            {
                std::string filename;
                is >> filename;
                filenames.push_back(filename);
            }

            else if (option == "basedir")   is >> base_dir;

            else if (option == "output_file_name") is >> output_file_name;
            else
            {
                conversion_error("Unknown convert_epd option: " + option);
            }
        }

        if (!target_dir.empty())
        {
            append_files_from_dir(filenames, base_dir, target_dir);
        }
        rebase_files(filenames, base_dir);

        Eval::NNUE::init();

        cout << "convert_epd.." << endl;
        convert_epd(filenames, output_file_name);
    }
}
