#pragma once

#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

namespace forge::net {
class Socket {
public:
    explicit Socket(int fd = -1) : fd_(fd) {}
    ~Socket();
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    Socket(Socket&& other) noexcept;
    Socket& operator=(Socket&& other) noexcept;
    int get() const { return fd_; }
private:
    int fd_;
};

enum class Type : std::uint8_t { register_worker = 1, heartbeat = 2, cluster = 3,
                                ok = 4, snapshot = 5, error = 6,
                                submit = 7, assignment = 8, result = 9,
                                jobs = 10, logs = 11, job_list = 12, authenticate = 13 };
struct Message {
    Type type;
    std::vector<std::string> fields;
};
constexpr std::uint32_t max_frame = 65536;
constexpr std::size_t max_fields = 1024;
std::vector<std::uint8_t> encode(const Message&);
Message decode(const std::vector<std::uint8_t>& payload);
Socket listen_loopback(std::uint16_t port);
Socket connect_loopback(std::uint16_t port);
void send(int fd, const Message&, std::chrono::milliseconds timeout = std::chrono::seconds(3));
Message receive(int fd, std::chrono::milliseconds timeout = std::chrono::seconds(3));
}
