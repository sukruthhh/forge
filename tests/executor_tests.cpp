#include "forge/executor.hpp"
#include <atomic>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <stdexcept>

int main() {
    char pattern[] = "/tmp/forge-executor-test-XXXXXX";
    const auto directory = ::mkdtemp(pattern);
    if (!directory) throw std::runtime_error("cannot create test directory");
    const auto marker = std::filesystem::path(directory) / "must-not-run";
    std::atomic<bool> cancelled{true};
    const auto result = forge::execute({"/usr/bin/touch", marker.string()}, 1, cancelled);
    const bool valid = result.exit_code == 130 && !result.timed_out && !std::filesystem::exists(marker);
    std::filesystem::remove_all(directory);
    if (!valid) throw std::runtime_error("an already-cancelled execution launched a process or reported timeout");
    std::cout << "Executor checks passed: cancellation observed before spawn prevents side effects\n";
}
