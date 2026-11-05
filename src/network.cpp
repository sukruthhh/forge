#include "forge/network.hpp"

#include <arpa/inet.h>
#include <algorithm>
#include <cerrno>
#include <climits>
#include <cstring>
#include <cstdlib>
#include <openssl/err.h>
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
void wait_ready(int fd, short events, Clock::time_point deadline) {
    for (;;) {
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
        if (remaining <= 0) throw std::runtime_error("I/O deadline exceeded");
        pollfd p{fd, events, 0};
        const int ready = ::poll(&p, 1, static_cast<int>(std::min<long long>(remaining, INT_MAX)));
        if (ready < 0) { if (errno == EINTR) continue; fail("poll"); }
        if (ready == 0) throw std::runtime_error("I/O deadline exceeded");
        return;
    }
}
void wait_tls(Connection& connection, int result, Clock::time_point deadline) {
    const int error = SSL_get_error(connection.tls(), result);
    if (error == SSL_ERROR_WANT_READ) wait_ready(connection.get(), POLLIN, deadline);
    else if (error == SSL_ERROR_WANT_WRITE) wait_ready(connection.get(), POLLOUT, deadline);
    else throw std::runtime_error("TLS connection failed (certificate verification or peer I/O)");
}
void transfer(Connection& connection, std::uint8_t* bytes, std::size_t size,
              bool writing, Clock::time_point deadline) {
    std::size_t offset = 0;
    while (offset < size) {
        if (Clock::now() >= deadline) throw std::runtime_error("I/O deadline exceeded");
        if (connection.tls()) {
            ERR_clear_error();
            const int count = writing ? SSL_write(connection.tls(), bytes + offset, static_cast<int>(size - offset))
                                      : SSL_read(connection.tls(), bytes + offset, static_cast<int>(size - offset));
            if (count <= 0) { wait_tls(connection, count, deadline); continue; }
            offset += static_cast<std::size_t>(count);
        } else {
            wait_ready(connection.get(), writing ? POLLOUT : POLLIN, deadline);
            const auto count = writing ? ::send(connection.get(), bytes + offset, size - offset, MSG_NOSIGNAL | MSG_DONTWAIT)
                                       : ::recv(connection.get(), bytes + offset, size - offset, MSG_DONTWAIT);
            if (count < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                fail(writing ? "send" : "receive");
            }
            if (count == 0) throw std::runtime_error("peer disconnected");
            offset += static_cast<std::size_t>(count);
        }
    }
}
sockaddr_in address(std::uint16_t port, const std::string& host, const TlsContext& tls) {
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    if (::inet_pton(AF_INET, host.c_str(), &a.sin_addr) != 1)
        throw std::runtime_error("endpoint must be a numeric IPv4 address");
    if ((ntohl(a.sin_addr.s_addr) >> 24) != 127 && !tls)
        throw std::runtime_error("non-loopback endpoints require mutual TLS");
    return a;
}
Socket make_socket() {
    Socket s(::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0));
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

TlsContext configured_tls() {
    const char* ca = std::getenv("FORGE_TLS_CA");
    const char* certificate = std::getenv("FORGE_TLS_CERT");
    const char* key = std::getenv("FORGE_TLS_KEY");
    if (!ca && !certificate && !key) return {};
    if (!ca || !*ca || !certificate || !*certificate || !key || !*key)
        throw std::runtime_error("TLS requires FORGE_TLS_CA, FORGE_TLS_CERT, and FORGE_TLS_KEY");
    TlsContext context(SSL_CTX_new(TLS_method()), SSL_CTX_free);
    if (!context) throw std::runtime_error("cannot create TLS context");
    SSL_CTX_set_default_passwd_cb(context.get(), [](char*, int, int, void*) { return 0; });
    if (SSL_CTX_set_min_proto_version(context.get(), TLS1_3_VERSION) != 1 ||
        SSL_CTX_load_verify_locations(context.get(), ca, nullptr) != 1 ||
        SSL_CTX_use_certificate_chain_file(context.get(), certificate) != 1 ||
        SSL_CTX_use_PrivateKey_file(context.get(), key, SSL_FILETYPE_PEM) != 1 ||
        SSL_CTX_check_private_key(context.get()) != 1)
        throw std::runtime_error("cannot configure TLS trust, certificate, or private key");
    SSL_CTX_set_verify(context.get(), SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT, nullptr);
    SSL_CTX_set_verify_depth(context.get(), 8);
    return context;
}

