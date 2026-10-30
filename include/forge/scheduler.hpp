#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace forge {
struct Resources {
    std::uint32_t cpu = 1;
    std::uint64_t memory_bytes = 0;
};

struct Worker {
    std::string id;
    std::string os;
    std::string arch;
    Resources capacity;
    Resources reserved{0, 0};
    bool cuda = false;
    bool ready = true;
};

struct JobRequirements {
    std::string os;
    std::string arch;
    Resources resources;
    bool cuda = false;
};

// Returns an actionable reason when placement is impossible.
std::optional<std::string> rejection(const Worker&, const JobRequirements&);

// Deterministic resource-aware placement. The caller reserves the selected
// resources before selecting another job; this function does not mutate state.
std::optional<std::size_t> select_worker(const std::vector<Worker>&,
                                       const JobRequirements&);
}
