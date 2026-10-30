#include "forge/scheduler.hpp"

#include <cstdlib>
#include <iostream>
#include <limits>

namespace {
void check(bool condition, const char* message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::exit(1);
    }
}
}

int main() {
    using namespace forge;
    Worker desktop{"desktop", "linux", "x86_64", {8, 16000}, {0, 0}, true};
    Worker mac{"mac", "darwin", "arm64", {8, 16000}};
    JobRequirements job{"linux", "x86_64", {4, 8000}};
    check(!rejection(desktop, job), "exactly compatible job must fit");
    check(rejection(mac, job).has_value(), "incompatible OS must be rejected");
    auto arm = desktop;
    arm.arch = "arm64";
    check(rejection(arm, job).has_value(), "architecture must match");
    auto busy = desktop;
    busy.reserved = {5, 0};
    check(rejection(busy, job).has_value(), "CPU reservations must count");
    busy.reserved = {0, 8001};
    check(rejection(busy, job).has_value(), "memory reservations must count");
    busy.reserved = {9, 0};
    check(rejection(busy, job).has_value(), "invalid accounting must fail closed");
    job.cuda = true;
    auto cpu_only = desktop;
    cpu_only.cuda = false;
    check(rejection(cpu_only, job).has_value(), "GPU requirement must be enforced");
    job.cuda = false;
    desktop.ready = false;
    check(!select_worker({desktop, mac}, job), "unavailable workers cannot receive jobs");
    desktop.ready = true;
    auto larger = desktop;
    larger.id = "larger";
    larger.capacity = {16, 32000};
    check(select_worker({desktop, larger}, job) == 1, "prefer lower projected dominant utilization");
    auto tie = desktop;
    tie.id = "aaa";
    check(select_worker({desktop, tie}, job) == 1, "ties use stable worker IDs");
    job.resources.cpu = 0;
    check(!select_worker({desktop}, job), "zero CPU request must fail");
    job.resources = {1, std::numeric_limits<std::uint64_t>::max()};
    desktop.reserved.memory_bytes = 1;
    check(!select_worker({desktop}, job), "oversized requests must not overflow");
    check(!select_worker({}, job), "empty cluster must queue jobs");
    std::cout << "Scheduler checks passed\n";
}
