#pragma once
#include <cstdint>
#include <string>
#include <vector>
namespace forge {
int coordinator(std::uint16_t port);
int join(std::uint16_t port, const std::string& id);
int cluster(std::uint16_t port);
int ps(std::uint16_t port);
int logs(std::uint16_t port, const std::string& id);
int run(std::uint16_t port, std::uint32_t cpu, std::uint64_t memory,
        std::uint32_t timeout, const std::vector<std::string>& arguments);
}
