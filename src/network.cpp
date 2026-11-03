#include "forge/network.hpp"

#include <arpa/inet.h>
#include <cerrno>
#include <climits>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>
#include <utility>

namespace forge::net {
namespace {
using Clock = std::chrono::steady_clock;
[[noreturn]] void fail(const char* operation) {
    throw std::runtime_error(std::string(operation) + ": " + std::strerror(errno));
}
void append32(std::vector<std::uint8_t>& out, std::uint32_t value) {
    for (int shift = 24; shift >= 0; shift -= 8) out.push_back((value >> shift) & 255);
}
std::uint32_t read32(const std::vector<std::uint8_t>& in, std::size_t& offset) {
    if (offset > in.size() || in.size() - offset < 4) throw std::runtime_error("truncated integer");
    std::uint32_t value = 0;
    for (int i = 0; i < 4; ++i) value = (value << 8) | in[offset++];
    return value;
}
void transfer(int fd, std::uint8_t* bytes, std::size_t size, bool writing, Clock::time_point deadline) {
    std::size_t offset = 0;
    while (offset < size) {
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
        if (remaining <= 0) throw std::runtime_error("I/O deadline exceeded");
        pollfd p{fd, static_cast<short>(writing ? POLLOUT : POLLIN), 0};
        const int ready = ::poll(&p, 1, static_cast<int>(remaining > INT_MAX ? INT_MAX : remaining));
        if (ready < 0) { if (errno == EINTR) continue; fail("poll"); }
        if (ready == 0) throw std::runtime_error("I/O deadline exceeded");
        const auto count = writing ? ::send(fd, bytes + offset, size - offset, MSG_NOSIGNAL | MSG_DONTWAIT)
                                   : ::recv(fd, bytes + offset, size - offset, MSG_DONTWAIT);
        if (count < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            fail(writing ? "send" : "receive");
        }
        if (count == 0) throw std::runtime_error("peer disconnected");
        offset += static_cast<std::size_t>(count);
    }
}
sockaddr_in address(std::uint16_t port) {
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    return a;
}
Socket make_socket() {
    Socket s(::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0));
    if (s.get() < 0) fail("socket");
    return s;
}
}

Socket::~Socket() { if (fd_ >= 0) ::close(fd_); }
Socket::Socket(Socket&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
Socket& Socket::operator=(Socket&& other) noexcept {
    if (this != &other) {
        if (fd_ >= 0) ::close(fd_);
        fd_ = std::exchange(other.fd_, -1);
    }
    return *this;
}

std::vector<std::uint8_t> encode(const Message& message) {
    const auto type = static_cast<std::uint8_t>(message.type);
    if (type < 1 || type > 13 || message.fields.size() > max_fields)
        throw std::runtime_error("invalid message");
    std::vector<std::uint8_t> payload{1, type};
    append32(payload, static_cast<std::uint32_t>(message.fields.size()));
    for (const auto& field : message.fields) {
        if (field.size() > max_frame || payload.size() + 4 + field.size() > max_frame)
            throw std::runtime_error("frame too large");
        append32(payload, static_cast<std::uint32_t>(field.size()));
        payload.insert(payload.end(), field.begin(), field.end());
    }
    std::vector<std::uint8_t> frame;
    append32(frame, static_cast<std::uint32_t>(payload.size()));
    frame.insert(frame.end(), payload.begin(), payload.end());
    return frame;
}

Message decode(const std::vector<std::uint8_t>& payload) {
    if (payload.size() < 6 || payload.size() > max_frame || payload[0] != 1 ||
        payload[1] < 1 || payload[1] > 13) throw std::runtime_error("invalid protocol header");
    Message message{static_cast<Type>(payload[1]), {}};
    std::size_t offset = 2;
    const auto count = read32(payload, offset);
    if (count > max_fields) throw std::runtime_error("too many fields");
    for (std::uint32_t i = 0; i < count; ++i) {
        const auto length = read32(payload, offset);
        if (length > payload.size() - offset) throw std::runtime_error("truncated field");
        message.fields.emplace_back(reinterpret_cast<const char*>(payload.data() + offset), length);
        offset += length;
    }
    if (offset != payload.size()) throw std::runtime_error("trailing frame data");
    return message;
}

Socket listen_loopback(std::uint16_t port) {
    auto s = make_socket();
    int one = 1;
    if (::setsockopt(s.get(), SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) < 0) fail("setsockopt");
    auto a = address(port);
    if (::bind(s.get(), reinterpret_cast<sockaddr*>(&a), sizeof(a)) < 0) fail("bind");
    if (::listen(s.get(), 64) < 0) fail("listen");
    return s;
}

Socket connect_loopback(std::uint16_t port) {
    auto s = make_socket();
    const auto flags = ::fcntl(s.get(), F_GETFL, 0);
    if (flags < 0 || ::fcntl(s.get(), F_SETFL, flags | O_NONBLOCK) < 0) fail("fcntl");
    auto a = address(port);
    if (::connect(s.get(), reinterpret_cast<sockaddr*>(&a), sizeof(a)) < 0) {
        if (errno != EINPROGRESS) fail("connect");
        pollfd p{s.get(), POLLOUT, 0};
        if (::poll(&p, 1, 3000) <= 0) throw std::runtime_error("connection deadline exceeded");
        int error = 0;
        socklen_t length = sizeof(error);
        if (::getsockopt(s.get(), SOL_SOCKET, SO_ERROR, &error, &length) < 0) fail("getsockopt");
        if (error) { errno = error; fail("connect"); }
    }
    return s;
}

void send(int fd, const Message& message, std::chrono::milliseconds timeout) {
    auto frame = encode(message);
    transfer(fd, frame.data(), frame.size(), true, Clock::now() + timeout);
}
Message receive(int fd, std::chrono::milliseconds timeout) {
    const auto deadline = Clock::now() + timeout;
    std::vector<std::uint8_t> header(4);
    transfer(fd, header.data(), header.size(), false, deadline);
    std::size_t offset = 0;
    const auto length = read32(header, offset);
    if (length < 6 || length > max_frame) throw std::runtime_error("invalid frame length");
    std::vector<std::uint8_t> payload(length);
    transfer(fd, payload.data(), payload.size(), false, deadline);
    return decode(payload);
}
}
