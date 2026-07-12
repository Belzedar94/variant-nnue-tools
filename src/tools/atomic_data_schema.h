#ifndef TOOLS_ATOMIC_DATA_SCHEMA_H_INCLUDED
#define TOOLS_ATOMIC_DATA_SCHEMA_H_INCLUDED

#include <cstddef>
#include <string_view>

namespace Stockfish::Tools {

inline constexpr std::string_view AtomicDataSchemaSha256 =
  "758ac9239c2b1cff34cd10e185d9ee1bc7a400e2758bb1ce71171e1a1fa50a78";
inline constexpr std::size_t LegacyAtomicV1RecordSize = 72;

inline constexpr std::string_view atomic_data_schema_json() noexcept
{
    return "{\"schema_sha256\":\"758ac9239c2b1cff34cd10e185d9ee1bc7a400e2758bb1ce71171e1a1fa50a78\","
           "\"formats\":{\"legacy-atomic-v1\":{\"read\":true,\"write\":true,"
           "\"record_size\":72}}}";
}

}  // namespace Stockfish::Tools

#endif  // TOOLS_ATOMIC_DATA_SCHEMA_H_INCLUDED
