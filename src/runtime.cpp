#include "forge/runtime.hpp"
#include "forge/network.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <csignal>
#include <iostream>
#include <map>
#include <mutex>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/sysinfo.h>
#include <sys/utsname.h>
#include <thread>
#include <unistd.h>

namespace forge {
namespace {
using Clock = std::chrono::steady_clock;
using net::Message;
using net::Type;
static_assert(std::atomic<bool>::is_always_lock_free);
std::atomic<bool> stopped{false};
void stop(int) { stopped.store(true, std::memory_order_relaxed); }
void install_signals() {
    stopped = 0;
    std::signal(SIGINT, stop);
    std::signal(SIGTERM, stop);
}
std::uint64_t number(const std::string& value) {
    std::uint64_t result = 0;
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size())
        throw std::runtime_error("invalid resource number");
    return result;
}
bool label(const std::string& value) {
    return !value.empty() && value.size() <= 64 &&
        std::all_of(value.begin(), value.end(), [](unsigned char c) {
            return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                   (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
        });
}
struct Record {
    std::vector<std::string> capabilities;
    Clock::time_point last_seen;
    bool connected = true;
};
struct Registry {
    std::mutex mutex;
    std::map<std::string, Record> workers;
};
Message snapshot(Registry& registry) {
    std::lock_guard lock(registry.mutex);
    Message result{Type::snapshot, {}};
    for (const auto& [id, record] : registry.workers) {
        const auto age = Clock::now() - record.last_seen;
        const std::string state = !record.connected || age >= std::chrono::seconds(2) ? "DEAD" :
            age >= std::chrono::seconds(1) ? "SUSPECT" : "READY";
        result.fields.insert(result.fields.end(), record.capabilities.begin(), record.capabilities.end());
        result.fields.push_back(state);
    }
    return result;
}
void handle(net::Socket socket, Registry& registry) {
    std::string registered;
    try {
        auto message = net::receive(socket.get());
        if (message.type == Type::cluster && message.fields.empty()) {
            net::send(socket.get(), snapshot(registry));
            return;
        }
        if (message.type != Type::register_worker || message.fields.size() != 6)
            throw std::runtime_error("expected registration or cluster request");
        const auto& f = message.fields;
        if (!label(f[0]) || !label(f[1]) || !label(f[2]) ||
            number(f[3]) == 0 || number(f[3]) > 1048576 || number(f[4]) == 0 ||
            (f[5] != "0" && f[5] != "1")) throw std::runtime_error("invalid worker capabilities");
        {
            std::lock_guard lock(registry.mutex);
            auto existing = registry.workers.find(f[0]);
            // Do not replace a live session: its cleanup could otherwise mark
            // the replacement disconnected. Reuse IDs only after session close.
            if (existing != registry.workers.end() && existing->second.connected)
                throw std::runtime_error("worker ID already connected");
            if (existing == registry.workers.end() && registry.workers.size() >= 64)
                throw std::runtime_error("worker registry full");
            registry.workers.insert_or_assign(f[0], Record{f, Clock::now(), true});
            registered = f[0];
        }
        net::send(socket.get(), {Type::ok, {}});
        while (!stopped) {
            message = net::receive(socket.get(), std::chrono::seconds(2));
            if (message.type != Type::heartbeat || !message.fields.empty())
                throw std::runtime_error("expected heartbeat");
            {
                std::lock_guard lock(registry.mutex);
                registry.workers.at(registered).last_seen = Clock::now();
            }
            net::send(socket.get(), {Type::ok, {}});
        }
    } catch (const std::exception& error) {
        try { net::send(socket.get(), {Type::error, {error.what()}}, std::chrono::milliseconds(100)); }
        catch (...) {} // A disconnected client must not terminate the server.
    }
    if (!registered.empty()) {
        std::lock_guard lock(registry.mutex);
        registry.workers.at(registered).connected = false;
    }
}
void expect_ok(const Message& message) {
    if (message.type == Type::error && message.fields.size() == 1)
        throw std::runtime_error(message.fields.front());
    if (message.type != Type::ok || !message.fields.empty())
        throw std::runtime_error("unexpected coordinator response");
}
}

int coordinator(std::uint16_t port) {
    install_signals();
    auto listener = net::listen_loopback(port);
    Registry registry;
    struct Slot { std::atomic<bool> done{true}; std::jthread thread; };
    // Fixed slots bound threads and retained thread objects. Destruction joins
    // every handler before registry destruction.
    std::array<Slot, 96> slots;
    std::cout << "Coordinator listening on 127.0.0.1:" << port << std::endl;
    while (!stopped) {
        pollfd p{listener.get(), POLLIN, 0};
        const int ready = ::poll(&p, 1, 100);
        if (ready < 0) {
            if (errno == EINTR) continue;
            stopped = true;
            throw std::runtime_error("accept poll failed");
        }
        if (ready == 0) continue;
        net::Socket peer(::accept4(listener.get(), nullptr, nullptr, SOCK_CLOEXEC));
        if (peer.get() < 0) continue;
        auto slot = std::find_if(slots.begin(), slots.end(), [](const Slot& s) { return s.done.load(); });
        if (slot == slots.end()) continue;
        if (slot->thread.joinable()) slot->thread.join();
        slot->done = false;
        try {
            slot->thread = std::jthread([socket = std::move(peer), &registry, &done = slot->done]() mutable {
                handle(std::move(socket), registry);
                done = true;
            });
        } catch (...) {
            stopped = true;
            throw;
        }
    }
    return 0;
}

int join(std::uint16_t port, const std::string& id) {
    if (!label(id)) throw std::runtime_error("worker ID must be 1-64 letters, digits, dots, underscores or hyphens");
    install_signals();
    utsname system{};
    struct sysinfo memory{};
    const auto cpus = ::sysconf(_SC_NPROCESSORS_ONLN);
    if (::uname(&system) != 0 || ::sysinfo(&memory) != 0 || cpus <= 0)
        throw std::runtime_error("capability discovery failed");
    auto socket = net::connect_loopback(port);
    net::send(socket.get(), {Type::register_worker,
        {id, "linux", system.machine, std::to_string(cpus),
         std::to_string(static_cast<std::uint64_t>(memory.totalram) * memory.mem_unit), "0"}});
    expect_ok(net::receive(socket.get()));
    std::cout << "Worker " << id << " registered (no job execution yet)" << std::endl;
    while (!stopped) {
        for (int i = 0; i < 5 && !stopped; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
        if (stopped) break;
        net::send(socket.get(), {Type::heartbeat, {}});
        expect_ok(net::receive(socket.get()));
    }
    return 0;
}

int cluster(std::uint16_t port) {
    auto socket = net::connect_loopback(port);
    net::send(socket.get(), {Type::cluster, {}});
    auto result = net::receive(socket.get());
    if (result.type != Type::snapshot || result.fields.size() % 7 != 0)
        throw std::runtime_error("invalid cluster snapshot");
    std::cout << "ID\tOS\tARCH\tCPU\tRAM_BYTES\tCUDA\tSTATE\n";
    for (std::size_t i = 0; i < result.fields.size(); ++i)
        std::cout << result.fields[i] << ((i + 1) % 7 == 0 ? '\n' : '\t');
    return 0;
}
}
