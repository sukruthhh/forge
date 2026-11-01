#include "forge/runtime.hpp"
#include "forge/network.hpp"
#include "forge/executor.hpp"
#include "forge/scheduler.hpp"
#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <csignal>
#include <condition_variable>
#include <deque>
#include <future>
#include <iostream>
#include <map>
#include <mutex>
#include <memory>
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
struct Job {
    std::string id;
    JobRequirements requirements;
    std::uint32_t timeout;
    std::vector<std::string> arguments;
    std::string state = "QUEUED";
    std::string worker;
    Clock::time_point assigned_at{};
    Message completion{Type::error, {"job unfinished"}};
    bool terminal = false;
};
struct Record {
    std::vector<std::string> capabilities;
    Clock::time_point last_seen;
    bool connected = true;
    std::shared_ptr<Job> job;
    Resources reserved{0, 0};
};
struct Registry {
    std::mutex mutex;
    std::map<std::string, Record> workers;
    std::map<std::string, std::shared_ptr<Job>> jobs;
    std::deque<std::shared_ptr<Job>> history;
    std::condition_variable changed;
    std::uint64_t next_job = 1;
};
void finish(Registry& registry, Record& record, Message result) {
    if (!record.job) return;
    if (!record.job->terminal) {
        record.job->completion = std::move(result);
        record.job->terminal = true;
        const auto& completion = record.job->completion;
        record.job->state = completion.type == Type::error ? "LOST" :
            completion.fields[4] == "1" ? "TIMED_OUT" :
            completion.fields[1] == "0" ? "SUCCEEDED" : "FAILED";
    }
    record.job.reset();
    record.reserved = {0, 0};
    registry.changed.notify_all();
}
void schedule(Registry& registry) {
    // Caller holds the registry lock through selection AND reservation.
    for (auto& [id, job] : registry.jobs) {
        if (job->state != "QUEUED" || job->terminal) continue;
        std::vector<Worker> candidates;
        for (const auto& [worker_id, record] : registry.workers) {
            const auto& c = record.capabilities;
            candidates.push_back({worker_id, c[1], c[2],
                {static_cast<std::uint32_t>(number(c[3])), number(c[4])}, record.reserved,
                c[5] == "1", record.connected && !record.job &&
                    Clock::now() - record.last_seen < std::chrono::seconds(1)});
        }
        auto selected = select_worker(candidates, job->requirements);
        if (!selected) continue;
        auto& record = registry.workers.at(candidates[*selected].id);
        job->worker = candidates[*selected].id;
        record.job = job;
        record.reserved = job->requirements.resources;
        job->state = "ASSIGNED";
        job->assigned_at = Clock::now();
    }
}
void submit(int fd, Registry& registry, const Message& message) {
    // Schema: CPU, RAM bytes, timeout seconds, OS, architecture, argv...
    const auto& fields = message.fields;
    if (fields.size() < 6 || fields.size() > 133 || fields[5].empty() ||
        fields[3] != "linux" || !label(fields[4]))
        throw std::runtime_error("invalid submission schema");
    auto cpu = number(fields[0]);
    auto memory = number(fields[1]);
    auto timeout = number(fields[2]);
    if (cpu == 0 || cpu > 1048576 || timeout == 0 || timeout > 30)
        throw std::runtime_error("invalid job resource request or timeout");
    for (std::size_t i = 5; i < fields.size(); ++i)
        if (fields[i].find('\0') != std::string::npos) throw std::runtime_error("NUL in argument");
    auto job = std::make_shared<Job>();
    job->requirements = {fields[3], fields[4], {static_cast<std::uint32_t>(cpu), memory}, false};
    job->timeout = static_cast<std::uint32_t>(timeout);
    job->arguments.assign(fields.begin() + 5, fields.end());
    {
        std::unique_lock lock(registry.mutex);
        if (registry.jobs.size() >= 128) throw std::runtime_error("job queue full");
        job->id = "job-" + std::to_string(registry.next_job++);
        registry.jobs.emplace(job->id, job);
        const auto queued_at = Clock::now();
        schedule(registry);
        while (!job->terminal && !stopped) {
            registry.changed.wait_for(lock, std::chrono::milliseconds(100));
            if (job->state == "QUEUED" && Clock::now() - queued_at >= std::chrono::seconds(5)) {
                job->terminal = true;
                job->state = "UNSCHEDULABLE";
                job->completion = {Type::error, {job->id + ": no available compatible worker within 5 seconds"}};
            }
        }
        if (!job->terminal) {
            job->state = "CANCELLED";
            job->completion = {Type::error, {job->id + ": coordinator stopping"}};
            job->terminal = true;
        }
        registry.jobs.erase(job->id);
        registry.history.push_back(job);
        if (registry.history.size() > 64) registry.history.pop_front();
    }
    net::send(fd, job->completion);
}
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
std::string summary(const std::vector<std::string>& arguments) {
    std::string text;
    for (const auto& arg : arguments) {
        if (!text.empty()) text += ' ';
        for (unsigned char c : arg) {
            if (text.size() >= 80) return text + "...";
            text += c >= 32 && c < 127 ? static_cast<char>(c) : '?';
        }
    }
    return text;
}
Message inspect(Registry& registry, const Message& request) {
    std::lock_guard lock(registry.mutex);
    if (request.type == Type::jobs && request.fields.empty()) {
        Message reply{Type::job_list, {}};
        auto append = [&reply](const std::shared_ptr<Job>& job) {
            reply.fields.insert(reply.fields.end(), {job->id, job->state,
                job->worker.empty() ? "-" : job->worker,
                job->terminal && job->completion.type == Type::result ? job->completion.fields[1] : "-",
                summary(job->arguments)});
        };
        for (const auto& [id, job] : registry.jobs) append(job);
        for (const auto& job : registry.history) append(job);
        return reply;
    }
    if (request.type != Type::logs || request.fields.size() != 1 || !label(request.fields[0]))
        throw std::runtime_error("invalid inspection request");
    const auto& id = request.fields[0];
    auto active = registry.jobs.find(id);
    if (active != registry.jobs.end()) {
        if (active->second->terminal) return active->second->completion;
        return {Type::error, {id + ": not finished; live streaming is not implemented"}};
    }
    for (const auto& job : registry.history)
        if (job->id == id) return job->completion;
    return {Type::error, {id + ": unknown or evicted job (history is in memory only)"}};
}
void handle(net::Socket socket, Registry& registry) {
    std::string registered;
    try {
        auto message = net::receive(socket.get());
        if (message.type == Type::cluster && message.fields.empty()) {
            net::send(socket.get(), snapshot(registry));
            return;
        }
        if (message.type == Type::jobs || message.type == Type::logs) {
            net::send(socket.get(), inspect(registry, message));
            return;
        }
        if (message.type == Type::submit) {
            submit(socket.get(), registry, message);
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
            registry.workers.insert_or_assign(f[0], Record{f, Clock::now(), true, nullptr, {0, 0}});
            registered = f[0];
        }
        net::send(socket.get(), {Type::ok, {}});
        while (!stopped) {
            message = net::receive(socket.get(), std::chrono::seconds(2));
            Message reply{Type::ok, {}};
            {
                std::lock_guard lock(registry.mutex);
                auto& record = registry.workers.at(registered);
                if (record.job && Clock::now() - record.job->assigned_at >
                    std::chrono::seconds(record.job->timeout + 5))
                    throw std::runtime_error("job result deadline exceeded");
                if (message.type == Type::result) {
                    const auto& r = message.fields;
                    if (!record.job || record.job->state != "RUNNING" || r.size() != 6 ||
                        r[0] != record.job->id || number(r[1]) > 255 ||
                        r[2].size() > 16384 || r[3].size() > 16384 ||
                        (r[4] != "0" && r[4] != "1") || (r[5] != "0" && r[5] != "1"))
                        throw std::runtime_error("invalid or stale job result");
                    finish(registry, record, message);
                } else if (message.type != Type::heartbeat || !message.fields.empty()) {
                    throw std::runtime_error("expected heartbeat or result");
                }
                record.last_seen = Clock::now();
                schedule(registry);
                // Results receive OK; assignments are delivered on heartbeats.
                if (message.type == Type::heartbeat && record.job && record.job->state == "ASSIGNED") {
                    auto& job = *record.job;
                    reply = {Type::assignment, {job.id, std::to_string(job.timeout)}};
                    reply.fields.insert(reply.fields.end(), job.arguments.begin(), job.arguments.end());
                    job.state = "RUNNING";
                }
            }
            net::send(socket.get(), reply);
        }
    } catch (const std::exception& error) {
        try { net::send(socket.get(), {Type::error, {error.what()}}, std::chrono::milliseconds(100)); }
        catch (...) {} // A disconnected client must not terminate the server.
    }
    if (!registered.empty()) {
        std::lock_guard lock(registry.mutex);
        auto& record = registry.workers.at(registered);
        record.connected = false;
        if (record.job) finish(registry, record, {Type::error, {record.job->id + ": worker lost; not retried"}});
        schedule(registry);
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
    std::cout << "Worker " << id << " registered" << std::endl;
    std::atomic<bool> cancel{false};
    std::future<ExecutionResult> execution;
    // Cancel before the future destructor waits during disconnect or shutdown.
    struct CancelOnExit { std::atomic<bool>& flag; ~CancelOnExit() { flag = true; } } cleanup{cancel};
    std::string job_id;
    while (!stopped) {
        for (int i = 0; i < 5 && !stopped; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
        if (stopped) break;
        if (execution.valid() && execution.wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
            ExecutionResult result;
            try { result = execution.get(); }
            catch (const std::exception& error) { result.error = error.what(); }
            net::send(socket.get(), {Type::result, {job_id, std::to_string(result.exit_code),
                result.output, result.error, result.timed_out ? "1" : "0", result.truncated ? "1" : "0"}});
            expect_ok(net::receive(socket.get()));
            job_id.clear();
        }
        net::send(socket.get(), {Type::heartbeat, {}});
        auto response = net::receive(socket.get());
        if (response.type == Type::assignment) {
            if (execution.valid() || response.fields.size() < 3 || response.fields.size() > 130 ||
                !label(response.fields[0])) throw std::runtime_error("invalid assignment");
            const auto timeout = number(response.fields[1]);
            if (timeout == 0 || timeout > 30) throw std::runtime_error("invalid execution timeout");
            job_id = response.fields[0];
            std::vector<std::string> arguments(response.fields.begin() + 2, response.fields.end());
            execution = std::async(std::launch::async, [arguments = std::move(arguments), timeout, &cancel] {
                return execute(arguments, static_cast<std::uint32_t>(timeout), cancel);
            });
        } else expect_ok(response);
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

int ps(std::uint16_t port) {
    auto socket = net::connect_loopback(port);
    net::send(socket.get(), {Type::jobs, {}});
    auto response = net::receive(socket.get());
    if (response.type != Type::job_list || response.fields.size() % 5 != 0)
        throw std::runtime_error("invalid job list");
    std::cout << "ID\tSTATE\tWORKER\tEXIT\tCOMMAND\n";
    for (std::size_t i = 0; i < response.fields.size(); ++i)
        std::cout << response.fields[i] << ((i + 1) % 5 == 0 ? '\n' : '\t');
    return 0;
}
int logs(std::uint16_t port, const std::string& id) {
    auto socket = net::connect_loopback(port);
    net::send(socket.get(), {Type::logs, {id}});
    auto response = net::receive(socket.get());
    if (response.type == Type::error && response.fields.size() == 1)
        throw std::runtime_error(response.fields[0]);
    if (response.type != Type::result || response.fields.size() != 6)
        throw std::runtime_error("invalid retained result");
    std::cout << response.fields[2];
    std::cerr << response.fields[3];
    std::cerr << "[forge] " << response.fields[0] << " exit=" << response.fields[1] << '\n';
    if (response.fields[4] == "1") std::cerr << "[forge] execution timed out\n";
    if (response.fields[5] == "1") std::cerr << "[forge] output truncated at 16384 bytes per stream\n";
    return 0; // retrieval succeeded, independent of the original command's exit
}

int run(std::uint16_t port, std::uint32_t cpu, std::uint64_t memory,
        std::uint32_t timeout, const std::vector<std::string>& arguments) {
    utsname system{};
    if (::uname(&system) != 0) throw std::runtime_error("architecture discovery failed");
    Message request{Type::submit, {std::to_string(cpu), std::to_string(memory),
        std::to_string(timeout), "linux", system.machine}};
    request.fields.insert(request.fields.end(), arguments.begin(), arguments.end());
    auto socket = net::connect_loopback(port);
    net::send(socket.get(), request);
    auto result = net::receive(socket.get(), std::chrono::seconds(45));
    if (result.type == Type::error && result.fields.size() == 1)
        throw std::runtime_error(result.fields.front());
    if (result.type != Type::result || result.fields.size() != 6 || number(result.fields[1]) > 255)
        throw std::runtime_error("invalid job result");
    const auto& f = result.fields;
    std::cerr << "[forge] " << f[0] << " exit=" << f[1] << '\n';
    std::cout << f[2];
    std::cerr << f[3];
    if (f[4] == "1") std::cerr << "[forge] execution timed out\n";
    if (f[5] == "1") std::cerr << "[forge] output truncated at 16384 bytes per stream\n";
    return static_cast<int>(number(f[1]));
}
}
