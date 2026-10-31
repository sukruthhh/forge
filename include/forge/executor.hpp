#pragma once
#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

namespace forge {
struct ExecutionResult {
    int exit_code = 1;
    std::string output;
    std::string error;
    bool timed_out = false;
    bool truncated = false;
};
// Trusted commands only. No shell interpretation or file staging. Runtime limit
// applies to the process group, including descendants keeping output pipes open.
ExecutionResult execute(const std::vector<std::string>& arguments,
                        std::uint32_t timeout_seconds, const std::atomic<bool>& stop);
}
