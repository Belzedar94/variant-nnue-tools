#ifndef TOOLS_RANDOM_SEED_H_INCLUDED
#define TOOLS_RANDOM_SEED_H_INCLUDED

#include "misc.h"

#include <string>

namespace Stockfish::Tools {

// Resolve an omitted or textual seed exactly once and return the decimal form
// accepted by PRNG. Printing and reusing this value makes a single-threaded
// generation run byte-reproducible with otherwise identical inputs/options.
inline std::string resolve_replayable_seed(const std::string& seed)
{
    PRNG source(seed);
    return std::to_string(source.get_seed());
}

}  // namespace Stockfish::Tools

#endif  // TOOLS_RANDOM_SEED_H_INCLUDED
