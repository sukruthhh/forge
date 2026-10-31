#pragma once
#include <cstdint>
#include <string>
namespace forge {
int coordinator(std::uint16_t port);
int join(std::uint16_t port, const std::string& id);
int cluster(std::uint16_t port);
}
