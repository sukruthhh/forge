#pragma once
#include <cstdint>
#include <string>
#include <vector>
namespace forge {
int coordinator(std::uint16_t port, const std::string& bind);
int join(std::uint16_t port, const std::string& id, const std::string& host);
int cluster(std::uint16_t port, const std::string& host);
int ps(std::uint16_t port, const std::string& host);
int logs(std::uint16_t port, const std::string& id, const std::string& host);
int run(std::uint16_t port, std::uint32_t cpu, std::uint64_t memory,
        std::uint32_t timeout, const std::vector<std::string>& arguments,
        const std::string& host, const std::string& architecture);
}