void Connection::secure(const TlsContext& context, bool server, const std::string& expected_ip) {
    if (!context) return;
    if (tls_) throw std::runtime_error("TLS already configured");
    tls_.reset(SSL_new(context.get()));
    if (!tls_ || SSL_set_fd(tls_.get(), get()) != 1)
        throw std::runtime_error("cannot initialize TLS connection");
    if (!server && X509_VERIFY_PARAM_set1_ip_asc(SSL_get0_param(tls_.get()), expected_ip.c_str()) != 1)
        throw std::runtime_error("cannot configure TLS server IP verification");
    const auto deadline = Clock::now() + std::chrono::seconds(3);
    for (;;) {
        if (Clock::now() >= deadline) throw std::runtime_error("TLS handshake deadline exceeded");
        ERR_clear_error();
        const int result = server ? SSL_accept(tls_.get()) : SSL_connect(tls_.get());
        if (result == 1) break;
        wait_tls(*this, result, deadline);
    }
}

std::vector<std::uint8_t> encode(const Message& message) {
    const auto type = static_cast<std::uint8_t>(message.type);
    if (type < 1 || type > 40 || message.fields.size() > max_fields)
        throw std::runtime_error("invalid message");
    std::vector<std::uint8_t> payload{1, type};
    append32(payload, static_cast<std::uint32_t>(message.fields.size()));
    for (const auto& field : message.fields) {
        if (field.size() > max_frame || payload.size() + 4 + field.size() > max_frame)
            throw std::runtime_error("frame too large");
        append32(payload, static_cast<std::uint32_t>(field.size()));
        payload.insert(payload.end(), field.begin(), field.end());
    }
    std::vector<std::uint8_t> frame(payload.size() + 4);
    const auto length = static_cast<std::uint32_t>(payload.size());
    for (int i = 0; i < 4; ++i) frame[i] = (length >> (24 - i * 8)) & 255;
    std::copy(payload.begin(), payload.end(), frame.begin() + 4);
    return frame;
}

Message decode(const std::vector<std::uint8_t>& payload) {
    if (payload.size() < 6 || payload.size() > max_frame || payload[0] != 1 ||
        payload[1] < 1 || payload[1] > 40) throw std::runtime_error("invalid protocol header");
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

Socket listen(std::uint16_t port, const std::string& bind, const TlsContext& tls) {
    auto s = make_socket();
    int one = 1;
    if (::setsockopt(s.get(), SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) < 0) fail("setsockopt");
    auto a = address(port, bind, tls);
    if (::bind(s.get(), reinterpret_cast<sockaddr*>(&a), sizeof(a)) < 0) fail("bind");
    if (::listen(s.get(), 64) < 0) fail("listen");
    return s;
}

Connection connect(std::uint16_t port, const std::string& host, const TlsContext& tls) {
    auto s = make_socket();
    auto a = address(port, host, tls);
    if (::connect(s.get(), reinterpret_cast<sockaddr*>(&a), sizeof(a)) < 0) {
        if (errno != EINPROGRESS) fail("connect");
        wait_ready(s.get(), POLLOUT, Clock::now() + std::chrono::seconds(3));
        int error = 0;
        socklen_t length = sizeof(error);
        if (::getsockopt(s.get(), SOL_SOCKET, SO_ERROR, &error, &length) < 0) fail("getsockopt");
        if (error) { errno = error; fail("connect"); }
    }
    Connection connection(std::move(s));
    connection.secure(tls, false, host);
    return connection;
}

void send(Connection& connection, const Message& message, std::chrono::milliseconds timeout) {
    auto frame = encode(message);
    transfer(connection, frame.data(), frame.size(), true, Clock::now() + timeout);
}
Message receive(Connection& connection, std::chrono::milliseconds timeout) {
    const auto deadline = Clock::now() + timeout;
    std::vector<std::uint8_t> header(4);
    transfer(connection, header.data(), header.size(), false, deadline);
    std::size_t offset = 0;
    const auto length = read32(header, offset);
    if (length < 6 || length > max_frame) throw std::runtime_error("invalid frame length");
    std::vector<std::uint8_t> payload(length);
    transfer(connection, payload.data(), payload.size(), false, deadline);
    return decode(payload);
}
}
