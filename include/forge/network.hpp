#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <openssl/ssl.h>
#include <string>
#include <utility>
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
                                jobs = 10, logs = 11, job_list = 12, authenticate = 13,
                                staged_submit = 14, staged_assignment = 15,
                                worker_capabilities = 16, capability_snapshot = 17,
                                timings = 18, timing_snapshot = 19, profiled_result = 20,
                                retry_submit = 21, retry_staged_submit = 22,
                                attempt_assignment = 23, attempt_staged_assignment = 24,
                                attempt_result = 25, attempts = 26, attempt_snapshot = 27,
                                retry_capabilities = 28, retry_capability_snapshot = 29,
                                job_error = 30 };
struct Message {
    Type type;
    std::vector<std::string> fields;
};
using TlsContext = std::shared_ptr<SSL_CTX>;
TlsContext configured_tls();

class Connection {
public:
    explicit Connection(Socket socket) : socket_(std::move(socket)) {}
    void secure(const TlsContext&, bool server, const std::string& expected_ip = {});
    int get() const { return socket_.get(); }
    SSL* tls() const { return tls_.get(); }
private:
    Socket socket_;
    std::unique_ptr<SSL, decltype(&SSL_free)> tls_{nullptr, SSL_free};
};
constexpr std::uint32_t max_frame = 65536;
constexpr std::size_t max_fields = 1024;
std::vector<std::uint8_t> encode(const Message&);
Message decode(const std::vector<std::uint8_t>& payload);
Socket listen(std::uint16_t port, const std::string& bind, const TlsContext&);
Connection connect(std::uint16_t port, const std::string& host, const TlsContext&);
void send(Connection&, const Message&, std::chrono::milliseconds timeout = std::chrono::seconds(3));
Message receive(Connection&, std::chrono::milliseconds timeout = std::chrono::seconds(3));
}
