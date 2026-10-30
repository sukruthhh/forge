#include "forge/scheduler.hpp"

#include <algorithm>
#include <limits>

namespace forge {
std::optional<std::string> rejection(const Worker& w, const JobRequirements& j) {
    if (j.resources.cpu == 0) return "CPU request must be positive";
    if (!w.ready) return "worker is unavailable";
    if (!j.os.empty() && j.os != w.os) return "OS mismatch";
    if (!j.arch.empty() && j.arch != w.arch) return "architecture mismatch";
    if (j.cuda && !w.cuda) return "CUDA required";
    // Validate before subtracting to avoid unsigned underflow.
    if (w.reserved.cpu > w.capacity.cpu ||
        w.reserved.memory_bytes > w.capacity.memory_bytes)
        return "invalid worker reservations";
    if (j.resources.cpu > w.capacity.cpu - w.reserved.cpu)
        return "insufficient CPU";
    if (j.resources.memory_bytes > w.capacity.memory_bytes - w.reserved.memory_bytes)
        return "insufficient memory";
    return std::nullopt;
}

std::optional<std::size_t> select_worker(const std::vector<Worker>& workers,
                                       const JobRequirements& job) {
    std::optional<std::size_t> best;
    double best_score = std::numeric_limits<double>::infinity();
    for (std::size_t i = 0; i < workers.size(); ++i) {
        const auto& w = workers[i];
        if (rejection(w, job)) continue;
        const double cpu = static_cast<double>(w.reserved.cpu + job.resources.cpu) /
                           w.capacity.cpu;
        const double memory = w.capacity.memory_bytes == 0 ? 0.0 :
            static_cast<double>(w.reserved.memory_bytes + job.resources.memory_bytes) /
            static_cast<double>(w.capacity.memory_bytes);
        const double score = std::max(cpu, memory);
        if (!best || score < best_score ||
            (score == best_score && w.id < workers[*best].id)) {
            best = i;
            best_score = score;
        }
    }
    return best;
}
}
